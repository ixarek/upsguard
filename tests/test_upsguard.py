import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "upsguard"))
import upsguard as app


def config(**changes):
    base = app.Config(
        role="master", action_mode="off",
        ups="delta@localhost", poll_interval=2, query_timeout=5,
        charge_percent=25, trigger_hold=20, ob_debounce=5, ol_stable=30,
        shutdown_grace=120, lb_grace=120, wake_interval=900,
        boot_check_timeout=240, comms_notify_after=60, dry_run=True,
        wall=False, state_file="/tmp/upsguard-test-state",
        log_file="/tmp/upsguard-test-log", display_backend="x11",
        display_name=":0", xauthority="/tmp/Xauthority", display_refresh=30,
        display_popup=False)
    return replace(base, **changes)


def sample(status, charge="100"):
    return app.parse_upsc("ups.status: %s\nbattery.charge: %s\n" % (status, charge))


def kinds(events):
    return [event.kind for event in events]


class PolicyTests(unittest.TestCase):
    def test_low_charge_shutdown_after_hold_and_grace(self):
        policy = app.Policy(config())
        self.assertEqual(kinds(policy.step(sample("OB", "24"), 0)), [])
        self.assertIn("outage", kinds(policy.step(sample("OB", "24"), 5)))
        self.assertIn("warning", kinds(policy.step(sample("OB", "24"), 25)))
        self.assertNotIn("power_action", kinds(policy.step(sample("OB", "24"), 144)))
        self.assertEqual(kinds(policy.step(sample("OB", "24"), 145)), ["power_action"])
        self.assertNotIn("power_action", kinds(policy.step(sample("OB", "24"), 146)))

    def test_return_to_stable_ol_cancels_shutdown(self):
        policy = app.Policy(config())
        for t in (0, 5, 25):
            policy.step(sample("OB", "24"), t)
        self.assertEqual(policy.deadline, 145)
        policy.step(sample("OL", "26"), 50)
        self.assertIn("restored", kinds(policy.step(sample("OL", "26"), 80)))
        self.assertNotIn("power_action", kinds(policy.step(sample("OL"), 145)))

    def test_loss_of_fresh_data_cancels_pending_shutdown(self):
        policy = app.Policy(config())
        for t in (0, 5, 25):
            policy.step(sample("OB", "24"), t)
        policy.step(sample("OL"), 50)
        self.assertIn("sensor_lost", kinds(policy.step(None, 69)))
        self.assertNotIn("power_action", kinds(policy.step(None, 200)))
        self.assertNotIn("power_action", kinds(policy.step(sample("OB", "24"), 202)))

    def test_lb_has_two_minute_grace_and_stable_ol_cancels(self):
        policy = app.Policy(config())
        for t in (0, 5, 25):
            policy.step(sample("OB", "24"), t)
        policy.step(sample("OB LB", "20"), 30)
        self.assertEqual(policy.deadline, 145)
        self.assertNotIn("restored", kinds(policy.step(sample("OL"), 80)))
        self.assertIn("restored", kinds(policy.step(sample("OL"), 110)))
        self.assertNotIn("power_action", kinds(policy.step(sample("OL"), 145)))

    def test_no_connection_does_not_invent_an_outage(self):
        policy = app.Policy(config())
        self.assertEqual(kinds(policy.step(None, 0)), ["connection_lost"])
        self.assertEqual(kinds(policy.step(None, 60)), ["warning"])
        self.assertNotIn("power_action", kinds(policy.step(None, 3600)))

    def test_restart_recovers_current_outage_and_deadline(self):
        original = app.Policy(config())
        for t in (0, 5, 25):
            original.step(sample("OB", "24"), t)
        record = original.snapshot(25, 1025, "same-boot")
        recovered = app.Policy(config())
        recovered.restore(record, 35, 1035, "same-boot")
        self.assertEqual(recovered.deadline, 145)
        self.assertIn("power_action", kinds(recovered.step(sample("OB", "24"), 145)))
        other_boot = app.Policy(config())
        other_boot.restore(record, 35, 1035, "different-boot")
        self.assertIsNone(other_boot.outage_since)

    def test_after_dry_run_restart_live_mode_can_issue_shutdown(self):
        original = app.Policy(config())
        for t in (0, 5, 25, 145):
            original.step(sample("OB", "24"), t)
        self.assertTrue(original.issued)
        record = original.snapshot(145, 1145, "same-boot")
        recovered = app.Policy(config(dry_run=False))
        recovered.restore(record, 146, 1146, "same-boot")
        self.assertIn("power_action", kinds(recovered.step(sample("OB", "24"), 146)))

    def test_changing_action_mode_starts_a_fresh_grace_period(self):
        original = app.Policy(config(action_mode="no"))
        for t in (0, 5, 25):
            original.step(sample("OB", "24"), t)
        record = original.snapshot(25, 1025, "same-boot")
        changed = app.Policy(config(action_mode="mem"))
        changed.restore(record, 140, 1140, "same-boot")
        self.assertIsNone(changed.deadline)
        self.assertNotIn("power_action", kinds(changed.step(sample("OB", "24"), 145)))
        self.assertIn("warning", kinds(changed.step(sample("OB", "24"), 165)))
        self.assertEqual(changed.deadline, 285)

    def test_configurable_50_percent_boundary(self):
        policy = app.Policy(config(charge_percent=50))
        for t in (0, 5, 25):
            events = policy.step(sample("OB", "50"), t)
        self.assertIn("warning", kinds(events))
        higher = app.Policy(config(charge_percent=50))
        for t in (0, 5, 25):
            events = higher.step(sample("OB", "50.1"), t)
        self.assertNotIn("warning", kinds(events))

    def test_monitor_only_never_requests_poweroff(self):
        policy = app.Policy(config(action_mode="monitor_only"))
        for t in (0, 5, 25, 145, 300):
            events = policy.step(sample("OB LB", "1"), t)
            self.assertNotIn("power_action", kinds(events))
        self.assertIsNone(policy.deadline)
        policy.step(sample("OL"), 330)
        self.assertIn("restored", kinds(policy.step(sample("OL"), 360)))


class IntegrationTests(unittest.TestCase):
    def test_client_reads_nut_without_local_usb_or_upsc(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(1024)
                self.request.sendall(
                    b'BEGIN LIST VAR delta\nVAR delta ups.status "OB"\n'
                    b'VAR delta battery.charge "49"\nEND LIST VAR delta\n')
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            cfg = config(role="client", ups="delta@127.0.0.1:%d" % server.server_address[1])
            result = app.read_ups(cfg, usb_check=lambda: False)
            thread.join(2)
        self.assertEqual(result.flags, frozenset(("OB",)))
        self.assertEqual(result.charge, 49)

    def test_client_rejects_nut_stale_data(self):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(1024)
                self.request.sendall(b"ERR DATA-STALE\n")
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            cfg = config(role="client", ups="delta@127.0.0.1:%d" % server.server_address[1])
            with self.assertRaises(app.QueryError):
                app.read_ups(cfg)
            thread.join(2)

    def test_master_rejects_unplugged_usb_even_if_upsc_has_old_data(self):
        class Result:
            returncode = 0
            stderr = ""
            stdout = "ups.status: OB\nbattery.charge: 49\n"
        with self.assertRaises(app.QueryError):
            app.read_ups(config(), runner=lambda *a, **kw: Result(),
                         usb_check=lambda: False)
        checks = iter((True, False))
        with self.assertRaises(app.QueryError):
            app.read_ups(config(), runner=lambda *a, **kw: Result(),
                         usb_check=lambda: next(checks))

    def test_display_mode_blanks_and_restores_without_shutdown(self):
        calls = []
        class Result:
            returncode = 0
            stderr = ""
        def runner(command, **_kwargs):
            calls.append(command)
            return Result()
        display = app.DisplayControl(config(action_mode="monitor_only", dry_run=False),
                                     runner=runner)
        display.step(True, 0)
        display.step(True, 10)
        display.step(True, 30)
        display.step(False, 31)
        self.assertEqual([item[-1] for item in calls], ["off", "off", "on"])

    def test_upsc_parser_uses_stdout_and_rejects_invalid_charge(self):
        result = app.parse_upsc("ups.status: OL\nbattery.charge: NaN\nups.load: 10.0\n")
        self.assertEqual(result.flags, frozenset(("OL",)))
        self.assertIsNone(result.charge)
        with self.assertRaises(app.QueryError):
            app.parse_upsc("battery.charge: 100\n")

    def test_poweroff_arms_rtc_then_powers_off_without_ups_command(self):
        calls = []
        class Result:
            returncode = 0
            stderr = ""
        def runner(command, **_kwargs):
            calls.append(command)
            return Result()
        class Store:
            def write(self, record):
                self.record = record.copy()
        store = Store()
        record = {"version": 1, "powered_off_by_policy": None}
        app.power_action(config(action_mode="no", dry_run=False), store, record,
                         "battery <= 25%", runner)
        self.assertEqual(calls, [["rtcwake", "-m", "no", "-s", "900"],
                                 ["systemctl", "poweroff"]])
        self.assertIsNotNone(store.record["powered_off_by_policy"])

    def test_sleep_calls_rtcwake_without_shutdown_and_without_short_timeout(self):
        class Result:
            returncode = 0
            stderr = b""
        class Store:
            def write(self, record):
                self.record = record.copy()
        for mode in ("mem", "disk", "standby", "freeze"):
            with self.subTest(mode=mode):
                calls = []
                store = Store()
                def runner(command, **kwargs):
                    calls.append((command, kwargs["timeout"]))
                    return Result()
                app.power_action(config(action_mode=mode, dry_run=False),
                                 store, {"version": 1}, "battery low", runner)
                self.assertEqual(calls, [(["rtcwake", "-m", mode, "-s", "900"], None)])
                self.assertIsNotNone(store.record["powered_off_by_policy"])

    def test_rtc_failure_cannot_turn_off_computer(self):
        calls = []
        class Result:
            returncode = 1
            stderr = b"no RTC alarm"
        class Store:
            def write(self, record):
                pass
        def runner(command, **_kwargs):
            calls.append(command)
            return Result()
        with self.assertRaisesRegex(RuntimeError, "rtcwake failed"):
            app.power_action(config(action_mode="no", dry_run=False), Store(),
                             {"version": 1}, "OB", runner)
        self.assertEqual(calls, [["rtcwake", "-m", "no", "-s", "900"]])

    def test_off_mode_lets_rtcwake_do_the_shutdown(self):
        calls = []
        class Result:
            returncode = 0
            stderr = b""
        class Store:
            def write(self, record):
                pass
        def runner(command, **_kwargs):
            calls.append(command)
            return Result()
        app.power_action(config(action_mode="off", dry_run=False), Store(),
                         {"version": 1}, "OB", runner)
        self.assertEqual(calls, [["rtcwake", "-m", "off", "-s", "900"]])

    def test_boot_check_waits_for_stable_ol_and_clears_marker(self):
        now = [0]
        class Store:
            record = {"version": 1, "boot_id": "old",
                      "powered_off_by_policy": {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        store = Store()
        app.boot_check(config(), store, threading.Event(),
                       clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=lambda: sample("OL"))
        self.assertIsNone(store.record["powered_off_by_policy"])
        self.assertGreaterEqual(now[0], 30)

    def test_boot_check_ob_rearms_and_shuts_down(self):
        now = [0]
        stop = threading.Event()
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        def action(cfg, store, record, reason):
            actions.append(reason)
            stop.set()
        app.boot_check(config(dry_run=False), Store(), stop,
                       clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=lambda: sample("OB", "24"), action=action)
        self.assertEqual(len(actions), 1)
        self.assertGreaterEqual(now[0], 125)

    def test_sleep_wakes_on_ob_waits_again_then_repeats(self):
        now = [0]
        stop = threading.Event()
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        store = Store()
        def action(cfg, store, record, reason):
            actions.append(now[0])
            now[0] += 900
            if len(actions) == 2:
                stop.set()
        app.boot_check(config(action_mode="mem", dry_run=False),
                       store, stop, clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=lambda: sample("OB", "49"), action=action)
        self.assertEqual(len(actions), 2)
        self.assertGreaterEqual(actions[1] - actions[0], 1025)

    def test_sleep_wake_without_usb_does_not_repeat(self):
        now = [0]
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        store = Store()
        def action(cfg, store, record, reason):
            actions.append(now[0])
            now[0] += 900
        app.boot_check(config(action_mode="mem", dry_run=False),
                       store, threading.Event(), clock=lambda: now[0],
                       wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=lambda: sample("OB") if not actions else None,
                       action=action)
        self.assertEqual(len(actions), 1)
        self.assertIsNone(store.record["powered_off_by_policy"])

    def test_monitor_sleep_resumes_then_stays_on_when_ol_is_stable(self):
        now = [0]
        stop = threading.Event()
        actions = []
        class Store:
            record = {"version": 1}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
                if actions and record.get("powered_off_by_policy") is None:
                    stop.set()
        store = Store()
        def action(cfg, store, record, reason):
            actions.append(now[0])
            record["powered_off_by_policy"] = {"epoch": 1000 + now[0], "reason": reason}
            store.write(record)
            now[0] += 900
        app.monitor(config(action_mode="mem", dry_run=False, wall=False),
                    store, stop, clock=lambda: now[0], wall_clock=lambda: 1000 + now[0],
                    sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    reader=lambda: sample("OB", "24") if not actions else sample("OL"),
                    action=action)
        self.assertEqual(len(actions), 1)
        self.assertIsNone(store.record["powered_off_by_policy"])

    def test_ascii_formatter_escapes_legacy_cyrillic_reason(self):
        import logging
        record = logging.LogRecord("upsguard", logging.WARNING, __file__, 1,
                                   "UPS: %s", ("сеть",), None)
        rendered = app.ASCIIFormatter("%(message)s").format(record)
        self.assertEqual(rendered, "UPS: \\u0441\\u0435\\u0442\\u044c")

    def test_boot_check_missing_usb_does_not_poweroff(self):
        now = [0]
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        store = Store()
        app.boot_check(config(dry_run=False), store, threading.Event(),
                       clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=lambda: None,
                       action=lambda *args: actions.append(args))
        self.assertEqual(actions, [])
        self.assertIsNone(store.record["powered_off_by_policy"])

    def test_boot_check_usb_disappears_during_two_minute_countdown(self):
        now = [0]
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        def reader():
            return sample("OB", "49") if now[0] < 60 else None
        app.boot_check(config(dry_run=False), Store(), threading.Event(),
                       clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=reader, action=lambda *args: actions.append(args))
        self.assertEqual(actions, [])

    def test_stopping_boot_check_within_grace_cancels_poweroff(self):
        now = [0]
        stop = threading.Event()
        actions = []
        class Store:
            record = {"version": 1, "powered_off_by_policy":
                      {"epoch": 990, "reason": "charge"}}
            def read(self):
                return self.record
            def write(self, record):
                self.record = record.copy()
        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 65:
                stop.set()
        app.boot_check(config(dry_run=False), Store(), stop,
                       clock=lambda: now[0], wall_clock=lambda: 1000,
                       sleeper=sleep, reader=lambda: sample("OB", "49"),
                       action=lambda *args: actions.append(args))
        self.assertEqual(actions, [])

    def test_state_store_is_atomic_and_preserves_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = app.StateStore(os.path.join(directory, "state.json"))
            data = {"version": 1, "powered_off_by_policy":
                    {"epoch": 1000, "reason": "battery"}}
            store.write(data)
            self.assertEqual(store.read(), data)
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)

    def test_config_uses_actual_delta_name_and_dry_run(self):
        path = os.path.join(os.path.dirname(__file__), "..", "conf", "upsguard.ini")
        cfg = app.load_config(path)
        self.assertEqual(cfg.ups, "delta@localhost")
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.charge_percent, 50)
        self.assertEqual(cfg.action_mode, "off")

    def test_utf8_config_selects_action_mode(self):
        path = os.path.join(os.path.dirname(__file__), "..", "conf", "upsguard.ini")
        with open(path, encoding="utf-8") as stream:
            original = stream.read()
        self.assertIn("; Роль этого АРМ", original)
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "config.ini")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(original.replace("action_mode = off", "action_mode = mem"))
            self.assertEqual(app.load_config(target).action_mode, "mem")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(original.replace("action_mode = off", "action_mode = invalid"))
            with self.assertRaisesRegex(ValueError, "action_mode must be"):
                app.load_config(target)

    def test_cli_uses_its_program_folder_instead_of_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, 'program folder %$')
            os.makedirs(os.path.join(root, 'conf'))
            os.makedirs(os.path.join(root, 'upsguard'))
            program = os.path.join(root, 'upsguard', 'upsguard.py')
            shutil.copyfile(app.__file__, program)
            source = os.path.join(os.path.dirname(__file__), '..', 'conf', 'upsguard.ini')
            with open(source, encoding='utf-8') as stream:
                ini = stream.read().replace('charge_percent = 50', 'charge_percent = 87')
            own_config = os.path.join(root, 'conf', 'upsguard.ini')
            with open(own_config, 'w', encoding='utf-8') as stream:
                stream.write(ini)
            # A different working directory has a conflicting INI.
            os.makedirs(os.path.join(directory, 'conf'))
            decoy = os.path.join(directory, 'conf', 'upsguard.ini')
            with open(decoy, 'w', encoding='utf-8') as stream:
                stream.write(ini.replace('charge_percent = 87', 'charge_percent = 61'))
            result = subprocess.run([sys.executable, program, 'check-config'],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Config file: ' + own_config, result.stdout)
            self.assertIn('threshold=87.0%', result.stdout)
            override = subprocess.run([sys.executable, program, '--config', decoy,
                                       'check-config'], cwd=directory,
                                      capture_output=True, text=True)
            self.assertEqual(override.returncode, 0, override.stderr)
            self.assertIn('Config file: ' + decoy, override.stdout)
            self.assertIn('threshold=61.0%', override.stdout)

    def test_old_ini_poweroff_and_sleep_remain_supported(self):
        path = os.path.join(os.path.dirname(__file__), "..", "conf", "upsguard.ini")
        with open(path, encoding="utf-8") as stream:
            original = stream.read()
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "config.ini")
            def old_ini(action, rtc):
                return (original.replace("action_mode = off", "action_mode = " + action)
                        .replace("wake_interval = 900", "rtc_mode = " + rtc + "\nwake_interval = 900"))
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(old_ini("poweroff", "no"))
            self.assertEqual(app.load_config(target).action_mode, "no")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(old_ini("sleep", "disk"))
            self.assertEqual(app.load_config(target).action_mode, "disk")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(old_ini("mem", "no"))
            self.assertEqual(app.load_config(target).action_mode, "mem")


if __name__ == "__main__":
    unittest.main()

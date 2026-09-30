"""Desktop warnings must never bypass the fresh-data or cancellation checks."""
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from test_upsguard import app, config, sample
import desktop


class Store:
    def __init__(self, marker=False):
        self.record = {'version': 1, 'powered_off_by_policy':
                       {'epoch': 990, 'reason': 'battery'} if marker else None}

    def read(self):
        return self.record.copy()

    def write(self, record):
        self.record = record.copy()


class Alert:
    def __init__(self):
        self.stop_requested = self.dismissed = self.ready = False
        self.process = None
        self.updates = []
        self.closed = 0

    def poll(self):
        pass

    def update(self, data, now):
        self.updates.append((now, data))

    def close(self):
        self.closed += 1


class DesktopTests(unittest.TestCase):
    def test_timer_uses_the_policy_deadline_and_does_not_go_negative(self):
        data = app.desktop_data(config(charge_percent=50), sample('OB', '49'), 120)
        self.assertEqual(desktop.panel_text(data, 0)['timer'], '02:00')
        self.assertEqual(desktop.panel_text(data, 0.2)['timer'], '02:00')
        self.assertEqual(desktop.panel_text(data, 119)['timer'], '00:01')
        self.assertEqual(desktop.panel_text(data, 125)['timer'], '00:00')
        self.assertEqual(desktop.panel_text(data, 0)['charge'], '49%')
        self.assertIn('ПРОБНЫЙ', desktop.panel_text(data, 0)['note'])
        data['phase'] = 'ol'
        self.assertIn('OL', desktop.panel_text(data, 0)['label'])
        data['phase'] = 'error'
        self.assertEqual(desktop.panel_text(data, 0)['label'], 'ДО ПОВТОРА')
        data['deadline'] = None
        self.assertEqual(desktop.panel_text(data, 0)['timer'], '—')

    def test_monitor_closes_warning_and_cancels_action_on_missing_usb(self):
        now, stop, alert, store, actions = [0], threading.Event(), Alert(), Store(), []
        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 70:
                stop.set()
        app.monitor(config(dry_run=False), store, stop, clock=lambda: now[0],
                    wall_clock=lambda: 1000 + now[0], sleeper=sleep,
                    reader=lambda: sample('OB', '24') if now[0] < 40 else None,
                    action=lambda *args: actions.append(args), alert=alert)
        countdown = [(t, d) for t, d in alert.updates if d and d['deadline'] is not None]
        self.assertTrue(countdown)
        self.assertEqual(countdown[0][1]['deadline'] - countdown[0][0], 120)
        self.assertIn((40, None), alert.updates)
        self.assertIsNone(store.record['policy']['deadline_epoch'])
        self.assertEqual(actions, [])

    def test_monitor_shows_ol_confirmation_then_closes_warning(self):
        now, stop, alert, store, actions = [0], threading.Event(), Alert(), Store(), []
        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 84:
                stop.set()
        app.monitor(config(), store, stop, clock=lambda: now[0],
                    wall_clock=lambda: 1000 + now[0], sleeper=sleep,
                    reader=lambda: sample('OB', '24') if now[0] < 50 else sample('OL'),
                    action=lambda *args: actions.append(args), alert=alert)
        ol = [(t, d) for t, d in alert.updates if d and d['phase'] == 'ol']
        self.assertEqual(ol[0][1]['deadline'], 80)
        self.assertIn((80, None), alert.updates)
        self.assertEqual(actions, [])

    def test_power_action_error_is_visible_with_retry_time(self):
        now, stop, alert, store = [0], threading.Event(), Alert(), Store()
        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 150:
                stop.set()
        def action(*args):
            raise RuntimeError('rtcwake: standby is not supported')
        app.monitor(config(action_mode='standby', dry_run=False), store, stop,
                    clock=lambda: now[0], wall_clock=lambda: 1000 + now[0],
                    sleeper=sleep, reader=lambda: sample('OB', '24'),
                    action=action, alert=alert)
        errors = [(t, d) for t, d in alert.updates if d and d['phase'] == 'error']
        self.assertTrue(errors)
        self.assertIn('standby', errors[0][1]['error'])
        self.assertEqual(errors[0][1]['deadline'] - errors[0][0], 30)
        self.assertIsNone(store.record['powered_off_by_policy'])

    def test_stop_button_during_final_query_prevents_power_action(self):
        cancel = app.stop_from_desktop
        for method in (app.monitor, app.boot_check):
            with self.subTest(method=method.__name__):
                now, stop, alert, store = [0], threading.Event(), Alert(), Store(marker=True)
                actions, commands, reads = [], [], {}
                def reader():
                    reads[now[0]] = reads.get(now[0], 0) + 1
                    # The second read at an expired deadline is the final check.
                    if reads[now[0]] == 2:
                        alert.stop_requested = True
                    return sample('OB', '24')
                def button(alert, store, stop):
                    return cancel(alert, store, stop,
                                  launcher=lambda command, **kw: commands.append(command))
                with patch.object(app, 'stop_from_desktop', button):
                    method(config(dry_run=False), store, stop, clock=lambda: now[0],
                           wall_clock=lambda: 1000 + now[0],
                           sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                           reader=reader, action=lambda *args: actions.append(args), alert=alert)
                self.assertTrue(stop.is_set())
                self.assertEqual(actions, [])
                self.assertEqual(store.record['policy'], {})
                self.assertIsNone(store.record['powered_off_by_policy'])
                self.assertEqual(commands, [['systemctl', '--no-block', 'stop',
                                            'upsguard.service', 'upsguard-bootcheck.service']])

    def test_manual_stop_during_final_query_prevents_power_action(self):
        for method in (app.monitor, app.boot_check):
            with self.subTest(method=method.__name__):
                now, stop, actions, reads = [0], threading.Event(), [], {}
                def reader():
                    reads[now[0]] = reads.get(now[0], 0) + 1
                    if reads[now[0]] == 2:
                        stop.set()
                    return sample('OB', '24')
                method(config(dry_run=False), Store(marker=True), stop,
                       clock=lambda: now[0], wall_clock=lambda: 1000 + now[0],
                       sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
                       reader=reader, action=lambda *args: actions.append(args), alert=Alert())
                self.assertEqual(actions, [])

    def test_stop_button_stops_service_even_when_state_write_fails(self):
        alert, stop, commands = Alert(), threading.Event(), []
        alert.stop_requested = True
        store = Store(marker=True)
        with patch.object(store, 'write', side_effect=OSError('disk full')):
            self.assertTrue(app.stop_from_desktop(alert, store, stop,
                            launcher=lambda command, **kw: commands.append(command)))
        self.assertTrue(stop.is_set())
        self.assertEqual(len(commands), 1)
        self.assertIn('stop', commands[0])

    def test_run_checks_rtc_before_monitoring_and_honours_stop(self):
        for should_stop in (False, True):
            with self.subTest(should_stop=should_stop):
                order = []
                def check(cfg, store, stop):
                    order.append('boot-check')
                    if should_stop:
                        stop.set()
                with patch.object(app, 'load_config', return_value=config()), \
                     patch.object(app, 'setup_logging'), patch.object(app.signal, 'signal'), \
                     patch.object(app, 'acquire_lock', return_value=77), \
                     patch.object(app.os, 'close') as close, \
                     patch.object(app, 'StateStore', return_value=Store()), \
                     patch.object(app, 'boot_check', side_effect=check), \
                     patch.object(app, 'monitor', side_effect=lambda *a: order.append('monitor')):
                    self.assertEqual(app.main(['run']), 0)
                    close.assert_called_once_with(77)
                self.assertEqual(order, ['boot-check'] if should_stop else ['boot-check', 'monitor'])

    def test_popup_demo_never_calls_service_or_power_commands(self):
        alert = Alert()
        def update(data, now):
            alert.updates.append((now, data))
            alert.dismissed = True
        alert.update = update
        with patch.object(app, 'load_config', return_value=config(dry_run=False)), \
             patch.object(app, 'setup_logging'), patch.object(app, 'DesktopAlert', return_value=alert), \
             patch.object(app, 'stop_from_desktop') as button, \
             patch.object(app, 'power_action') as power, \
             patch.object(app, 'acquire_lock') as lock, \
             patch.object(app, 'read_ups') as ups:
            self.assertEqual(app.main(['test-popup']), 0)
            for operation in (button, power, lock, ups):
                operation.assert_not_called()
        self.assertTrue(alert.updates[0][1]['demo'])
        self.assertTrue(alert.updates[0][1]['dry_run'])

    def test_worker_without_x11_reports_error_and_exits(self):
        env = os.environ.copy()
        env['DISPLAY'] = ':65520'
        result = subprocess.run([sys.executable, desktop.__file__], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['event'], 'error')

    def test_popup_configuration_is_optional_for_previous_ini(self):
        import tempfile
        path = os.path.join(os.path.dirname(__file__), '..', 'conf', 'upsguard.ini')
        with open(path, encoding='utf-8') as stream:
            original = stream.read()
        self.assertTrue(app.load_config(path).display_popup)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'config.ini')
            for contents, expected in ((original.replace('popup = yes\n', ''), True),
                                       (original.replace('popup = yes', 'popup = no'), False)):
                with open(path, 'w', encoding='utf-8') as stream:
                    stream.write(contents)
                self.assertEqual(app.load_config(path).display_popup, expected)


if __name__ == '__main__':
    unittest.main()

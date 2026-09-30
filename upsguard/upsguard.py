#!/usr/bin/env python3
"""Read-only NUT monitor for one workstation powered by a USB UPS.

Python 3.7+, standard library only.  This program never sends a command to
the UPS; the only power commands target the local computer.
"""

import argparse
import configparser
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional


LOG = logging.getLogger("upsguard")
STATE_VERSION = 1
MAX_STATE_AGE = 7 * 86400
SLEEP_MODES = frozenset(("mem", "disk", "standby", "freeze"))
RTC_MODES = SLEEP_MODES | {"no", "off"}
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                              "conf", "upsguard.ini")


@dataclass(frozen=True)
class Config:
    role: str
    action_mode: str
    ups: str
    poll_interval: float
    query_timeout: float
    charge_percent: float
    trigger_hold: float
    ob_debounce: float
    ol_stable: float
    shutdown_grace: float
    lb_grace: float
    wake_interval: int
    boot_check_timeout: float
    comms_notify_after: float
    dry_run: bool
    wall: bool
    state_file: str
    log_file: str
    display_backend: str
    display_name: str
    xauthority: str
    display_refresh: float
    display_popup: bool = True


def load_config(path):
    parser = configparser.ConfigParser(inline_comment_prefixes=(";",))
    if not parser.read(path, encoding="utf-8"):
        raise ValueError("Config not found: " + path)
    allowed = {
        "nut": {"ups", "poll_interval", "query_timeout"},
        "policy": {"charge_percent", "trigger_hold", "ob_debounce", "ol_stable",
                   "shutdown_grace", "lb_grace", "wake_interval", "rtc_mode",
                   "boot_check_timeout", "comms_notify_after"},
        "general": {"role", "action_mode", "dry_run", "wall", "state_file", "log_file"},
        "display": {"backend", "name", "xauthority", "refresh_interval", "popup"},
    }
    for section in parser.sections():
        if section not in allowed:
            raise ValueError("Unknown section: " + section)
        unknown = set(parser[section]) - allowed[section]
        if unknown:
            raise ValueError("Unknown setting: " + section + "." + sorted(unknown)[0])
    for section in ("nut", "policy", "general"):
        if section not in parser:
            raise ValueError("Missing section: " + section)
    selected_mode = parser.get("general", "action_mode", fallback="poweroff")
    legacy_rtc_mode = parser.get("policy", "rtc_mode", fallback=None)
    if selected_mode == "poweroff":
        # Accept installed configurations from previous releases unchanged.
        mode = legacy_rtc_mode or "no"
        if mode not in ("no", "off"):
            raise ValueError("Legacy poweroff requires rtc_mode=no or off")
    elif selected_mode == "sleep":
        mode = legacy_rtc_mode or "mem"
        if mode not in SLEEP_MODES:
            raise ValueError("Legacy sleep requires rtc_mode=mem, disk, standby or freeze")
    else:
        # A modern action_mode is authoritative, even if an older INI still
        # contains a stale rtc_mode line. This makes one-field edits safe.
        mode = selected_mode
    c = Config(
        role=parser.get("general", "role", fallback="master"),
        action_mode=mode,
        ups=parser.get("nut", "ups"),
        poll_interval=parser.getfloat("nut", "poll_interval"),
        query_timeout=parser.getfloat("nut", "query_timeout"),
        charge_percent=parser.getfloat("policy", "charge_percent"),
        trigger_hold=parser.getfloat("policy", "trigger_hold"),
        ob_debounce=parser.getfloat("policy", "ob_debounce"),
        ol_stable=parser.getfloat("policy", "ol_stable"),
        shutdown_grace=parser.getfloat("policy", "shutdown_grace"),
        lb_grace=parser.getfloat("policy", "lb_grace"),
        wake_interval=parser.getint("policy", "wake_interval"),
        boot_check_timeout=parser.getfloat("policy", "boot_check_timeout"),
        comms_notify_after=parser.getfloat("policy", "comms_notify_after"),
        dry_run=parser.getboolean("general", "dry_run"),
        wall=parser.getboolean("general", "wall"),
        state_file=parser.get("general", "state_file"),
        log_file=parser.get("general", "log_file"),
        display_backend=parser.get("display", "backend", fallback="auto"),
        display_name=parser.get("display", "name", fallback=":0"),
        xauthority=parser.get("display", "xauthority", fallback=""),
        display_refresh=parser.getfloat("display", "refresh_interval", fallback=30),
        display_popup=parser.getboolean("display", "popup", fallback=True),
    )
    if c.role not in ("master", "client"):
        raise ValueError("role must be master or client")
    if c.action_mode not in RTC_MODES | {"monitor_only"}:
        raise ValueError("action_mode must be off, mem, disk, standby, freeze, no or monitor_only")
    if c.display_backend not in ("auto", "x11", "console"):
        raise ValueError("display.backend must be auto, x11 or console")
    if not c.display_name.startswith(":") or any(ch.isspace() for ch in c.display_name):
        raise ValueError("display.name must look like :0")
    if c.xauthority and not os.path.isabs(c.xauthority):
        raise ValueError("display.xauthority must be an absolute path")
    if c.display_refresh < 5 or not math.isfinite(c.display_refresh):
        raise ValueError("display.refresh_interval must be at least 5 seconds")
    if not c.ups or any(ch.isspace() for ch in c.ups) or "@" not in c.ups:
        raise ValueError("nut.ups must look like delta@localhost")
    name, host, port = split_ups_address(c.ups)
    if c.role == "master" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("USB master must query local NUT")
    if c.role == "client" and host in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Client nut.ups must point to the master's IP")
    for name in ("poll_interval", "query_timeout", "trigger_hold", "ob_debounce",
                 "ol_stable", "shutdown_grace", "lb_grace", "boot_check_timeout",
                 "comms_notify_after"):
        value = getattr(c, name)
        if not math.isfinite(value) or value < 0:
            raise ValueError(name + " must be a finite nonnegative number")
    if c.poll_interval < 1 or c.query_timeout < 1:
        raise ValueError("poll_interval and query_timeout must be at least 1 second")
    if not 0 <= c.charge_percent <= 100:
        raise ValueError("charge_percent must be between 0 and 100")
    if c.shutdown_grace < c.ol_stable + 15:
        raise ValueError("shutdown_grace must exceed ol_stable by at least 15 seconds")
    if c.wake_interval < 180:
        raise ValueError("wake_interval must be at least 180 seconds")
    if c.boot_check_timeout < c.shutdown_grace + c.ob_debounce + c.poll_interval:
        raise ValueError("boot_check_timeout must cover OB detection and the grace period")
    if c.shutdown_grace < 60:
        raise ValueError("shutdown_grace must be at least 60 seconds")
    if not os.path.isabs(c.state_file) or not os.path.isabs(c.log_file):
        raise ValueError("state_file and log_file must be absolute paths")
    return c


class QueryError(Exception):
    pass


def split_ups_address(address):
    name, separator, hostport = address.partition("@")
    if not separator or not name or not hostport:
        raise ValueError("nut.ups must look like delta@localhost or delta@IP")
    if ":" in hostport and not hostport.startswith("["):
        host, text_port = hostport.rsplit(":", 1)
        if not text_port.isdigit():
            raise ValueError("Invalid NUT port")
        port = int(text_port)
    else:
        host, port = hostport, 3493
    if not host or not 1 <= port <= 65535:
        raise ValueError("Invalid NUT address")
    return name, host, port


@dataclass(frozen=True)
class Sample:
    flags: frozenset
    charge: Optional[float]
    raw: dict


def parse_upsc(output):
    values = {}
    for line in output.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            values[key] = value.strip()
    status = values.get("ups.status", "")
    flags = frozenset(status.split())
    if not flags or ("OB" in flags and "OL" in flags):
        raise QueryError("Missing or ambiguous ups.status in upsc response")
    charge = None
    try:
        candidate = float(values.get("battery.charge", ""))
        if math.isfinite(candidate) and 0 <= candidate <= 100:
            charge = candidate
    except ValueError:
        pass
    return Sample(flags, charge, values)


def usb_connected(root="/sys/bus/usb/devices"):
    """Physical 05dd:041b presence on ARM1; never infer it from cached NUT data."""
    try:
        with os.scandir(root) as devices:
            for device in devices:
                try:
                    with open(os.path.join(device.path, "idVendor"), encoding="ascii") as f:
                        vendor = f.read().strip().lower()
                    with open(os.path.join(device.path, "idProduct"), encoding="ascii") as f:
                        product = f.read().strip().lower()
                    if (vendor, product) == ("05dd", "041b"):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def read_nut_socket(cfg):
    """Read-only LIST VAR for ARM2, which needs no NUT packages."""
    name, host, port = split_ups_address(cfg.ups)
    try:
        with socket.create_connection((host, port), timeout=cfg.query_timeout) as conn:
            conn.settimeout(cfg.query_timeout)
            conn.sendall(("LIST VAR " + name + "\n").encode("ascii"))
            with conn.makefile("rb") as stream:
                expected_begin = "BEGIN LIST VAR " + name
                expected_end = "END LIST VAR " + name
                first = stream.readline(4097).decode("utf-8", "replace").strip()
                if first != expected_begin:
                    raise QueryError("NUT: " + (first or "no response"))
                values = {}
                size = 0
                for _ in range(300):
                    line = stream.readline(4097)
                    size += len(line)
                    if not line or len(line) > 4096 or size > 65536:
                        raise QueryError("NUT: incomplete or oversized response")
                    line = line.decode("utf-8", "replace").strip()
                    if line == expected_end:
                        return parse_upsc("".join(k + ": " + v + "\n" for k, v in values.items()))
                    try:
                        fields = shlex.split(line)
                    except ValueError as exc:
                        raise QueryError("NUT: malformed line") from exc
                    if len(fields) != 4 or fields[0:2] != ["VAR", name]:
                        raise QueryError("NUT: unexpected response: " + line[:100])
                    values[fields[2]] = fields[3]
                raise QueryError("NUT: too many variables")
    except (OSError, UnicodeError) as exc:
        raise QueryError(str(exc)) from exc


def read_ups(cfg, runner=subprocess.run, usb_check=usb_connected):
    if cfg.role == "client":
        return read_nut_socket(cfg)
    if not usb_check():
        raise QueryError("USB 05dd:041b missing on master")
    try:
        result = runner(["upsc", cfg.ups], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, universal_newlines=True,
                        timeout=cfg.query_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueryError(str(exc)) from exc
    if result.returncode:
        raise QueryError((result.stderr or result.stdout).strip() or
                         "upsc failed with code " + str(result.returncode))
    if not usb_check():
        raise QueryError("USB 05dd:041b unplugged during query")
    return parse_upsc(result.stdout)


def boot_id():
    with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as stream:
        return stream.read().strip()


def _valid_age(epoch, now):
    return isinstance(epoch, (int, float)) and math.isfinite(epoch) and 0 <= now - epoch <= MAX_STATE_AGE


class StateStore:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            if isinstance(data, dict) and data.get("version") == STATE_VERSION:
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            LOG.warning("State could not be read: %s", exc)
        return {"version": STATE_VERSION}

    def write(self, data):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass(frozen=True)
class Event:
    kind: str
    text: str


class Policy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ob_since = None
        self.outage_since = None
        self.ol_since = None
        self.low_since = None
        self.deadline = None
        self.reason = None
        self.critical = False
        self.issued = False
        self.comms_since = None
        self.comms_notified = False

    def restore(self, data, now, wall_now, current_boot):
        if data.get("boot_id") != current_boot:
            return
        policy = data.get("policy") or {}
        for attr in ("outage_since", "deadline"):
            epoch = policy.get(attr + "_epoch")
            valid = (_valid_age(epoch, wall_now) if attr == "outage_since" else
                     isinstance(epoch, (int, float)) and math.isfinite(epoch) and
                     abs(wall_now - epoch) <= MAX_STATE_AGE)
            if valid:
                setattr(self, attr, now - (wall_now - epoch))
        if self.outage_since is not None:
            self.ob_since = self.outage_since
            self.reason = policy.get("reason") or None
            self.critical = bool(policy.get("critical"))
            # A still-running host must be able to retry after a daemon restart
            # (or after changing dry_run from yes to no during an outage).
            self.issued = False
        else:
            self.deadline = None
        if policy.get("action_mode") != self.cfg.action_mode:
            # Switching from shutdown to sleep (or loading an old state) must
            # not reuse an almost-expired countdown for a different action.
            self.deadline = None
            self.reason = None
            self.critical = False
            self.issued = False
        if self.cfg.action_mode == "monitor_only":
            self.deadline = None
            self.reason = None
            self.critical = False
            self.issued = False

    def snapshot(self, now, wall_now, current_boot, marker=None):
        def epoch(value):
            # Stable across polls: avoid rewriting the state file every 2 s.
            return None if value is None else float(round(wall_now - (now - value)))
        return {
            "version": STATE_VERSION,
            "boot_id": current_boot,
            "policy": {
                "action_mode": self.cfg.action_mode,
                "outage_since_epoch": epoch(self.outage_since),
                "deadline_epoch": epoch(self.deadline),
                "reason": self.reason,
                "critical": self.critical,
                "issued": self.issued,
            },
            "powered_off_by_policy": marker,
        }

    def _warn(self, reason, due, critical, events):
        if critical:
            self.critical = True
        if self.deadline is None or due < self.deadline:
            self.deadline = due
            self.reason = reason
            events.append(Event("warning", "Power action pending: " + reason))

    def step(self, sample, now):
        events = []
        if sample is None:
            if self.outage_since is not None or self.deadline is not None:
                events.append(Event("sensor_lost", "No fresh UPS data: action cancelled"))
            self.outage_since = None
            self.deadline = None
            self.reason = None
            self.critical = False
            self.issued = False
            self.ob_since = None
            self.ol_since = None
            self.low_since = None
            if self.comms_since is None:
                self.comms_since = now
                events.append(Event("connection_lost", "NUT unavailable; waiting for recovery"))
            elif (not self.comms_notified and
                  now - self.comms_since >= self.cfg.comms_notify_after):
                self.comms_notified = True
                events.append(Event("warning", "NUT has been unavailable for a long time"))
        else:
            if self.comms_since is not None:
                events.append(Event("info", "NUT connection restored"))
                self.comms_since = None
                self.comms_notified = False
            if "OB" in sample.flags:
                self.ol_since = None
                if self.ob_since is None:
                    self.ob_since = now
                if (self.outage_since is None and
                        now - self.ob_since >= self.cfg.ob_debounce):
                    self.outage_since = self.ob_since
                    events.append(Event("outage", "Mains failed: UPS on battery (OB)"))
                if self.outage_since is not None:
                    if self.cfg.action_mode != "monitor_only":
                        if "LB" in sample.flags:
                            self._warn("UPS reports LB", now + self.cfg.lb_grace,
                                       True, events)
                        if sample.charge is not None and sample.charge <= self.cfg.charge_percent:
                            if self.low_since is None:
                                self.low_since = now
                            if now - self.low_since >= self.cfg.trigger_hold:
                                self._warn("battery charge %.1f%% <= %.1f%%" %
                                           (sample.charge, self.cfg.charge_percent),
                                           now + self.cfg.shutdown_grace, False, events)
                        else:
                            self.low_since = None
            elif "OL" in sample.flags:
                self.ob_since = None
                self.low_since = None
                if self.outage_since is not None:
                    if self.ol_since is None:
                        self.ol_since = now
                    if now - self.ol_since >= self.cfg.ol_stable:
                        self.outage_since = None
                        self.deadline = None
                        self.reason = None
                        self.issued = False
                        self.ol_since = None
                        events.append(Event("restored", "Mains restored (OL): action cancelled"))
                else:
                    self.ol_since = None
            else:
                # Unknown flags never establish a fresh outage or a mains return.
                self.ob_since = None if self.outage_since is None else self.ob_since
                self.ol_since = None
                self.low_since = None
        if (self.cfg.action_mode != "monitor_only" and self.deadline is not None and
                now >= self.deadline and not self.issued):
            self.issued = True
            events.append(Event("power_action", self.reason or "UPS threshold"))
        return events


class ASCIIFormatter(logging.Formatter):
    """Keep journal, SSH terminals and the log readable with any charset."""

    def format(self, record):
        return super().format(record).encode("ascii", "backslashreplace").decode("ascii")


def setup_logging(path):
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    formatter = ASCIIFormatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    LOG.addHandler(stream)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=5)
    handler.setFormatter(formatter)
    LOG.addHandler(handler)


def notify(cfg, event, runner=subprocess.run):
    level = logging.WARNING if event.kind in ("connection_lost", "sensor_lost", "warning", "power_action") else logging.INFO
    LOG.log(level, "%s: %s", event.kind, event.text)
    if not cfg.wall or event.kind not in ("outage", "warning", "restored", "power_action"):
        return
    try:
        result = runner(["wall", "upsguard: " + event.text], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=5)
        if result.returncode:
            LOG.warning("wall failed with code %d", result.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("wall could not run: %s", exc)


def power_action(cfg, store, record, reason, runner=subprocess.run):
    command = ["rtcwake", "-m", cfg.action_mode, "-s", str(cfg.wake_interval)]
    if cfg.dry_run:
        LOG.warning("DRY RUN: would execute %s for %s: %s",
                    " ".join(command), cfg.action_mode, reason)
        return
    record["powered_off_by_policy"] = {"epoch": time.time(), "reason": reason}
    store.write(record)
    try:
        # Sleep modes return only after resume; a 20-second timeout would kill rtcwake.
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=None if cfg.action_mode in SLEEP_MODES else 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("rtcwake failed: " + str(exc)) from exc
    if result.returncode:
        raise RuntimeError("rtcwake failed: " + str(result.stderr))
    if cfg.action_mode in SLEEP_MODES:
        LOG.info("Returned from RTC sleep; checking UPS before next action")
        return
    if cfg.action_mode == "off":
        LOG.info("rtcwake -m off requested shutdown; RTC wake in %d seconds", cfg.wake_interval)
        return
    LOG.info("RTC armed for %d seconds; requesting systemctl poweroff", cfg.wake_interval)
    result = runner(["systemctl", "poweroff"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=20)
    if result.returncode:
        raise RuntimeError("systemctl poweroff: " + str(result.stderr))


def xorg_auth(display):
    """Find the X server's -auth file if no XAUTHORITY was configured."""
    try:
        pids = (name for name in os.listdir("/proc") if name.isdigit())
        for pid in pids:
            try:
                with open("/proc/" + pid + "/cmdline", "rb") as stream:
                    args = [part.decode("utf-8", "replace") for part in
                            stream.read(8192).split(b"\0") if part]
                if (not args or os.path.basename(args[0]) not in ("Xorg", "X") or
                        display not in args or "-auth" not in args):
                    continue
                index = args.index("-auth")
                if index + 1 < len(args):
                    return args[index + 1]
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return ""


def x11_environment(cfg):
    env = os.environ.copy()
    env['DISPLAY'] = cfg.display_name
    auth = cfg.xauthority or xorg_auth(cfg.display_name)
    if auth:
        env['XAUTHORITY'] = auth
    return env


class DesktopAlert:
    """Keep the graphical worker separate from UPS polling and power actions."""
    def __init__(self, cfg, launcher=subprocess.Popen):
        self.cfg, self.launcher = cfg, launcher
        self.process = None
        self.buffer = b''
        self.dismissed = self.stop_requested = self.ready = False
        self.retry_after = 0

    def poll(self):
        if self.process is None:
            return
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.buffer += chunk
        except (BlockingIOError, OSError):
            pass
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                message = json.loads(line.decode('utf-8'))
            except (UnicodeError, ValueError):
                continue
            event = message.get('event')
            if event == 'stop':
                self.stop_requested = True
            elif event == 'dismiss':
                self.dismissed = True
            elif event == 'ready':
                self.ready = True
                LOG.info('Desktop warning window opened on %s', self.cfg.display_name)
            elif event == 'error':
                LOG.warning('Desktop warning unavailable: %s', message.get('message'))

    def update(self, data, now):
        self.poll()
        if data is None:
            self.close()
            self.dismissed = False
            return
        if self.dismissed:
            self.close()
            return
        if not self.cfg.display_popup or self.cfg.display_backend == 'console':
            return
        if self.process is not None and self.process.poll() is not None:
            self.close()
            self.retry_after = now + 15
        if self.process is None:
            if now < self.retry_after:
                return
            socket_path = '/tmp/.X11-unix/X' + self.cfg.display_name[1:].split('.')[0]
            if not os.path.exists(socket_path):
                LOG.info('Desktop not available yet on %s; warning remains in journal', self.cfg.display_name)
                self.retry_after = now + 15
                return
            try:
                worker = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'desktop.py')
                self.process = self.launcher([sys.executable, worker],
                    env=x11_environment(self.cfg), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                os.set_blocking(self.process.stdin.fileno(), False)
                os.set_blocking(self.process.stdout.fileno(), False)
                self.buffer, self.ready = b'', False
            except OSError as exc:
                LOG.warning('Could not open desktop warning: %s', exc)
                self.close()
                self.retry_after = now + 15
                return
        try:
            packet = (json.dumps(data, ensure_ascii=False) + '\n').encode('utf-8')
            # Messages are smaller than PIPE_BUF, so a blocked GUI cannot block
            # UPS polling or leave a partial JSON message in its input pipe.
            if len(packet) > 4096:
                raise ValueError('Desktop message is too large')
            os.write(self.process.stdin.fileno(), packet)
        except BlockingIOError:
            pass
        except (OSError, ValueError) as exc:
            LOG.warning('Desktop warning pipe: %s', exc)
            self.close()
            self.retry_after = now + 15

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
            self.process.wait(timeout=0.3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
                self.process.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        finally:
            self.process.stdout.close()
            self.process = None


def desktop_data(cfg, sample, deadline=None, phase='outage', error=None):
    return {'mode': cfg.action_mode, 'charge': sample.charge if sample else None,
            'threshold': cfg.charge_percent, 'deadline': deadline, 'phase': phase,
            'status': ' '.join(sorted(sample.flags)) if sample else '',
            'dry_run': cfg.dry_run, 'error': str(error)[:300] if error else None}


def stop_from_desktop(alert, store, stop, launcher=subprocess.Popen):
    alert.poll()
    if not alert.stop_requested:
        return False
    LOG.warning('Desktop button: cancelling countdown and stopping UPS Guard services')
    stop.set()
    try:
        data = store.read()
        data['policy'], data['powered_off_by_policy'] = {}, None
        store.write(data)
    except OSError as exc:
        # A failed state write must not prevent the requested service stop.
        LOG.error('Could not clear the saved countdown: %s', exc)
    alert.close()
    try:
        launcher(['systemctl', '--no-block', 'stop', 'upsguard.service', 'upsguard-bootcheck.service'],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        LOG.error('Could not stop systemd units: %s', exc)
    return True


class DisplayControl:
    def __init__(self, cfg, runner=subprocess.run, framebuffer="/sys/class/graphics/fb0/blank"):
        self.cfg = cfg
        self.runner = runner
        self.framebuffer = framebuffer
        self.blanked = False
        self.last_attempt = None

    def _backend(self):
        if self.cfg.display_backend != "auto":
            return self.cfg.display_backend
        socket_name = "/tmp/.X11-unix/X" + self.cfg.display_name[1:].split(".")[0]
        return "x11" if os.path.exists(socket_name) else "console"

    def _switch(self, off):
        if self.cfg.dry_run:
            LOG.info("DRY RUN: would turn display %s", "off" if off else "on")
            return True
        backend = self._backend()
        if backend == "console":
            try:
                with open(self.framebuffer, "w", encoding="ascii") as stream:
                    stream.write("1" if off else "0")
                LOG.info("Console display: %s", "off" if off else "on")
                return True
            except OSError as exc:
                LOG.error("Could not control console display: %s", exc)
                return False
        env = x11_environment(self.cfg)
        try:
            result = self.runner(["xset", "-display", self.cfg.display_name,
                                  "dpms", "force", "off" if off else "on"],
                                 env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=5)
            if result.returncode:
                LOG.error("xset: %s", result.stderr)
                return False
            LOG.info("X11 display: %s", "off" if off else "on")
            return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("xset unavailable: %s", exc)
            return False

    def step(self, should_blank, now):
        if should_blank:
            if (self.last_attempt is None or
                    now - self.last_attempt >= self.cfg.display_refresh):
                self.last_attempt = now
                if self._switch(True):
                    self.blanked = True
        elif self.blanked:
            if self._switch(False):
                self.blanked = False
                self.last_attempt = None

    def restore(self):
        if self.blanked:
            self._switch(False)
            self.blanked = False


def get_sample(cfg):
    try:
        return read_ups(cfg)
    except QueryError:
        return None


def describe(sample):
    raw = sample.raw
    return ("status=%s, charge=%s%%, runtime=%s c, load=%s%%, power=%s W, "
            "input=%s V, output=%s V" %
            (raw.get("ups.status", "?"), raw.get("battery.charge", "?"),
             raw.get("battery.runtime", "?"), raw.get("ups.load", "?"),
             raw.get("ups.realpower", "?"), raw.get("input.voltage", "?"),
             raw.get("output.voltage", "?")))


def valid_marker(data, now):
    marker = data.get("powered_off_by_policy")
    return marker if isinstance(marker, dict) and _valid_age(marker.get("epoch"), now) else None


def boot_check(cfg, store, stop, clock=time.monotonic, wall_clock=time.time,
               sleeper=None, reader=None, action=power_action, alert=None):
    desktop = alert or DesktopAlert(cfg)
    try:
        return _boot_check(cfg, store, stop, clock, wall_clock, sleeper, reader, action, desktop)
    finally:
        desktop.close()


def _boot_check(cfg, store, stop, clock, wall_clock, sleeper, reader, action, desktop):
    data = store.read()
    marker = valid_marker(data, wall_clock())
    if marker is None:
        return
    def clear_marker():
        data["powered_off_by_policy"] = None
        data["policy"] = {}
        data["boot_id"] = boot_id()
        store.write(data)
    if cfg.action_mode == "monitor_only":
        LOG.info("monitor_only: RTC wake check cleared without a power action")
        clear_marker()
        return
    LOG.warning("Previous UPS action (%s); checking mains before normal operation",
                marker.get("reason", "?"))
    sleeper = sleeper or stop.wait
    reader = reader or (lambda: get_sample(cfg))
    started = clock()
    ol_since = None
    ob_since = None
    shutdown_due = None
    last_valid = None
    while not stop.is_set() and clock() - started < cfg.boot_check_timeout:
        if stop_from_desktop(desktop, store, stop):
            return
        sample = reader()
        now = clock()
        if sample is not None and "OB" in sample.flags:
            last_valid = now
            ol_since = None
            if ob_since is None:
                ob_since = now
            if now - ob_since >= cfg.ob_debounce and shutdown_due is None:
                shutdown_due = now + cfg.shutdown_grace
                LOG.warning("RTC wake: UPS still OB; next action in %.0f seconds. "
                            "Stop the service to cancel", cfg.shutdown_grace)
            desktop.update(desktop_data(cfg, sample, shutdown_due), now)
            if shutdown_due is not None and now >= shutdown_due:
                fresh = reader()  # repeat just before the local power action
                if stop.is_set() or stop_from_desktop(desktop, store, stop):
                    return
                if fresh is None or "OB" not in fresh.flags:
                    LOG.warning("Fresh OB not confirmed: repeat action cancelled")
                    desktop.update(None, now)
                    shutdown_due = None
                    ob_since = None
                    sleeper(cfg.poll_interval)
                    continue
                if cfg.dry_run:
                    LOG.warning("DRY RUN: repeat action skipped")
                    clear_marker()
                    return
                try:
                    desktop.close()
                    action(cfg, store, dict(data), "Mains still absent after RTC wake")
                except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                    LOG.error("Repeat RTC action failed: %s", exc)
                    clear_marker()
                    return
                if cfg.action_mode in SLEEP_MODES:
                    # rtcwake returned after resume. Start a fresh OB/OL check and grace.
                    data = store.read()
                    started = clock()
                    ol_since = None
                    ob_since = None
                    shutdown_due = None
                    last_valid = None
                    continue
                # Let systemd complete the shutdown; do not start normal monitoring.
                if not stop.wait(30):
                    LOG.error("Shutdown requested, but OS is still running after 30 seconds")
                return
        elif sample is not None and "OL" in sample.flags:
            last_valid = now
            ob_since = None
            shutdown_due = None
            if ol_since is None:
                ol_since = now
            desktop.update(desktop_data(cfg, sample, ol_since + cfg.ol_stable, 'ol'), now)
            if now - ol_since >= cfg.ol_stable:
                LOG.info("Stable OL after RTC wake: keeping workstation online")
                clear_marker()
                return
        else:
            desktop.update(None, now)
            ob_since = None
            ol_since = None
            shutdown_due = None
            if last_valid is None and now - started >= 60:
                LOG.error("USB/NUT unavailable after RTC wake: keeping workstation online")
                clear_marker()
                return
        sleeper(cfg.poll_interval)
    if stop.is_set():
        return
    LOG.error("Could not confirm stable OL or OB within %.0f seconds; "
              "keeping workstation online for diagnosis", cfg.boot_check_timeout)
    clear_marker()


def monitor(cfg, store, stop, clock=time.monotonic, wall_clock=time.time,
            sleeper=None, reader=None, action=power_action, alert=None):
    sleeper = sleeper or stop.wait
    reader = reader or (lambda: get_sample(cfg))
    current_boot = boot_id()
    data = store.read()
    policy = Policy(cfg)
    policy.restore(data, clock(), wall_clock(), current_boot)
    marker = valid_marker(data, wall_clock())
    last_written = None
    last_diag = None
    display = DisplayControl(cfg) if cfg.action_mode == "monitor_only" else None
    desktop = alert or DesktopAlert(cfg)
    last_power_error = None
    LOG.info("Service started: %s, role=%s, action_mode=%s, threshold=%.1f%%, dry_run=%s",
             cfg.ups, cfg.role, cfg.action_mode, cfg.charge_percent, cfg.dry_run)
    try:
        while not stop.is_set():
            if stop_from_desktop(desktop, store, stop):
                return
            sample = reader()
            now = clock()
            if sample is not None and (last_diag is None or now - last_diag >= 60):
                LOG.info("UPS: %s", describe(sample))
                last_diag = now
            for event in policy.step(sample, now):
                notify(cfg, event)
                if event.kind == "power_action":
                    fresh = reader()
                    if stop.is_set() or stop_from_desktop(desktop, store, stop):
                        return
                    if fresh is None or "OB" not in fresh.flags:
                        LOG.warning("Fresh OB not confirmed before power action; cancelled")
                        policy.step(fresh, now)
                        policy.issued = False
                        if fresh is not None:
                            policy.deadline = now + cfg.shutdown_grace
                        continue
                    record = policy.snapshot(now, wall_clock(), current_boot, marker)
                    try:
                        desktop.close()
                        action(cfg, store, record, event.text)
                        last_power_error = None
                        marker = record.get("powered_off_by_policy")
                        if cfg.action_mode in SLEEP_MODES and not cfg.dry_run:
                            boot_check(cfg, store, stop, clock, wall_clock, sleeper, reader, action)
                            policy = Policy(cfg)
                            marker = valid_marker(store.read(), wall_clock())
                            last_written = None
                            break
                    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                        LOG.error("Power action failed: %s", exc)
                        last_power_error = exc
                        policy.issued = False
                        policy.deadline = now + 30
                        record["powered_off_by_policy"] = None
                        marker = None
                        store.write(record)
            if policy.outage_since is None or cfg.action_mode == 'monitor_only':
                desktop.update(None, now)
                last_power_error = None
            elif policy.ol_since is not None:
                desktop.update(desktop_data(cfg, sample, policy.ol_since + cfg.ol_stable, 'ol'), now)
            else:
                phase = ('done' if policy.issued and cfg.dry_run else
                         'error' if last_power_error else 'outage')
                desktop.update(desktop_data(cfg, sample, policy.deadline, phase, last_power_error), now)
            if display is not None:
                display.step(policy.outage_since is not None, now)
            record = policy.snapshot(now, wall_clock(), current_boot, marker)
            if record != last_written:
                store.write(record)
                last_written = record
            sleeper(cfg.poll_interval)
    finally:
        desktop.close()
        if display is not None:
            display.restore()


def acquire_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError("Another upsguard process is already running")
    return fd


def main(argv=None):
    parser = argparse.ArgumentParser(description="Delta UPS monitor for one workstation")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="INI path (default: conf/upsguard.ini in the program folder)")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "boot-check", "status", "check-config", "test-popup"))
    args = parser.parse_args(argv)
    config_path = os.path.abspath(args.config)
    try:
        cfg = load_config(config_path)
    except (ValueError, configparser.Error) as exc:
        print("Config error: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"),
              file=sys.stderr)
        return 2
    if args.command == "check-config":
        print("Config file: " + config_path.encode("ascii", "backslashreplace").decode("ascii"))
        print("Config OK; role=%s; action_mode=%s; UPS=%s; threshold=%.1f%%; "
              "grace=%.0fs; wake_interval=%ds; dry_run=%s" %
              (cfg.role, cfg.action_mode, cfg.ups, cfg.charge_percent,
               cfg.shutdown_grace, cfg.wake_interval, cfg.dry_run))
        return 0
    if args.command == "status":
        print("Config file: " + config_path.encode("ascii", "backslashreplace").decode("ascii"))
        try:
            sample = read_ups(cfg)
        except QueryError as exc:
            print("No UPS data: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"),
                  file=sys.stderr)
            return 1
        print(describe(sample))
        print("role=%s, action_mode=%s, threshold=%.1f%%, grace=%.0fs, wake_interval=%ds, dry_run=%s" %
              (cfg.role, cfg.action_mode, cfg.charge_percent, cfg.shutdown_grace,
               cfg.wake_interval, cfg.dry_run))
        return 0
    setup_logging(cfg.log_file)
    LOG.info("Config file: %s", config_path)
    if args.command == 'test-popup':
        desktop = DesktopAlert(cfg)
        due = time.monotonic() + 30
        data = desktop_data(cfg, parse_upsc('ups.status: OB\nbattery.charge: 49\n'), due)
        data['demo'], data['dry_run'] = True, True
        try:
            while time.monotonic() < due:
                desktop.update(data, time.monotonic())
                if desktop.stop_requested or desktop.dismissed:
                    return 0
                if desktop.process is None:
                    LOG.error('Test window unavailable; check display.popup, name and xauthority')
                    return 1
                time.sleep(0.1)
            if not desktop.ready:
                LOG.error('Test window did not open; check the desktop warning in the journal')
                return 1
            return 0
        finally:
            desktop.close()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    lock_fd = acquire_lock("/run/upsguard/lock")
    try:
        store = StateStore(cfg.state_file)
        if args.command == "boot-check":
            boot_check(cfg, store, stop)
        else:
            # Type=simple lets the desktop start while the RTC check is running.
            # Keep both stages under one lock and do not monitor before the check.
            boot_check(cfg, store, stop)
            if not stop.is_set():
                monitor(cfg, store, stop)
    except (OSError, RuntimeError) as exc:
        LOG.exception("Service error: %s", exc)
        return 1
    finally:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())

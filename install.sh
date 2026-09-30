#!/bin/sh
set -eu

cd "$(dirname "$0")"
project_dir=$(pwd -P)
config_file="$project_dir/conf/upsguard.ini"
program_file="$project_dir/upsguard/upsguard.py"

role=master
master_ip=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --role) [ "$#" -ge 2 ] || exit 2; role=$2; shift 2 ;;
        --master-ip) [ "$#" -ge 2 ] || exit 2; master_ip=$2; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$role" in master|client) ;; *) echo "Role must be master or client" >&2; exit 2 ;; esac
if [ "$role" = client ]; then
    [ -n "$master_ip" ] || { echo "Client requires --master-ip <MASTER_IP>" >&2; exit 2; }
    python3 - "$master_ip" <<'PY'
import ipaddress
import sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    sys.exit("A valid IPv4 address is required for the master")
PY
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run: sudo ./install.sh" >&2
    exit 1
fi
if [ "$role" = master ]; then
    command -v upsc >/dev/null 2>&1 || { echo "Install NUT on the master first" >&2; exit 1; }
fi
command -v rtcwake >/dev/null 2>&1 || { echo "rtcwake command missing" >&2; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "systemd command missing" >&2; exit 1; }

[ -f "$config_file" ] || { echo "Missing program-folder INI: $config_file" >&2; exit 1; }
# This is the only working INI. Set the requested role/address in place,
# preserving all other values, Russian comments and the file's ownership.
/usr/bin/python3 - "$project_dir" "$role" "$master_ip" <<'PY'
import configparser
import os
import re
import sys
import tempfile

project_dir, role, master_ip = sys.argv[1:]
sys.path.insert(0, os.path.join(project_dir, 'upsguard'))
from upsguard import load_config

target = os.path.realpath(os.path.join(project_dir, 'conf', 'upsguard.ini'))
with open(target, encoding='utf-8', newline='') as stream:
    original = stream.read()
parser = configparser.ConfigParser(inline_comment_prefixes=(';',))
parser.read_string(original)
updates = {('general', 'role'): role}
ups_name = parser.get('nut', 'ups').split('@', 1)[0]
if role == 'client':
    updates[('nut', 'ups')] = ups_name + '@' + master_ip
elif parser.get('general', 'role') == 'client':
    updates[('nut', 'ups')] = ups_name + '@localhost'
updates = {key: value for key, value in updates.items()
           if parser.get(*key) != value}
section = None
found = set()
lines = []
for line in original.splitlines(keepends=True):
    header = re.match(r'\s*\[([^]]+)\]', line)
    if header:
        section = header.group(1)
    match = re.match(r'^(\s*([^\s=:;#]+)\s*[=:]\s*)([^\r\n]*)(\r?\n)?$', line)
    if match:
        key = (section, match.group(2).lower())
        if key in updates:
            comment = re.search(r'(\s+;.*)$', match.group(3))
            line = (match.group(1) + updates[key] +
                    (comment.group(1) if comment else '') + (match.group(4) or ''))
            found.add(key)
    lines.append(line)
if found != set(updates):
    sys.exit('Role and UPS address must be explicit INI parameters')
if updates:
    metadata = os.stat(target)
    fd, temporary = tempfile.mkstemp(prefix='.upsguard-', dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
            os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(''.join(lines))
        load_config(temporary)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
else:
    load_config(target)
PY

/usr/bin/python3 "$program_file" --config "$config_file" check-config
/usr/bin/python3 "$program_file" --config "$config_file" status
install -d -m 0700 /var/lib/upsguard

if [ "$role" = master ] && [ -e /etc/killpower ]; then
    echo "/etc/killpower exists; investigate it before enabling UPS Guard" >&2
    exit 1
fi

if [ "$role" = master ] && [ -f /etc/nut/upsd.conf ]; then
    python3 - <<'PY'
import sys
with open('/etc/nut/upsd.conf', encoding='utf-8') as source:
    for line in source:
        words = line.partition('#')[0].split()
        if len(words) >= 2 and words[0].upper() == 'MAXAGE':
            try:
                value = int(words[1])
            except ValueError:
                sys.exit('Invalid MAXAGE in /etc/nut/upsd.conf')
            if value > 30:
                sys.exit('MAXAGE in upsd.conf must be 30 seconds or less for the client')
PY
fi

# NUT's upsmon can initiate a UPS load shutdown. UPS Guard targets only the
# local workstation and its display; network equipment remains supplied.
if [ "$role" = master ]; then
    systemctl mask --now nut-monitor.service
fi
# Render absolute paths for this folder, including folders with spaces.
# These templates are not installed verbatim and no /opt or /etc INI copy is made.
/usr/bin/python3 - "$project_dir" <<'PY'
import os
import sys

project_dir = sys.argv[1]
if any(ord(char) < 32 or ord(char) == 127 for char in project_dir):
    sys.exit('The program folder must not contain control characters')

def escape(value, command=False):
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return value.replace('$', '$$') if command else value

values = {
    '@PROJECT_DIR@': escape(project_dir),
    '@PROGRAM@': escape(os.path.join(project_dir, 'upsguard', 'upsguard.py'), command=True),
    '@CONFIG@': escape(os.path.join(project_dir, 'conf', 'upsguard.ini'), command=True),
}
for name in ('upsguard-bootcheck.service', 'upsguard.service'):
    with open(os.path.join(project_dir, 'systemd', name), encoding='utf-8') as stream:
        unit = stream.read()
    # One pass avoids interpreting placeholder-like text in a folder name.
    import re
    unit = re.sub(r'@PROJECT_DIR@|@PROGRAM@|@CONFIG@', lambda match: values[match.group()], unit)
    target = os.path.join('/etc/systemd/system', name)
    with open(target, 'w', encoding='utf-8') as stream:
        stream.write(unit)
    os.chmod(target, 0o644)
PY
systemctl daemon-reload
# The main process now runs the RTC check itself, allowing the desktop to start
# and show the countdown. Disable the old early boot unit when upgrading.
systemctl disable --now upsguard-bootcheck.service
systemctl enable upsguard.service
systemctl stop upsguard.service
systemctl start upsguard.service

echo "Installed on $role in $project_dir. NUT configuration unchanged."
echo "Edit the working INI: $config_file"
echo "Diagnostics from the program folder: sudo python3 upsguard/upsguard.py status"

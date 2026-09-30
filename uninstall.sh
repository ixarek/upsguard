#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "Run: sudo ./uninstall.sh" >&2
    exit 1
fi
systemctl disable --now upsguard.service upsguard-bootcheck.service || true
rm -f /etc/systemd/system/upsguard.service /etc/systemd/system/upsguard-bootcheck.service
systemctl daemon-reload
echo "Services removed; program folder, config, log and state retained."
echo "nut-monitor remains masked to avoid switching off the UPS output."

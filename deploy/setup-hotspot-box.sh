#!/usr/bin/env bash
# Turn a Raspberry Pi (5 recommended) into a Smash Speed box: its own Wi-Fi hotspot,
# and the phone web app on http://10.42.0.1. See docs/pi-hotspot.md.
# Raspberry Pi OS Bookworm (64-bit). Run from the repository root, as your user:
#   bash deploy/setup-hotspot-box.sh "wifi-password"
# Not yet tested on a Pi.
set -euo pipefail

PASS="${1:-}"
if [ "${#PASS}" -lt 8 ]; then
    echo "Give the hotspot a Wi-Fi password of at least 8 characters:"
    echo "  bash deploy/setup-hotspot-box.sh \"wifi-password\""
    exit 1
fi
SSID="${SSID:-SmashSpeed}"

cd "$(dirname "$0")/.."
REPO="$(pwd)"
DATA="$HOME/shuttlecam-web"

if grep -q "Zero 2" /proc/device-tree/model 2>/dev/null; then
    echo "Note: a Pi Zero 2 W takes many minutes per clip (docs/pi-hotspot.md). A Pi 5 is recommended."
fi

echo "== System packages (OpenCV, NumPy, Flask, Waitress, Matplotlib come from apt) =="
sudo apt-get update
sudo apt-get install -y python3-opencv python3-numpy python3-flask python3-waitress python3-matplotlib python3-venv

echo "== Virtual environment that can see the apt packages =="
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-deps -e .
mkdir -p "$DATA"

echo "== Wi-Fi hotspot \"$SSID\" (10.42.0.1), started at boot =="
sudo nmcli connection delete "$SSID" >/dev/null 2>&1 || true
sudo nmcli connection add type wifi ifname wlan0 con-name "$SSID" autoconnect yes ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
    connection.autoconnect-priority 100
sudo nmcli connection up "$SSID"

echo "== The web app on port 80, started at boot =="
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER|g" -e "s|@DATA@|$DATA|g" deploy/shuttlecam-web.service |
    sudo tee /etc/systemd/system/shuttlecam-web.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now shuttlecam-web.service
sleep 3

cat <<EOF

Done. On a phone: join the Wi-Fi "$SSID", open http://10.42.0.1
Passcode: $(cat "$DATA/passcode.txt" 2>/dev/null || echo "see: journalctl -u shuttlecam-web")
Logs:     journalctl -u shuttlecam-web -f
Tuned drag: copy a drag.toml into $DATA and run: sudo systemctl restart shuttlecam-web
EOF

#!/usr/bin/env bash
# Set up shuttlecam on a Raspberry Pi Zero 2 W running Raspberry Pi OS Bookworm (64-bit Lite).
# Run from the repository root:  bash deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

echo "== System packages (Picamera2, OpenCV, NumPy and pyserial come from apt) =="
sudo apt-get update
sudo apt-get install -y python3-picamera2 python3-opencv python3-numpy python3-serial python3-venv

echo "== Virtual environment that can see the apt packages =="
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-deps -e .

echo "== Serial port for the P10 display node =="
# Turn the UART on and the Linux login console on it off, so /dev/serial0 is ours.
sudo raspi-config nonint do_serial_hw 0
sudo raspi-config nonint do_serial_cons 1

if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    sed -i 's/^backend = "console"/backend = "serial"/' config.toml
    echo "Wrote config.toml (display backend: serial)."
fi

echo "== Start on boot =="
sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER|g" deploy/shuttlecam.service |
    sudo tee /etc/systemd/system/shuttlecam.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable shuttlecam.service

cat <<EOF

Done. Next:
  1. Reboot, so the serial port settings apply.
  2. Calibrate (see docs/calibration.md):
       .venv/bin/shuttlecam snapshot -o frame.png
       .venv/bin/shuttlecam calibrate frame.png --world "0,0 0,2 1.98,0 1.98,2" --points "..."
  3. sudo systemctl start shuttlecam    (logs: journalctl -u shuttlecam -f)
EOF

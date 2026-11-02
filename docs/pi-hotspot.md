# The Smash Speed box: a Pi with its own hotspot (planned)

> **Status: design and set-up scripts, not yet run on a Pi.** The web app itself is
> tested ([web app](web-app.md)); what's new here is running it on a Raspberry Pi
> that makes its own Wi-Fi network, so nothing at the court needs the internet or
> a PC at home.

```
phone ──Wi-Fi "SmashSpeed"──> Pi 5: hotspot + shuttlecam serve ──> result on the phone
                                    └──(optional) UART──> ESP32 + P10 panels: "234 KM/H"
```

At the court: switch the box on, join the **SmashSpeed** Wi-Fi on the phone, open
**http://10.42.0.1**, and upload clips exactly as with the PC version: mark the
court once per camera spot, upload, get the speed.

## Which Pi

The 3D analysis runs on one CPU core: about **67 s per 2 s clip** on a laptop i5
(8th gen), most of it fitting flights in Python. Scaling by single-core speed:

| Board | Per clip (estimate) | Verdict |
|---|---|---|
| **Raspberry Pi 5, 4 or 8 GB** | ~1.5–2 min | the one to use |
| Raspberry Pi 4, 4 GB | ~3–4 min | workable |
| Raspberry Pi Zero 2 W (the v2 board) | 8 min or more; 512 MB is tight for 1080p | too slow |

Measure on the real board before relying on these. Faster options if needed:
measure at 960 px wide (the pixel settings already scale with width), and run
two clips at once on the Pi 5's four cores (the worker runs one at a time today).

Parts: Pi 5 (4 GB+), its 27 W USB-C supply or a USB-C power bank that can
deliver 5 V / 5 A (a 3 A bank works if nothing else is plugged in), a 32 GB+
microSD card, a case with a fan or heatsink (the fits keep a core busy for
minutes). The phone on its 2 m tripod is the camera.

## Set-up (on Raspberry Pi OS Bookworm, 64-bit)

```bash
git clone https://github.com/RyanEngineering2/shuttle-speed-cam.git
cd shuttle-speed-cam
bash deploy/setup-hotspot-box.sh "your-wifi-password"
```

The script:

1. installs the app with its web extras into `.venv` (as `deploy/install.sh` does);
2. creates a NetworkManager hotspot called **SmashSpeed** (WPA2, the password you
   give) that starts at boot; the Pi is **10.42.0.1** on it;
3. installs `deploy/shuttlecam-web.service`: `shuttlecam serve --host 0.0.0.0
   --port 80` with its data in `~/shuttlecam-web`, started at boot, restarted if
   it stops.

The site's passcode is printed in `journalctl -u shuttlecam-web` (and kept in
`~/shuttlecam-web/passcode.txt`). Copy a `drag.toml` into that folder to use a
tuned shuttle drag; the service passes it with `-c` when it's there.

Notes:

- The hotspot has no internet. iPhones stay connected; on Android, choose "keep
  Wi-Fi connection" when it warns, or it may switch to mobile data.
- Plain HTTP is fine here: the Wi-Fi itself is WPA2-protected and the site still
  needs the passcode. Tailscale isn't needed.
- A 2 s 1080p60 clip is about 6 MB: a few seconds to upload over the hotspot.
- Turning the box off mid-measurement is safe: queued and unfinished clips are
  picked up again at the next start.

## Showing the result on the P10 board (later)

The v2 hardware already has a speed display: an ESP32 driving two P10 LED panels,
listening for `SPEED <kmh> <best>` lines on a UART ([hardware](hardware.md)). The
box can keep it: wire the Pi's UART to the ESP32 as before, and have the web
app send each new measured speed to it, so the whole hall sees the number when
the phone does. That's a small addition to the worker (it already writes the
result), not done yet.

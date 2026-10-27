# v1: everything on an ESP32-CAM (retired)

The first version of the project ran the whole pipeline on an AI-Thinker
ESP32-CAM with its OV2640 camera: motion detection, blob finding and a
constant-velocity Kalman filter. It sent `SPEED <kmh> <best>` lines from its
serial TX pin to the same [P10 display node](../p10-display) the current version
uses.

## Why it was replaced

**Frame rate.** The ESP32 + OV2640 manage about 30 fps at 320×240 in grayscale.
A shuttle loses speed so fast that this is not enough:

- Between two frames at 30 fps, a 250 km/h smash travels almost 2 m and slows to
  about 166 km/h.
- The camera's first look comes anywhere from 0 to 33 ms after the hit, so the
  same smash can read anywhere between ~165 and 250 km/h.
- A fast smash crosses the view in four or five frames, sometimes too few to
  confirm a track at all.

**The camera.** The OV2640 is a low-cost sensor. Indoors it needs a long exposure,
which smears a fast shuttle into a faint streak.

**No room for a better filter.** A drag-aware extended Kalman filter with a
smoother, which is what fixes the underestimate (see
[how it works](../../docs/how-it-works.md)), is awkward to fit next to the
camera driver on a single ESP32.

v2 moved the vision work to a Raspberry Pi Zero 2 W with a 200 fps camera, and
kept an ESP32 for what it's good at: driving the LED panels.

## If you want to try it anyway

```bash
pio run -d firmware/esp32-cam-v1 -t upload    # via a USB-serial adapter, GPIO0 to GND to flash
```

Set `METRES_PER_PIXEL` in `src/main.cpp` for your camera position first. Connect
the ESP32-CAM's U0TXD (GPIO1) to the display node's GPIO16 and share GND. Lines
starting with `#` are debug output, including the frame rate every 5 s, and the
display node ignores them.

# Calibration

Calibration tells shuttlecam how pixels map to metres on the plane the shuttle
flies in: the court's centre line, seen side-on. You need four or more points on
that plane whose positions you know. Calibrate again whenever the camera moves.

## 1. Mark known points

The easy way uses a 2 m pole and the court markings. On the court's centre line:

| Point | Where | World position (m) |
|---|---|---|
| 1 | Pole bottom, under the net | `0,0` |
| 2 | Pole top, under the net | `0,2` |
| 3 | Pole bottom, at the short service line (1.98 m from the net) | `1.98,0` |
| 4 | Pole top, at the short service line | `1.98,2` |

x runs along the court toward the side the camera's smashes fly into, and y is
height. Any other points on the plane work too: more points and points spread
across the image both give a better fit. For example, add the pole at the back
boundary line, 6.70 m from the net.

## 2. Take a snapshot

On the Pi:

```bash
.venv/bin/shuttlecam snapshot -o frame.png
```

The frame is saved at processing resolution. Calibrations are stored with their
image size and rescaled automatically, so you can also calibrate on a
full-resolution photo from the same camera position.

If you only have one pole, take one snapshot per pole position (`-o net.png`,
`-o service.png`) and read the pixel coordinates from each in any image viewer
that shows the cursor position.

## 3. Build the calibration

With a desktop, click the points in order:

```bash
shuttlecam calibrate frame.png --world "0,0 0,2 1.98,0 1.98,2"
```

Headless, or with pixel coordinates you already read off:

```bash
shuttlecam calibrate frame.png --world "0,0 0,2 1.98,0 1.98,2" \
    --points "102,400 99,240 330,410 335,248"
```

It writes `calibration.json` and prints where each clicked point lands in
metres. With exactly four points the fit is exact. With more, the printout shows
how well they agree. A few centimetres is fine.

## 4. Check it

Hold the pole at a third spot on the centre line, take a snapshot, and work out
its height from the calibration:

```bash
python -c "from shuttlecam.calibration import Calibration as C; c = C.load('calibration.json'); print(c.to_world(210, 405), c.to_world(212, 247))"
```

The two points should come out about 2 m apart. If not, re-click or add points.

## Faster smashes, wider shots

If you change `camera.size` or `camera.process_width`, the saved calibration
still works, because it rescales to the new frame size. If you move the camera
or change the lens or zoom, calibrate again.

"""Regenerate the figures in docs/images from the simulator.

    python tools/make_figures.py

Everything here is simulated: the point is to show what the algorithm does with a
known ground truth, not to claim field results.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
from matplotlib.figure import Figure

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shuttlecam.config import Config
from shuttlecam.pipeline import Pipeline, analyze
from shuttlecam.plot import plot_shot
from shuttlecam.sim import SmashScene

OUT = ROOT / "docs" / "images"


def simulated_smash() -> None:
    scene = SmashScene(speed_kmh=250, fps=200)
    (shot,) = analyze(scene.frames(), Config(), scene.calibration())
    plot_shot(
        shot,
        str(OUT / "simulated-smash.png"),
        truth=scene.truth,
        title=f"Simulated 250 km/h smash at 200 fps: reported {shot.speed_kmh:.0f} km/h",
    )
    print(f"simulated-smash.png: reported {shot.speed_kmh:.1f} km/h")


def frame_rate() -> None:
    """Reported speed vs frame rate for a 250 km/h smash, for three methods."""
    speed = 250
    rates = [30, 45, 60, 90, 120, 160, 200]
    cv_cfg = Config()
    cv_cfg.tracker.model = "cv"
    cv_cfg.tracker.accel_noise = 500.0
    rows = {"drag": [], "cv": [], "raw": [], "seen": []}
    for fps in rates:
        # Average over where the hit falls between two frames.
        acc = {k: [] for k in rows}
        for k in range(4):
            scene = SmashScene(speed_kmh=speed, fps=fps, t_hit=0.08 + k / fps / 4, seed=k + 1)
            frames = list(scene.frames())
            cal = scene.calibration()
            drag = analyze(frames, Config(), cal)
            cv_ = analyze(frames, cv_cfg, cal)
            if drag:
                shot = max(drag, key=lambda s: s.speed_kmh)
                acc["drag"].append(shot.speed_kmh)
                acc["raw"].append(shot.raw_speed_kmh)
                first_frame = math.ceil(scene.t_hit * fps - 1e-9) / fps
                acc["seen"].append(scene.true_speed_kmh(first_frame))
            if cv_:
                acc["cv"].append(max(s.speed_kmh for s in cv_))
        for key, values in acc.items():
            rows[key].append(np.mean(values) if values else np.nan)
        print(f"  {fps:3d} fps: " + "  ".join(f"{k} {rows[k][-1]:.0f}" for k in rows))

    fig = Figure(figsize=(7.5, 4.4), dpi=150, layout="constrained")
    ax = fig.subplots()
    ax.axhline(speed, color="0.6", lw=3, label=f"true launch speed ({speed} km/h)")
    ax.plot(rates, rows["seen"], ":", color="0.4", label="true speed at the first frame after the hit")
    ax.plot(rates, rows["drag"], "o-", color="#00798c", lw=2, label="drag EKF + smoother (this project)")
    ax.plot(rates, rows["cv"], "s--", color="#edae49", label="constant-velocity Kalman filter")
    ax.plot(rates, rows["raw"], "^--", color="#d1495b", label="frame-to-frame distance / time")
    ax.axvspan(25, 35, color="#d1495b", alpha=0.08)
    ax.annotate("ESP32-CAM\n(~30 fps)", (30, 108), ha="center", fontsize=8, color="#d1495b")
    ax.axvspan(190, 210, color="#00798c", alpha=0.08)
    ax.annotate("Pi Zero 2 W\n(200 fps)", (200, 108), ha="center", fontsize=8, color="#00798c")
    ax.set_xlabel("camera frame rate (fps)")
    ax.set_ylabel("reported speed (km/h)")
    ax.set_ylim(100, 270)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="lower center")
    ax.set_title("Same smash, different frame rates (simulated)")
    fig.savefig(OUT / "frame-rate.png")
    print("frame-rate.png")


def camera_view() -> None:
    """What the camera sees: one frame, plus the whole flight stacked on top of it."""
    scene = SmashScene(speed_kmh=250, fps=200)
    frames = list(scene.frames())
    pipeline = Pipeline(Config(), scene.calibration())
    shots = []
    for f in frames:
        shots += pipeline.process(f)
    shots += pipeline.flush()
    (shot,) = shots

    base = frames[0].image.astype(np.float32)
    stack = base.copy()
    for f in frames:
        stack = np.maximum(stack, f.image.astype(np.float32))
    img = cv2.cvtColor(stack.clip(0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)
    scale = 3
    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)

    cal = scene.calibration()
    pts = [cal.to_image(x, y) * scale for _, x, y in shot.measurements]
    smooth = np.array([cal.to_image(s[1], s[2]) * scale for s in shot.smoothed], np.int32)
    cv2.polylines(img, [smooth.reshape(-1, 1, 2)], False, (140, 121, 0), 2, cv2.LINE_AA)
    for u, v in pts[::2]:
        cv2.circle(img, (int(u), int(v)), 5, (91, 73, 209), 2, cv2.LINE_AA)

    def label(text, org, size, color):
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, size, (0, 0, 0), 5, cv2.LINE_AA)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, size, color, 2, cv2.LINE_AA)

    w = img.shape[1]
    label(f"{shot.speed_kmh:.0f} km/h", (w - 250, 60), 1.4, (255, 255, 255))
    label("simulated, every frame stacked", (16, img.shape[0] - 16), 0.6, (220, 220, 220))
    label("shuttle comes in", (int(pts[0][0]) + 30, int(pts[0][1]) - 45), 0.6, (220, 220, 220))
    label("smash", (int(pts[6][0]), int(pts[6][1]) + 40), 0.6, (220, 220, 220))
    label("player", (int(0.04 * w), int(0.42 * img.shape[0])), 0.6, (220, 220, 220))
    cv2.imwrite(str(OUT / "camera-view.png"), img)
    print("camera-view.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    simulated_smash()
    camera_view()
    frame_rate()

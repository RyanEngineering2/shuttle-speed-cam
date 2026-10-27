"""Figures of a shot: where the shuttle went and how fast. Needs matplotlib."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

from .tracker import Shot


def plot_shot(
    shot: Shot,
    path: str,
    truth: Callable[[float], np.ndarray] | None = None,
    title: str | None = None,
) -> None:
    """Save a two-panel figure: trajectory (left) and speed over time (right).

    `truth`, if given, maps a time to the true [x, y, vx, vy] (from the simulator).
    """
    from matplotlib.figure import Figure  # no pyplot, so no GUI backend needed

    fig = Figure(figsize=(11, 4.2), dpi=150, layout="constrained")
    ax_path, ax_speed = fig.subplots(1, 2, width_ratios=[1.25, 1])

    mt = np.array([m[0] for m in shot.measurements])
    mx = np.array([m[1] for m in shot.measurements])
    my = np.array([m[2] for m in shot.measurements])
    st = np.array([s[0] for s in shot.smoothed])
    sx = np.array([s[1] for s in shot.smoothed])
    sy = np.array([s[2] for s in shot.smoothed])
    sv = np.array([math.hypot(s[3], s[4]) * 3.6 for s in shot.smoothed])
    t0 = shot.t_start

    tt = np.linspace(t0 - 0.004, shot.t_end, 300)
    if truth is not None:
        true = np.array([truth(t) for t in tt])
        ax_path.plot(true[:, 0], true[:, 1], color="0.75", lw=4, label="true path", zorder=1)
    ax_path.plot(mx, my, "o", ms=3.5, color="#d1495b", label="detections", zorder=3)
    ax_path.plot(sx, sy, "-", lw=1.4, color="#00798c", label="Kalman smoothed", zorder=2)
    ax_path.set_xlabel("along the court (m)")
    ax_path.set_ylabel("height (m)")
    ax_path.set_aspect("equal", adjustable="datalim")
    ax_path.grid(alpha=0.3)
    ax_path.legend(loc="upper right", fontsize=8)

    ms = lambda t: (np.asarray(t) - t0) * 1000
    if truth is not None:
        ax_speed.plot(ms(tt), np.hypot(true[:, 2], true[:, 3]) * 3.6, color="0.75", lw=4, label="true speed")
    raw_t = (mt[1:] + mt[:-1]) / 2
    raw_v = np.hypot(np.diff(mx), np.diff(my)) / np.diff(mt) * 3.6
    ax_speed.plot(ms(raw_t), raw_v, ".", color="#d1495b", label="frame-to-frame")
    ax_speed.plot(ms(st), sv, "-", lw=1.6, color="#00798c", label="Kalman smoothed")
    ax_speed.axhline(shot.speed_kmh, color="#00798c", lw=0.8, ls="--")
    ax_speed.annotate(
        f"reported {shot.speed_kmh:.0f} km/h",
        (ms(st[0]), shot.speed_kmh),
        xytext=(8, 6),
        textcoords="offset points",
        color="#00798c",
        fontsize=9,
    )
    top = max([shot.speed_kmh, *raw_v])
    ax_speed.set_ylim(0, top * 1.18)
    ax_speed.set_xlabel("time since first detection (ms)")
    ax_speed.set_ylabel("speed (km/h)")
    ax_speed.grid(alpha=0.3)
    ax_speed.legend(loc="upper right", fontsize=8)

    if title:
        fig.suptitle(title)
    fig.savefig(path)

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


def plot_flight3d(fit, camera, path: str, image=None, title: str | None = None, truth=None) -> None:
    """Save a figure of a 3D shot: the image with the fitted path, the court from
    above, and speed over time.

    `fit` is a fit3d.FlightFit and `camera` the CameraModel it was fitted with.
    `image` (optional) is a frame from the video, RGB or grayscale, at the camera's
    resolution. `truth` (optional) maps a time to the true state, for simulations.
    """
    from matplotlib.figure import Figure

    from .court import ACROSS, ALONG, HALF_LENGTH, HALF_WIDTH, NET_POST_HEIGHT, segments

    obs = fit.observations
    t_end = max(o.t for o in obs)
    if fit.landing_time is not None:
        t_end = max(t_end, fit.landing_time)
    times = np.linspace(fit.contact_time, t_end, 300)
    states = fit.states(times)
    w, h = camera.image_size

    fig = Figure(figsize=(14, 7), dpi=110, layout="constrained")
    gs = fig.add_gridspec(2, 2, width_ratios=[2.0, 1])

    ax = fig.add_subplot(gs[:, 0])
    if image is not None:
        ax.imshow(image, cmap="gray" if image.ndim == 2 else None, extent=(0, w, h, 0))
    else:
        ax.set_facecolor("#1d2330")
    for a, b in segments():
        if min(camera.depth([a, b])) > 0:
            (u1, v1), (u2, v2) = camera.project([a, b])
            ax.plot([u1, u2], [v1, v2], color="white", lw=0.6, alpha=0.5)
    uv = camera.project(states[:, :3])
    ax.plot(uv[:, 0], uv[:, 1], "-", color="#00c2ff", lw=1.8, label="fitted 3D flight, projected")
    ax.plot(
        [o.u for o in obs],
        [o.v for o in obs],
        "o",
        ms=3.5,
        mfc="none",
        color="#ff4d6d",
        label="tracked shuttle",
    )
    cu, cv = camera.project([fit.contact])[0]
    ax.plot(cu, cv, "x", color="yellow", ms=10, mew=2, label="estimated hit")
    pad = 120
    ax.set_xlim(max(0, min(uv[:, 0].min(), cu) - pad), min(w, max(uv[:, 0].max(), cu) + pad))
    ax.set_ylim(min(h, max(uv[:, 1].max(), cv) + pad), max(0, min(uv[:, 1].min(), cv) - pad))
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(loc="lower left", fontsize=8)
    if title:
        ax.set_title(title)

    top = fig.add_subplot(gs[0, 1])
    for y in ACROSS.values():
        top.plot([-HALF_WIDTH, HALF_WIDTH], [y, y], color="0.6", lw=0.8)
    for name, x in ALONG.items():
        if name == "centre":
            top.plot([0, 0], [-HALF_LENGTH, ACROSS["near_short"]], color="0.6", lw=0.8)
            top.plot([0, 0], [ACROSS["far_short"], HALF_LENGTH], color="0.6", lw=0.8)
        else:
            top.plot([x, x], [-HALF_LENGTH, HALF_LENGTH], color="0.6", lw=0.8)
    top.plot([-HALF_WIDTH - 0.3, HALF_WIDTH + 0.3], [0, 0], color="0.2", lw=2)
    top.plot(states[:, 0], states[:, 1], color="#00c2ff", lw=2)
    top.plot(fit.contact[0], fit.contact[1], "x", color="k", label=f"hit, {fit.contact[2]:.1f} m high")
    if fit.landing is not None:
        top.plot(fit.landing[0], fit.landing[1], "o", color="#ff4d6d", label="lands")
    if truth is not None:
        tt = np.array([truth(t) for t in times])
        top.plot(tt[:, 0], tt[:, 1], color="0.75", lw=4, zorder=0, label="true path")
    top.set_aspect("equal")
    top.set_xlim(-3.8, 3.8)
    y_extent = max(HALF_LENGTH + 0.5, *(abs(y) + 0.5 for y in states[:, 1]))
    top.set_ylim(-y_extent, y_extent)
    top.set_xticks([])
    top.set_yticks([])
    top.set_title(f"From above (camera at the bottom; net {NET_POST_HEIGHT} m)", fontsize=9)
    top.legend(fontsize=7, loc="lower left")

    sp = fig.add_subplot(gs[1, 1])
    ms = (times - fit.contact_time) * 1000
    speed = np.linalg.norm(states[:, 3:], axis=1) * 3.6
    if truth is not None:
        sp.plot(
            ms, [np.linalg.norm(truth(t)[3:]) * 3.6 for t in times], color="0.75", lw=4, label="true speed"
        )
    sp.plot(ms, speed, color="#00c2ff", lw=2, label="fitted")
    seen = (fit.first_seen_time - fit.contact_time) * 1000
    sp.axvspan(0, seen, color="0.88", label="not seen (hit, racket)")
    lo, hi = fit.speed_range_kmh
    sp.errorbar(
        [0], [fit.speed_kmh], yerr=[[fit.speed_kmh - lo], [hi - fit.speed_kmh]], fmt="o", color="k", capsize=4
    )
    sp.annotate(
        f"{fit.speed_kmh:.0f} km/h ({lo:.0f}-{hi:.0f})",
        (0, fit.speed_kmh),
        xytext=(12, -4),
        textcoords="offset points",
        fontsize=9,
    )
    sp.annotate(
        f"{fit.first_seen_kmh:.0f} km/h first tracked",
        (seen, fit.first_seen_kmh),
        xytext=(14, 6),
        textcoords="offset points",
        fontsize=9,
        arrowprops={"arrowstyle": "->", "color": "0.4"},
    )
    sp.set_xlabel("ms after the hit")
    sp.set_ylabel("km/h")
    sp.set_ylim(0, max(hi, speed.max()) * 1.15)
    sp.grid(alpha=0.3)
    sp.legend(fontsize=7, loc="upper right")
    fig.savefig(path)

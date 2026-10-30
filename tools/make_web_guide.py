"""Draw the camera set-up guide shown in the web app, to scale.

    python tools/make_web_guide.py

Writes three SVGs into shuttlecam/web/static/: a side view (distance, height, what
the phone sees, a smash), a top view, and what the phone screen should show. The
numbers come from the court model and the recommended set-up below, so the
pictures stay true if those change. Colours are fixed (each picture has its own
background), so they read the same in light and dark mode.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shuttlecam.court import HALF_LENGTH, HALF_WIDTH, NET_POST_HEIGHT, segments
from shuttlecam.sim import Smash3DScene, look_at_camera

OUT = ROOT / "shuttlecam" / "web" / "static"

# The recommended set-up (see docs/web-app.md for how these were chosen).
PHONE_HEIGHT = 2.0
BACK_1X = 6.2  # metres behind the baseline, normal 1x camera (~70 x 42 degrees)
BACK_05X = 3.5  # with the 0.5x ultra-wide (~100 x 65 degrees)
HFOV_1X, VFOV_1X = 70.0, 42.0
TILT = 1.0  # degrees up: at 2 m high, practically level
KEEP_IN_VIEW = 4.5  # m: the shuttle coming down onto a smasher at the baseline
PHONE = (0.4, -HALF_LENGTH - BACK_1X, PHONE_HEIGHT)
AIM = (0.4, 0.0, PHONE_HEIGHT + (HALF_LENGTH + BACK_1X) * math.tan(math.radians(TILT)))

BG = "#0e1f17"
FLOOR = "#3f4a44"
COURT = "#15803d"
LINE = "#ffffff"
TEXT = "#e5efe8"
MUTED = "#9fb3a7"
VIEW = "#facc15"
SMASH = "#fb7185"
PHONE_C = "#93c5fd"
FONT = 'font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif"'
FONT_SCALE = 1.35  # the pictures show about 320 px wide on a phone: keep labels readable


def smash_path(contact=(0.4, -4.5, 2.8), heading=4.0) -> np.ndarray:
    """A 320 km/h smash from the near back court, away from the camera, to its landing."""
    scene = Smash3DScene(speed_kmh=320, contact=contact, heading_deg=heading, elevation_deg=-10.0)
    t_land = scene.landing[0]
    ts = np.linspace(scene.t_hit, t_land, 60)
    return np.array([scene.truth(t)[:3] for t in ts])


def text(x, y, s, size=9, color=TEXT, anchor="start", weight="400") -> str:
    size *= FONT_SCALE
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" text-anchor="{anchor}" '
        f'font-weight="{weight}" {FONT}>{s}</text>'
    )


def svg(width, height, body, label) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{label}">\n'
        f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>\n' + "\n".join(body) + "\n</svg>\n"
    )


def side_view() -> str:
    s, left, ground = 15.0, -14.4, 132.0
    X = lambda y: 14 + (y - left) * s
    Z = lambda z: ground - z * s
    W, H = 360, 172
    b = []
    py, pz = PHONE[1], PHONE[2]
    # What the 1x camera sees: a wedge from the phone.
    half = VFOV_1X / 2
    top = math.radians(TILT + half)
    bot = math.radians(TILT - half)
    floor_hit = py + pz / math.tan(-bot)
    z_max = (ground - 6) / s
    top_y = py + (z_max - pz) / math.tan(top)
    right = (W - 8 - 14) / s + left
    b.append(
        f'<polygon points="{X(py):.1f},{Z(pz):.1f} {X(floor_hit):.1f},{Z(0):.1f} {X(right):.1f},{Z(0):.1f} '
        f'{X(right):.1f},{Z(z_max):.1f} {X(top_y):.1f},{Z(z_max):.1f}" fill="{VIEW}" fill-opacity=".16"/>'
    )
    for y2, z2 in ((floor_hit, 0.0), (top_y, z_max)):
        b.append(
            f'<line x1="{X(py):.1f}" y1="{Z(pz):.1f}" x2="{X(y2):.1f}" y2="{Z(z2):.1f}" stroke="{VIEW}" '
            f'stroke-width="1.2" stroke-dasharray="4 3"/>'
        )
    # Floor, court, net.
    b.append(
        f'<line x1="{X(left):.1f}" y1="{Z(0)}" x2="{X(right):.1f}" y2="{Z(0)}" stroke="{FLOOR}" stroke-width="3"/>'
    )
    b.append(
        f'<line x1="{X(-HALF_LENGTH):.1f}" y1="{Z(0)}" x2="{X(HALF_LENGTH):.1f}" y2="{Z(0)}" stroke="{COURT}" '
        f'stroke-width="5"/>'
    )
    b.append(
        f'<line x1="{X(0):.1f}" y1="{Z(0)}" x2="{X(0):.1f}" y2="{Z(NET_POST_HEIGHT):.1f}" stroke="{LINE}" stroke-width="2"/>'
    )
    b.append(
        f'<rect x="{X(0) - 1.5:.1f}" y="{Z(1.55):.1f}" width="3" height="{0.76 * s:.1f}" fill="{LINE}" opacity=".5"/>'
    )
    for y, name in ((-HALF_LENGTH, "your baseline"), (HALF_LENGTH, "far baseline")):
        b.append(
            f'<line x1="{X(y):.1f}" y1="{Z(0) - 4}" x2="{X(y):.1f}" y2="{Z(0) + 4}" stroke="{LINE}" stroke-width="1.5"/>'
        )
        b.append(text(X(y), Z(0) + 17, name, 8, MUTED, "middle"))
    b.append(text(X(0), Z(0) + 17, "net", 8, MUTED, "middle"))
    # The smasher and the smash.
    path = smash_path()
    hy, hz = path[0][1], path[0][2]
    head = (hy - 0.25, 1.72)
    b.append(f'<circle cx="{X(head[0]):.1f}" cy="{Z(head[1]):.1f}" r="3.2" fill="{TEXT}"/>')
    b.append(
        f'<path d="M{X(head[0]):.1f} {Z(1.55):.1f} L{X(head[0]):.1f} {Z(0.95):.1f} M{X(head[0]):.1f} {Z(0.95):.1f} '
        f"L{X(head[0] - 0.3):.1f} {Z(0):.1f} M{X(head[0]):.1f} {Z(0.95):.1f} L{X(head[0] + 0.3):.1f} {Z(0):.1f} "
        f'M{X(head[0]):.1f} {Z(1.45):.1f} L{X(hy):.1f} {Z(hz):.1f}" stroke="{TEXT}" stroke-width="1.6" fill="none" '
        f'stroke-linecap="round"/>'
    )
    # The lift coming down onto the hit (drawn, not simulated: only its end matters).
    b.append(
        f'<path d="M{X(hy + 3.2):.1f} {Z(z_max):.1f} Q{X(hy + 0.6):.1f} {Z(5.6):.1f} {X(hy):.1f} {Z(hz):.1f}" '
        f'stroke="{MUTED}" stroke-width="1.3" fill="none" stroke-dasharray="3 3"/>'
    )
    pts = " ".join(f"{X(p[1]):.1f},{Z(p[2]):.1f}" for p in path)
    b.append(f'<polyline points="{pts}" fill="none" stroke="{SMASH}" stroke-width="2"/>')
    end = path[-1]
    b.append(f'<circle cx="{X(end[1]):.1f}" cy="{Z(0):.1f}" r="2.5" fill="{SMASH}"/>')
    b.append(text(X(hy) + 6, Z(hz) - 4, "smash", 8.5, SMASH, "start", "600"))
    # Keep-in-view height over your back court.
    ky = -HALF_LENGTH + 0.7
    b.append(
        f'<line x1="{X(ky) - 10:.1f}" y1="{Z(KEEP_IN_VIEW):.1f}" x2="{X(ky) + 10:.1f}" y2="{Z(KEEP_IN_VIEW):.1f}" '
        f'stroke="{VIEW}" stroke-width="1.5"/>'
    )
    b.append(text(X(ky) - 13, Z(KEEP_IN_VIEW) + 3, f"{KEEP_IN_VIEW:g} m up: still in view", 8, VIEW, "end"))
    # The phone on its stand, and the 0.5x position.
    for y, solid in ((py, True), (-HALF_LENGTH - BACK_05X, False)):
        dash = "" if solid else ' stroke-dasharray="2 2"'
        col = PHONE_C if solid else MUTED
        b.append(
            f'<path d="M{X(y):.1f} {Z(pz - 0.1):.1f} L{X(y - 0.5):.1f} {Z(0):.1f} M{X(y):.1f} {Z(pz - 0.1):.1f} '
            f'L{X(y + 0.5):.1f} {Z(0):.1f} M{X(y):.1f} {Z(pz - 0.1):.1f} L{X(y):.1f} {Z(0):.1f}" '
            f'stroke="{col}" stroke-width="1.2" fill="none"{dash}/>'
        )
        b.append(
            f'<rect x="{X(y) - 2:.1f}" y="{Z(pz) - 5:.1f}" width="4" height="10" rx="1" fill="{col if solid else BG}" '
            f'stroke="{col}"{dash}/>'
        )
    b.append(text(X(-HALF_LENGTH - BACK_05X) - 5, Z(pz) + 4, "0.5×", 8, MUTED, "end"))
    b.append(text(X(py), Z(pz) - 9, "1×", 8.5, PHONE_C, "middle", "700"))
    # Height and distance.
    hx = X(py) - 10
    b.append(
        f'<line x1="{hx:.1f}" y1="{Z(0):.1f}" x2="{hx:.1f}" y2="{Z(pz):.1f}" stroke="{PHONE_C}" stroke-width="1"/>'
    )
    b.append(text(hx - 3, Z(pz / 2) + 3, f"{PHONE_HEIGHT:g} m", 8.5, PHONE_C, "end", "700"))
    dy = Z(0) + 33
    b.append(
        f'<path d="M{X(py):.1f} {dy:.1f} L{X(-HALF_LENGTH):.1f} {dy:.1f} M{X(py):.1f} {dy - 3:.1f} v6 '
        f'M{X(-HALF_LENGTH):.1f} {dy - 3:.1f} v6" stroke="{PHONE_C}" stroke-width="1"/>'
    )
    b.append(
        text(
            (X(py) + X(-HALF_LENGTH)) / 2,
            dy - 4,
            f"{BACK_1X:.0f} m+ (1×) · {BACK_05X:g} m+ (0.5×)",
            8,
            PHONE_C,
            "middle",
        )
    )
    b.append(text(X(-HALF_LENGTH - BACK_05X + 0.9), Z(pz) + 3, "→ aim level", 8, VIEW))
    b.append(text(W - 12, 18, "SIDE VIEW", 7, MUTED, "end", "700"))
    return svg(W, H, b, "Side view: phone 2 m high, 6 m behind your baseline, aimed level")


def top_view() -> str:
    s = 10.0
    X = lambda x: 100 + x * s
    Y = lambda y: 16 + (7.4 - y) * s
    W, H = 200, 262
    b = []
    px, py = PHONE[0], PHONE[1]
    # What the 1x camera sees (clipped by the picture's edges).
    far = 30.0
    half = math.radians(HFOV_1X / 2)
    b.append(
        f'<polygon points="{X(px):.1f},{Y(py):.1f} {X(px - far * math.sin(half)):.1f},{Y(py + far * math.cos(half)):.1f} '
        f'{X(px + far * math.sin(half)):.1f},{Y(py + far * math.cos(half)):.1f}" fill="{VIEW}" fill-opacity=".14"/>'
    )
    for sgn in (-1, 1):
        b.append(
            f'<line x1="{X(px):.1f}" y1="{Y(py):.1f}" x2="{X(px + sgn * far * math.sin(half)):.1f}" '
            f'y2="{Y(py + far * math.cos(half)):.1f}" stroke="{VIEW}" stroke-width="1.2" stroke-dasharray="4 3"/>'
        )
    b.append(
        f'<rect x="{X(-HALF_WIDTH):.1f}" y="{Y(HALF_LENGTH):.1f}" width="{2 * HALF_WIDTH * s:.1f}" '
        f'height="{2 * HALF_LENGTH * s:.1f}" fill="{COURT}"/>'
    )
    for a, c in segments():
        if a[2] > 0 or c[2] > 0:
            continue
        b.append(
            f'<line x1="{X(a[0]):.1f}" y1="{Y(a[1]):.1f}" x2="{X(c[0]):.1f}" y2="{Y(c[1]):.1f}" stroke="{LINE}" '
            f'stroke-width=".9"/>'
        )
    b.append(
        f'<line x1="{X(-HALF_WIDTH - 0.3):.1f}" y1="{Y(0):.1f}" x2="{X(HALF_WIDTH + 0.3):.1f}" y2="{Y(0):.1f}" '
        f'stroke="{LINE}" stroke-width="2" stroke-dasharray="3 2"/>'
    )
    path = smash_path()
    pts = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in path)
    b.append(f'<polyline points="{pts}" fill="none" stroke="{SMASH}" stroke-width="2.2"/>')
    end = path[-1]
    b.append(f'<circle cx="{X(end[0]):.1f}" cy="{Y(end[1]):.1f}" r="2.8" fill="{SMASH}"/>')
    b.append(f'<circle cx="{X(path[0][0]):.1f}" cy="{Y(path[0][1]):.1f}" r="4" fill="{TEXT}"/>')
    b.append(text(X(path[0][0]) + 7, Y(path[0][1]) + 3, "smasher", 8.5, TEXT, "start", "600"))
    b.append(
        f'<rect x="{X(px) - 5:.1f}" y="{Y(py) - 2.5:.1f}" width="10" height="5" rx="1" fill="{PHONE_C}"/>'
    )
    b.append(text(X(px), Y(py) + 16, "phone, behind the smasher", 8, PHONE_C, "middle", "700"))
    b.append(text(X(px), Y(py) + 30, "both sidelines in view", 8, VIEW, "middle"))
    b.append(text(W - 8, 14, "FROM ABOVE", 7, MUTED, "end", "700"))
    return svg(W, H, b, "From above: the phone behind the smasher, near the middle, seeing both sidelines")


def frame_view() -> str:
    W, H = 320, 180
    cam = look_at_camera(PHONE, AIM, 960 / math.tan(math.radians(HFOV_1X / 2)) * W / 1920, (W, H))

    def poly(points3, fill, extra=""):
        uv = cam.project(np.array(points3, float))
        pts = " ".join(f"{u:.1f},{v:.1f}" for u, v in uv)
        return f'<polygon points="{pts}" fill="{fill}"{extra}/>'

    b = [f'<rect width="{W}" height="{H}" rx="10" fill="#22313a"/>']
    b.append(poly([(-30, -7.6, 0), (30, -7.6, 0), (30, 60, 0), (-30, 60, 0)], FLOOR))
    b.append(
        poly(
            [
                (-HALF_WIDTH, -HALF_LENGTH, 0),
                (HALF_WIDTH, -HALF_LENGTH, 0),
                (HALF_WIDTH, HALF_LENGTH, 0),
                (-HALF_WIDTH, HALF_LENGTH, 0),
            ],
            COURT,
        )
    )
    for a, c in segments():
        (u1, v1), (u2, v2) = cam.project(np.array([a, c], float))
        b.append(
            f'<line x1="{u1:.1f}" y1="{v1:.1f}" x2="{u2:.1f}" y2="{v2:.1f}" stroke="{LINE}" stroke-width=".9"/>'
        )
    b.append(
        poly(
            [(-HALF_WIDTH, 0, 0.79), (HALF_WIDTH, 0, 0.79), (HALF_WIDTH, 0, 1.55), (-HALF_WIDTH, 0, 1.55)],
            LINE,
            ' fill-opacity=".22"',
        )
    )
    path = smash_path()
    uv = cam.project(path)
    b.append(
        f'<polyline points="{" ".join(f"{u:.1f},{v:.1f}" for u, v in uv)}" fill="none" stroke="{SMASH}" stroke-width="2"/>'
    )
    hit = cam.project(path[:1])[0]
    b.append(f'<circle cx="{hit[0]:.1f}" cy="{hit[1]:.1f}" r="3" fill="{SMASH}"/>')
    k = cam.project(
        np.array([(-2.5, -HALF_LENGTH + 0.7, KEEP_IN_VIEW), (2.5, -HALF_LENGTH + 0.7, KEEP_IN_VIEW)])
    )
    b.append(
        f'<line x1="{k[0][0]:.1f}" y1="{k[0][1]:.1f}" x2="{k[1][0]:.1f}" y2="{k[1][1]:.1f}" stroke="{VIEW}" '
        f'stroke-width="1.3" stroke-dasharray="4 3"/>'
    )
    b.append(text(k[1][0] + 4, k[1][1] + 3, f"{KEEP_IN_VIEW:g} m up", 8, VIEW))
    nb = cam.project(np.array([(HALF_WIDTH, -HALF_LENGTH, 0.0)]))[0]
    b.append(text(min(nb[0] + 4, W - 70), nb[1] - 9, "your baseline", 8, TEXT))
    b.append(
        f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="10" fill="none" stroke="{PHONE_C}" stroke-width="2"/>'
    )
    b.append(text(12, 34, "ON YOUR PHONE", 7, PHONE_C, "start", "700"))
    return svg(W, H, b, "What the phone screen should show: the whole court, both net posts, and space above")


def main() -> None:
    for name, make in (
        ("guide-side.svg", side_view),
        ("guide-top.svg", top_view),
        ("guide-frame.svg", frame_view),
    ):
        (OUT / name).write_text(make(), encoding="utf-8")
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()

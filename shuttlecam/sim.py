"""Synthetic smash clips with known ground truth.

Renders roughly what the camera sees from the side of the court: a textured hall
background, a player swaying, sensor noise, mains flicker, and a shuttle that
drops in slowly, gets smashed, then slows down under real drag physics.

The simulated shuttle uses a slightly different terminal velocity (6.5 m/s) from
the tracker's default (6.7 m/s), so the tests don't rely on the filter having
perfect physics.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property

import cv2
import numpy as np

from .calibration import Calibration
from .camera import Frame


@dataclass
class SmashScene:
    speed_kmh: float = 250.0
    angle_deg: float = -12.0  # launch angle, negative is downward
    hit_point: tuple[float, float] = (-3.0, 2.6)  # metres; x along the court (net at 0), y up
    terminal_velocity: float = 6.5
    gravity: float = 9.81
    fps: float = 200.0
    exposure_s: float = 0.0005
    size: tuple[int, int] = (320, 240)
    # Visible part of the flight plane: x_min, x_max, y_min, y_max in metres.
    view: tuple[float, float, float, float] = (-4.6, 3.2, -0.2, 3.6)
    shuttle_diameter: float = 0.065
    t_hit: float = 0.08
    duration: float = 0.5
    incoming_velocity: tuple[float, float] = (-3.5, -4.5)
    noise: float = 2.0  # sensor noise, grey levels (1 sigma)
    flicker: float = 0.03  # relative brightness swing at 100 Hz
    player: bool = True
    seed: int = 1

    # -- camera ------------------------------------------------------------------

    def image_corners(self) -> np.ndarray:
        """Where the corners of the view land in the image: a mild keystone."""
        w, h = self.size
        return np.array([[8, h - 6], [w - 4, h - 10], [w - 12, 6], [4, 12]], np.float64)

    def world_corners(self) -> np.ndarray:
        x0, x1, y0, y1 = self.view
        return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float64)

    def calibration(self) -> Calibration:
        """The calibration a user would get by clicking the four corners."""
        return Calibration.from_points(self.image_corners(), self.world_corners(), self.size)

    # -- physics -----------------------------------------------------------------

    def _launch(self) -> np.ndarray:
        v0 = self.speed_kmh / 3.6
        a = math.radians(self.angle_deg)
        return np.array([self.hit_point[0], self.hit_point[1], v0 * math.cos(a), v0 * math.sin(a)])

    _STEP = 5e-5  # integration step for the ground truth, seconds

    @cached_property
    def _flight(self) -> np.ndarray:
        """States after the hit on a fine time grid, integrated with RK4."""
        k = self.gravity / self.terminal_velocity**2
        g = self.gravity

        def f(s):
            sp = math.hypot(s[2], s[3])
            return np.array([s[2], s[3], -k * sp * s[2], -k * sp * s[3] - g])

        h = self._STEP
        n = math.ceil((self.duration + 0.1) / h) + 2
        out = np.empty((n, 4))
        s = self._launch()
        for i in range(n):
            out[i] = s
            k1 = f(s)
            k2 = f(s + 0.5 * h * k1)
            k3 = f(s + 0.5 * h * k2)
            k4 = f(s + h * k3)
            s = s + (h / 6) * (k1 + 2 * k2 + 2 * k3 + k4)
        return out

    def truth(self, t: float) -> np.ndarray:
        """True [x, y, vx, vy] at time t."""
        if t < self.t_hit:
            vx, vy = self.incoming_velocity
            dt = t - self.t_hit
            return np.array([self.hit_point[0] + vx * dt, self.hit_point[1] + vy * dt, vx, vy])
        u = (t - self.t_hit) / self._STEP
        i = min(int(u), len(self._flight) - 2)
        a = u - i
        return (1 - a) * self._flight[i] + a * self._flight[i + 1]

    def true_speed_kmh(self, t: float) -> float:
        s = self.truth(t)
        return math.hypot(s[2], s[3]) * 3.6

    # -- rendering ---------------------------------------------------------------

    def _background(self, rng: np.random.Generator) -> np.ndarray:
        w, h = self.size
        y = np.linspace(0, 1, h)[:, None]
        x = np.linspace(0, 1, w)[None, :]
        bg = 95 - 35 * y + 15 * x  # hall lighting falling off toward the floor
        blobs = cv2.GaussianBlur(rng.normal(0, 1, (h, w)), (0, 0), 6)
        bg = bg + 25 * blobs / (blobs.std() + 1e-9)
        for col in range(20, w, 47):  # wall panels
            bg[:, col : col + 2] -= 18
        floor = int(h * 0.82)
        bg[floor:, :] -= 20
        return bg

    def frames(self) -> Iterator[Frame]:
        rng = np.random.default_rng(self.seed)
        w, h = self.size
        background = self._background(rng)
        to_image = cv2.getPerspectiveTransform(
            self.world_corners().astype(np.float32), self.image_corners().astype(np.float32)
        )

        def px(x: float, y: float) -> tuple[float, float]:
            p = to_image @ np.array([x, y, 1.0])
            return p[0] / p[2], p[1] / p[2]

        scale = abs(px(1, 1)[0] - px(0, 1)[0])  # rough pixels per metre
        thickness = max(1, round(self.shuttle_diameter * scale))
        n = int(self.duration * self.fps)
        sub = 16  # cv2 subpixel shift: coordinates in 1/16 px

        for i in range(n):
            t = i / self.fps
            img = background.copy()

            if self.player:
                # A player in a light shirt next to the hit point, swaying.
                sway = 0.15 * math.sin(2 * math.pi * 1.3 * t)
                cx, cy = px(self.hit_point[0] - 0.45 + sway, 1.0)
                top = px(self.hit_point[0] - 0.45 + sway, 1.85)
                axes = (int(0.25 * scale), int(abs(cy - top[1])))
                cv2.ellipse(img, (int(cx), int(cy)), axes, 0, 0, 360, 165, -1)

            # The shuttle, smeared over the exposure time.
            a = self.truth(t)
            b = self.truth(t + self.exposure_s)
            layer = np.zeros((h, w), np.uint8)
            pa = tuple(round(c * sub) for c in px(a[0], a[1]))
            pb = tuple(round(c * sub) for c in px(b[0], b[1]))
            cv2.line(layer, pa, pb, 255, thickness, cv2.LINE_AA, shift=4)
            alpha = layer.astype(np.float64) / 255.0
            img = img * (1 - alpha) + 235 * alpha

            img = img * (1 + self.flicker * math.sin(2 * math.pi * 100 * t))
            img = img + rng.normal(0, self.noise, (h, w))
            yield Frame(np.clip(img, 0, 255).astype(np.uint8), t, i)

    def write_video(self, path: str) -> None:
        w, h = self.size
        out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h), isColor=False)
        if not out.isOpened():
            raise RuntimeError(f"could not write {path}")
        for frame in self.frames():
            out.write(frame.image)
        out.release()

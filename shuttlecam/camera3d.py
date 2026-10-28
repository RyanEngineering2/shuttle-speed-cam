"""A full 3D camera model, calibrated from the badminton court.

The planar calibration in calibration.py assumes the shuttle flies across the
camera's view, side-on. Match footage is usually filmed from behind a baseline,
with the shuttle flying toward or away from the camera, so we need the real
camera: where it is, which way it points, and its focal length.

The court is a free calibration target with exactly known dimensions. Click (or
type) where a handful of landmarks appear in a frame: line crossings and the
tops and bases of the net posts. `calibrate_camera` then finds the pinhole camera
that best reprojects them. It assumes square pixels, the principal point at the
image centre and no lens distortion, which suits phone video (phones correct
distortion in processing) and leaves only the focal length to search for.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .court import landmarks


@dataclass
class CameraModel:
    f: float  # focal length, pixels
    cx: float  # principal point, pixels
    cy: float
    rvec: np.ndarray  # world -> camera rotation (Rodrigues vector)
    tvec: np.ndarray  # world -> camera translation
    image_size: tuple[int, int]

    @property
    def K(self) -> np.ndarray:
        return np.array([[self.f, 0.0, self.cx], [0.0, self.f, self.cy], [0.0, 0.0, 1.0]])

    @property
    def R(self) -> np.ndarray:
        return cv2.Rodrigues(np.asarray(self.rvec, float))[0]

    @property
    def position(self) -> np.ndarray:
        """Camera centre in world coordinates (metres)."""
        return -self.R.T @ np.asarray(self.tvec, float).ravel()

    def project(self, points) -> np.ndarray:
        """World points, shape (N, 3) -> pixels, shape (N, 2)."""
        P = np.atleast_2d(np.asarray(points, float))
        Pc = P @ self.R.T + np.asarray(self.tvec, float).ravel()
        uv = Pc[:, :2] / Pc[:, 2:3]
        return uv * self.f + np.array([self.cx, self.cy])

    def depth(self, points) -> np.ndarray:
        """Distance in front of the camera along its optical axis."""
        P = np.atleast_2d(np.asarray(points, float))
        return (P @ self.R.T + np.asarray(self.tvec, float).ravel())[:, 2]

    def ray(self, u: float, v: float) -> tuple[np.ndarray, np.ndarray]:
        """Camera centre and unit direction of the ray through pixel (u, v)."""
        d = self.R.T @ np.array([(u - self.cx) / self.f, (v - self.cy) / self.f, 1.0])
        return self.position, d / np.linalg.norm(d)

    def point_at_height(self, u: float, v: float, z: float = 0.0) -> np.ndarray | None:
        """Where the ray through (u, v) reaches height z, or None if it never does."""
        c, d = self.ray(u, v)
        if abs(d[2]) < 1e-9:
            return None
        s = (z - c[2]) / d[2]
        return c + s * d if s > 0 else None

    def for_frame_size(self, width: int, height: int) -> CameraModel:
        """The same camera for frames scaled to another resolution."""
        s = width / self.image_size[0]
        return CameraModel(self.f * s, self.cx * s, self.cy * s, self.rvec, self.tvec, (width, height))

    def save(self, path: str | Path, extra: dict | None = None) -> None:
        data = {
            "f": self.f,
            "cx": self.cx,
            "cy": self.cy,
            "rvec": np.asarray(self.rvec, float).ravel().tolist(),
            "tvec": np.asarray(self.tvec, float).ravel().tolist(),
            "image_size": list(self.image_size),
            "position_m": self.position.round(3).tolist(),
        }
        data.update(extra or {})
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: str | Path) -> CameraModel:
        d = json.loads(Path(path).read_text())
        return cls(d["f"], d["cx"], d["cy"], np.array(d["rvec"]), np.array(d["tvec"]), tuple(d["image_size"]))


@dataclass
class CameraCalibration:
    camera: CameraModel
    rms_px: float
    errors_px: dict[str, float]  # reprojection error per landmark


def _pose(world: np.ndarray, image: np.ndarray, f: float, cx: float, cy: float):
    K = np.array([[f, 0.0, cx], [0.0, f, cy], [0.0, 0.0, 1.0]])
    ok, rvec, tvec = cv2.solvePnP(world, image, K, None, flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return math.inf, None, None
    rvec, tvec = cv2.solvePnPRefineLM(world, image, K, None, rvec, tvec)
    proj, _ = cv2.projectPoints(world, rvec, tvec, K, None)
    err = np.linalg.norm(proj.reshape(-1, 2) - image, axis=1)
    return float(np.sqrt(np.mean(err**2))), rvec, tvec


def calibrate_camera(
    points: dict[str, tuple[float, float]], image_size: tuple[int, int]
) -> CameraCalibration:
    """Camera from landmark name -> pixel position (at least 6 landmarks).

    Use landmarks spread over the image, and include at least one net post top:
    points off the floor pin down the focal length much better than floor points
    alone.
    """
    known = landmarks()
    unknown = [name for name in points if name not in known]
    if unknown:
        raise ValueError(f"unknown landmark(s): {', '.join(unknown)} (see `shuttlecam calibrate3d --list`)")
    if len(points) < 6:
        raise ValueError("need at least 6 landmarks")
    names = list(points)
    world = np.array([known[n] for n in names], np.float64)
    image = np.array([points[n] for n in names], np.float64)
    w, h = image_size
    cx, cy = w / 2.0, h / 2.0

    # The focal length is the one unknown intrinsic: search it, solving the pose
    # for each candidate. Coarse log grid first, then golden-section refinement.
    grid = np.exp(np.linspace(np.log(0.25 * w), np.log(6.0 * w), 80))
    costs = [_pose(world, image, f, cx, cy)[0] for f in grid]
    i = int(np.argmin(costs))
    lo, hi = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
    g = (math.sqrt(5) - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = _pose(world, image, a, cx, cy)[0], _pose(world, image, b, cx, cy)[0]
    for _ in range(40):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = _pose(world, image, a, cx, cy)[0]
        else:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = _pose(world, image, b, cx, cy)[0]
    f = (lo + hi) / 2
    rms, rvec, tvec = _pose(world, image, f, cx, cy)
    camera = CameraModel(float(f), cx, cy, rvec.ravel(), tvec.ravel(), (w, h))
    errors = np.linalg.norm(camera.project(world) - image, axis=1)
    return CameraCalibration(camera, rms, {n: float(e) for n, e in zip(names, errors)})

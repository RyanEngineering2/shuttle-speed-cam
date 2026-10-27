"""Turn pixels into metres.

One camera cannot see depth, so we assume the shuttle flies in a known vertical
plane: the plane along the court's centre line, seen side-on. A homography maps
that plane from image pixels to metres (x along the court, y up). Four or more
points with known positions on the plane are enough, for example the two ends of
a 2 m pole held upright at the net and again at the short service line.

A shuttle that flies off that plane, toward or away from the camera, is measured
with an error roughly equal to the depth offset divided by the camera distance.
That is why the camera should sit well back from the court, side-on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Calibration:
    H: np.ndarray  # 3x3 homography, image pixels -> plane metres
    image_size: tuple[int, int]  # (width, height) the homography was measured at
    points: dict = field(default_factory=dict)  # the clicked points, for reference

    @classmethod
    def from_points(cls, image_pts, world_pts, image_size) -> Calibration:
        img = np.asarray(image_pts, np.float64).reshape(-1, 2)
        wld = np.asarray(world_pts, np.float64).reshape(-1, 2)
        if len(img) != len(wld):
            raise ValueError("need the same number of image and world points")
        if len(img) < 4:
            raise ValueError("need at least 4 point pairs for a homography")
        H, _ = cv2.findHomography(img, wld, 0)
        if H is None:
            raise ValueError("points are degenerate (three or more in a line?)")
        return cls(H, tuple(image_size), {"image": img.tolist(), "world": wld.tolist()})

    @classmethod
    def from_scale(cls, metres_per_pixel: float, image_size, origin_px=None) -> Calibration:
        """Simple fallback: a fixed scale, with y flipped so it points up.

        `origin_px` is the pixel at world (0, 0); defaults to the bottom-left corner.
        """
        w, h = image_size
        u0, v0 = origin_px if origin_px is not None else (0.0, float(h))
        m = metres_per_pixel
        H = np.array([[m, 0.0, -m * u0], [0.0, -m, m * v0], [0.0, 0.0, 1.0]])
        return cls(H, (w, h))

    def for_frame_size(self, width: int, height: int) -> Calibration:
        """The same calibration for frames of a different resolution."""
        sx = self.image_size[0] / width
        sy = self.image_size[1] / height
        return Calibration(self.H @ np.diag([sx, sy, 1.0]), (width, height), self.points)

    def to_world(self, u: float, v: float) -> np.ndarray:
        p = self.H @ np.array([u, v, 1.0])
        return p[:2] / p[2]

    def to_image(self, x: float, y: float) -> np.ndarray:
        p = np.linalg.solve(self.H, np.array([x, y, 1.0]))
        return p[:2] / p[2]

    def jacobian(self, u: float, v: float) -> np.ndarray:
        """d(world)/d(pixel) at (u, v)."""
        p = self.H @ np.array([u, v, 1.0])
        w = p[2]
        return (self.H[:2, :2] * w - np.outer(p[:2], self.H[2, :2])) / (w * w)

    def measurement_cov(self, u: float, v: float, sigma_px: float) -> np.ndarray:
        """Pixel noise carried through to metres, with perspective taken into account."""
        J = self.jacobian(u, v)
        return (sigma_px**2) * (J @ J.T)

    def save(self, path: str | Path) -> None:
        data = {"image_size": list(self.image_size), "homography": self.H.tolist(), "points": self.points}
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: str | Path) -> Calibration:
        data = json.loads(Path(path).read_text())
        return cls(np.array(data["homography"], float), tuple(data["image_size"]), data.get("points", {}))

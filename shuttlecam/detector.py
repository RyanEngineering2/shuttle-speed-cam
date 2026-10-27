"""Find shuttle candidates in each frame.

The camera does not move, so anything that changes is a candidate. A pixel counts
only if it differs from *both*:

* a slow running average of the scene (the background), and
* the previous frame.

The first test drops the "ghost" where the shuttle was a frame ago, because that
spot now looks like the background again. The second drops anything slow or
static: players walking about, a bag left on a chair, lighting drifting.

Plain three-frame differencing (current vs. the two previous frames) is the usual
cheap alternative, but it fires on the spot where a slow shuttle sat in *both*
earlier frames, which put a fake point right at the start of tracks.

It is deliberately simple. A Pi Zero 2 W has a few milliseconds per frame at
200 fps, and the tracker downstream is what rejects players, rackets and noise.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import DetectorConfig


@dataclass
class Detection:
    x: float  # centroid, processed-frame pixels
    y: float
    area: int  # pixels in the motion blob
    strength: float  # summed brightness change, used to rank candidates


class MotionDetector:
    def __init__(self, cfg: DetectorConfig):
        if cfg.polarity not in ("bright", "dark", "any"):
            raise ValueError(f"polarity must be bright, dark or any, not {cfg.polarity!r}")
        self.cfg = cfg
        self.background: np.ndarray | None = None  # float32 running average
        self.previous: np.ndarray | None = None  # last frame, uint8
        self.kernel = np.ones((3, 3), np.uint8)
        self.ignore: np.ndarray | None = None

    def reset(self) -> None:
        self.background = None
        self.previous = None

    def _change(self, cur: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """Per-pixel change from `ref` to `cur`, as uint8, in the configured direction.

        Everything stays in 8-bit OpenCV calls, which use SIMD on the Pi's ARM cores.
        """
        # Short exposures under mains lighting flicker at 100 Hz (50 Hz mains), which
        # brightens or darkens the whole frame. Shifting the reference by the change
        # in mean brightness cancels it.
        offset = cv2.mean(cur)[0] - cv2.mean(ref)[0]
        ref = cv2.convertScaleAbs(ref, alpha=1.0, beta=offset)
        if self.cfg.polarity == "bright":
            return cv2.subtract(cur, ref)
        if self.cfg.polarity == "dark":
            return cv2.subtract(ref, cur)
        return cv2.absdiff(cur, ref)

    def __call__(self, gray: np.ndarray) -> list[Detection]:
        if self.background is None or self.background.shape != gray.shape:
            self.background = gray.astype(np.float32)
            self.previous = gray
            return []

        # How much each pixel stands out from both the background and the last frame.
        change = cv2.min(
            self._change(gray, cv2.convertScaleAbs(self.background)),
            self._change(gray, self.previous),
        )
        cv2.accumulateWeighted(gray, self.background, self.cfg.background_rate)
        self.previous = gray
        _, mask = cv2.threshold(change, self.cfg.diff_threshold, 1, cv2.THRESH_BINARY)

        if self.cfg.ignore_rects:
            if self.ignore is None or self.ignore.shape != mask.shape:
                self.ignore = np.zeros(mask.shape, bool)
                for x, y, w, h in self.cfg.ignore_rects:
                    self.ignore[y : y + h, x : x + w] = True
            mask[self.ignore] = 0

        if self.cfg.dilate:
            mask = cv2.dilate(mask, self.kernel, iterations=self.cfg.dilate)

        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        detections = []
        for i in range(1, n):
            x, y, w, h, grown = stats[i]
            # Dilation can grow a blob up to ~9x, so this is a safe early reject.
            if grown > 9 * self.cfg.max_area:
                continue
            weights = np.where(labels[y : y + h, x : x + w] == i, change[y : y + h, x : x + w], 0).astype(
                np.float32
            )
            # Size limits use the pixels that really changed, not the dilated blob, so a
            # single noisy pixel can't pass as a shuttle.
            area = int(np.count_nonzero(weights > self.cfg.diff_threshold))
            if not self.cfg.min_area <= area <= self.cfg.max_area:
                continue
            # Centroid weighted by how strongly each pixel changed. Sharper than the
            # plain blob centroid, which the dilation above smears out.
            total = float(weights.sum())
            ys, xs = np.mgrid[y : y + h, x : x + w]
            detections.append(
                Detection(
                    x=float((weights * xs).sum() / total),
                    y=float((weights * ys).sum() / total),
                    area=int(area),
                    strength=total,
                )
            )
        detections.sort(key=lambda d: d.strength, reverse=True)
        return detections[: self.cfg.max_detections]

"""Detector + tracker, wired together for live use, video files and tests."""

from __future__ import annotations

from collections.abc import Iterable

from .calibration import Calibration
from .camera import Frame
from .config import Config
from .detector import MotionDetector
from .tracker import Shot, Tracker


class Pipeline:
    def __init__(self, cfg: Config, calibration: Calibration):
        self.cfg = cfg
        self.calibration = calibration
        self.detector = MotionDetector(cfg.detector)
        self.tracker: Tracker | None = None

    def process(self, frame: Frame) -> list[Shot]:
        if self.tracker is None:
            h, w = frame.image.shape[:2]
            self.tracker = Tracker(self.cfg.tracker, self.calibration.for_frame_size(w, h))
        return self.tracker.step(frame.t, self.detector(frame.image))

    def flush(self) -> list[Shot]:
        return self.tracker.flush() if self.tracker else []


def analyze(frames: Iterable[Frame], cfg: Config, calibration: Calibration) -> list[Shot]:
    """Every shot in a finite sequence of frames."""
    pipeline = Pipeline(cfg, calibration)
    shots: list[Shot] = []

    def add(new: list[Shot]) -> None:
        for shot in new:
            if shot.replaces_previous and shots:
                shots[-1] = shot
            else:
                shots.append(shot)

    for frame in frames:
        add(pipeline.process(frame))
    add(pipeline.flush())
    return shots

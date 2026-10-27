"""Frame sources.

* `PiCameraSource`: the Raspberry Pi camera through Picamera2, for live use.
* `VideoSource`: OpenCV, for recorded clips (phone slow-motion works well) and webcams.

Both yield `Frame` objects: an 8-bit grayscale image plus a timestamp in seconds.
Timestamps come from the sensor whenever possible, because speed is distance over
time and a dropped frame must not look like a slow shuttle.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass

import cv2
import numpy as np

from .config import CameraConfig


@dataclass
class Frame:
    image: np.ndarray  # 8-bit grayscale, already scaled to the processing size
    t: float  # seconds
    index: int


def process_size(width: int, height: int, process_width: int) -> tuple[int, int]:
    """Processing size for a `width` x `height` frame, keeping the aspect ratio."""
    if process_width <= 0 or process_width >= width:
        return width, height
    h = round(height * process_width / width / 2) * 2
    return process_width, max(2, h)


def pick_sensor_mode(modes: list[dict], size: tuple[int, int], fps: float) -> dict:
    """Smallest sensor mode that covers `size` and reaches `fps`.

    Falls back to the fastest mode if none reaches the requested frame rate.
    """
    good = [
        m for m in modes if m["fps"] >= fps * 0.98 and m["size"][0] >= size[0] and m["size"][1] >= size[1]
    ]
    if not good:
        return max(modes, key=lambda m: m["fps"])
    return min(good, key=lambda m: m["size"][0] * m["size"][1])


class PiCameraSource:
    """High-frame-rate capture on the Pi.

    The main stream has to exist, but we only read the small "lores" stream. The ISP
    scales it in hardware, so the Pi Zero 2 W spends its CPU on detection rather than
    on resizing. The Y plane of YUV420 is the grayscale image, so there is no colour
    conversion either.
    """

    def __init__(self, cfg: CameraConfig):
        from picamera2 import Picamera2  # only installed on the Pi

        self.cam = Picamera2()
        mode = pick_sensor_mode(self.cam.sensor_modes, cfg.size, cfg.fps)
        fps = min(cfg.fps, mode["fps"])
        frame_us = int(1_000_000 / fps)
        self.w, self.h = process_size(cfg.size[0], cfg.size[1], cfg.process_width)
        config = self.cam.create_video_configuration(
            main={"size": tuple(cfg.size), "format": "YUV420"},
            lores={"size": (self.w, self.h), "format": "YUV420"},
            sensor={"output_size": mode["size"], "bit_depth": mode["bit_depth"]},
            controls={
                "FrameDurationLimits": (frame_us, frame_us),
                "AeEnable": False,
                "ExposureTime": min(cfg.exposure_us, frame_us),
                "AnalogueGain": cfg.analogue_gain,
            },
            buffer_count=6,
        )
        self.cam.configure(config)
        self.fps = fps
        self.cam.start()

    def __iter__(self) -> Iterator[Frame]:
        index = 0
        while True:
            request = self.cam.capture_request()
            try:
                yuv = request.make_array("lores")
                gray = np.ascontiguousarray(yuv[: self.h, : self.w])
                stamp_ns = request.get_metadata().get("SensorTimestamp")
            finally:
                request.release()
            t = stamp_ns * 1e-9 if stamp_ns else time.monotonic()
            yield Frame(gray, t, index)
            index += 1

    def close(self) -> None:
        self.cam.stop()
        self.cam.close()


class VideoSource:
    """Video files and webcams through OpenCV."""

    def __init__(self, cfg: CameraConfig, source: str):
        self.live = source.isdigit()
        self.cap = cv2.VideoCapture(int(source) if self.live else source)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open video source {source!r}")
        file_fps = self.cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.fps = cfg.file_fps or (file_fps if file_fps > 1 else cfg.fps)
        self.process_width = cfg.process_width

    def __iter__(self) -> Iterator[Frame]:
        index = 0
        while True:
            ok, image = self.cap.read()
            if not ok:
                return
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
            h, w = gray.shape
            size = process_size(w, h, self.process_width)
            if size != (w, h):
                gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
            t = time.monotonic() if self.live else index / self.fps
            yield Frame(gray, t, index)
            index += 1

    def close(self) -> None:
        self.cap.release()


def open_source(cfg: CameraConfig, source: str | None = None):
    source = source or cfg.source
    if source == "picamera2":
        return PiCameraSource(cfg)
    return VideoSource(cfg, source)

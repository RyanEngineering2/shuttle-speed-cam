"""Settings for every stage of the pipeline.

Everything has a default, so a config file is optional. A TOML file only needs the
values you want to change, for example::

    [camera]
    fps = 120

    [display]
    backend = "serial"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class CameraConfig:
    # "picamera2" for the Pi camera, a video file path, or a webcam index such as "0".
    source: str = "picamera2"
    # Sensor output size. On an IMX219 (Pi Camera v2 and clones), 640x480 is the
    # ~200 fps binned/cropped mode.
    size: tuple[int, int] = (640, 480)
    # Frames are scaled to this width before detection. On the Pi the ISP does the
    # scaling for free through the "lores" stream.
    process_width: int = 320
    fps: float = 200.0
    # A short shutter keeps the shuttle a dot instead of a long streak.
    exposure_us: int = 1000
    analogue_gain: float = 8.0
    # Phone slow-motion clips often claim 30 fps in their metadata. Set the real
    # capture rate here (for example 240) when analysing them.
    file_fps: float | None = None


@dataclass
class DetectorConfig:
    # Minimum brightness change (0-255) that counts as motion.
    diff_threshold: int = 18
    # How fast the background average follows the scene, per frame (0-1).
    background_rate: float = 0.02
    # "bright": shuttle lighter than the background, "dark": darker, "any": either.
    polarity: str = "any"
    min_area: int = 2
    max_area: int = 250
    # Dilation passes that join the fragments of a motion-blurred shuttle.
    dilate: int = 1
    max_detections: int = 8
    # Player-aware mode, for match footage with players close to the shuttle. Motion
    # blobs bigger than `player_area` pixels are treated as people, and small blobs
    # within `player_margin` pixels of one can continue a track but not start one.
    # 0 turns it off.
    player_area: int = 600
    player_margin: int = 0
    # Regions to ignore, [x, y, w, h] in processed-frame pixels. Use it for anything
    # that flickers or moves on its own, like the P10 display itself.
    ignore_rects: list[list[int]] = field(default_factory=list)


@dataclass
class TrackerConfig:
    # "drag": extended Kalman filter with air drag and gravity (recommended).
    # "cv": plain constant-velocity Kalman filter, kept for comparison.
    model: str = "drag"
    # Terminal velocity of the shuttle in m/s. Feather shuttles sit around 6.5-6.9.
    terminal_velocity: float = 6.7
    gravity: float = 9.81
    # Detector centroid noise, in processed-frame pixels (1 sigma).
    pixel_noise: float = 1.5
    # Process noise: a floor in m/s^2, plus a fraction of the current drag force to
    # cover spin, tumbling and a terminal velocity that is a little off.
    accel_noise: float = 30.0
    drag_uncertainty: float = 0.25
    # A detection joins a track only inside this many standard deviations.
    gate: float = 4.0
    # Stricter test for a track's first point, which may be from just before the hit.
    # Dropping a good first point costs one frame of speed decay; keeping a bad one
    # costs more, so this leans toward dropping.
    first_point_gate: float = 3.0
    min_hits: int = 5
    max_misses: int = 3
    max_tracks: int = 10
    max_duration_s: float = 2.0
    min_speed_kmh: float = 50.0
    max_speed_kmh: float = 450.0
    # A shot must travel at least this far, which filters out jittery false tracks.
    min_path_m: float = 0.6
    # How far, on average, detections may sit from the smoothed path, in units of
    # `pixel_noise`. A shuttle flies a clean curve and scores well under this; arms,
    # legs and rackets jump about and score higher. Try 0.6 for crowded match footage.
    max_jitter: float = 1.0
    # A track that starts this soon after the last shot is treated as the same
    # flight (the tracker lost it for a moment) and only counts if it is faster.
    merge_gap_s: float = 0.3


@dataclass
class DisplayConfig:
    # "serial": ESP32 + P10 panels (firmware/p10-display), "console", or "none".
    backend: str = "console"
    port: str = "/dev/serial0"
    baud: int = 115200


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    calibration: str = "calibration.json"
    log: str = "shots.csv"


def _apply(obj: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown setting '{where}{key}'")
        current = getattr(obj, key)
        if isinstance(value, dict):
            _apply(current, value, f"{where}{key}.")
        else:
            if isinstance(current, tuple):
                value = tuple(value)
            setattr(obj, key, value)


def load_config(path: str | Path | None = None) -> Config:
    """Defaults, overridden by whatever the TOML file at `path` sets."""
    cfg = Config()
    if path is not None:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f), "")
    return cfg

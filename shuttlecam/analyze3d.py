"""Match footage: find shots in a video and measure them in 3D.

1. Track the shuttle in image space (pixels), with player-aware detection and the
   jitter check, so arms and rackets don't become shots. The image tracker can
   follow the shuttle straight through a hit, so tracks are split wherever the
   shuttle's image speed or direction jumps.
2. For each tracked flight, look for the incoming shuttle: a track that ended just
   before this one started, close by. The hit happened where the two meet, which
   tells the 3D fit when and where contact was.
3. Fit each flight in 3D through the calibrated camera (fit3d.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .calibration import Calibration
from .camera import Frame
from .camera3d import CameraModel
from .config import Analyze3DConfig, DetectorConfig, TrackerConfig
from .detector import MotionDetector
from .fit3d import FlightFit, Observation, fit_flight
from .tracker import Tracker


@dataclass
class ImageTrack:
    observations: list[Observation]  # pixels, in the video's resolution

    @property
    def t_start(self) -> float:
        return self.observations[0].t

    @property
    def t_end(self) -> float:
        return self.observations[-1].t


@dataclass
class Shot3D:
    track: ImageTrack
    incoming: ImageTrack | None
    fit: FlightFit
    hinted: bool = False  # the user said where/when the hit was

    @property
    def launch_known(self) -> bool:
        """Is the moment of the hit known, from the incoming shuttle or a hint?
        Without it, the contact moment is a guess and only the first-seen speed is
        a measurement."""
        return self.incoming is not None or self.hinted

    @property
    def reliable(self) -> bool:
        """A clean fit: tight to the track, a settled distance, enough points, and
        a speed a shuttle can actually reach. Short or messy tracks (limbs, a shot
        mostly hidden behind a player) fail this and should not be reported."""
        f = self.fit
        return (
            f.rms_px <= 3.0
            and not f.depth_uncertain
            and len(f.observations) >= 10
            and f.first_seen_kmh <= 600
        )

    @property
    def launch_reliable(self) -> bool:
        lo, hi = self.fit.speed_range_kmh
        return self.reliable and self.launch_known and hi <= 1.5 * lo and hi <= 600


def track_image_space(frames: Iterable[Frame], cfg: Analyze3DConfig) -> list[ImageTrack]:
    """Shuttle flights in pixel coordinates."""
    detector = tracker = None
    width = height = 0
    shots = []
    for frame in frames:
        if tracker is None:
            height, width = frame.image.shape[:2]
            s = width / 1920  # settings are for 1920 px wide video
            detector = MotionDetector(
                DetectorConfig(
                    diff_threshold=cfg.diff_threshold,
                    max_area=round(cfg.max_area * s * s),
                    max_detections=20,
                    player_area=round(cfg.player_area * s * s),
                    player_margin=round(cfg.player_margin * s),
                )
            )
            # The tracker works in "world units"; here 1 unit is 1 pixel, so its
            # speed settings (km/h = units/s x 3.6) are image speeds.
            tracker = Tracker(
                TrackerConfig(
                    model="cv",
                    accel_noise=cfg.accel_noise * s,
                    pixel_noise=cfg.pixel_noise,
                    min_hits=cfg.min_hits,
                    max_misses=cfg.max_misses,
                    max_tracks=16,
                    max_duration_s=3.0,
                    min_speed_kmh=cfg.min_image_speed * s * 3.6,
                    max_speed_kmh=cfg.max_image_speed * s * 3.6,
                    min_path_m=cfg.min_path * s,
                    merge_gap_s=-1.0,
                    max_jitter=cfg.max_jitter,
                ),
                Calibration.from_scale(1.0, (width, height)),
            )
        shots += tracker.step(frame.t, detector(frame.image))
    if tracker is not None:
        shots += tracker.flush()
    tracks = []
    for shot in sorted(shots, key=lambda s: s.t_start):
        obs = [Observation(t, x, height - y, cfg.pixel_noise) for t, x, y in shot.measurements]
        tracks += split_at_hits(ImageTrack(obs))
    return tracks


def split_at_hits(track: ImageTrack, min_points: int = 4) -> list[ImageTrack]:
    """Split a track where the shuttle was hit.

    Between two hits a shuttle's image speed changes smoothly, so a hit shows up
    as a jump: the first full step after it is at least 1.7x faster (or 2.5x
    slower) than the steps just before, or the direction turns by more than 60
    degrees. A smash flying straight away from the camera can keep the same image
    direction as the incoming shuttle, so the speed jump is the main signal.
    """
    obs = track.observations
    uv = np.array([(o.u, o.v) for o in obs])
    t = np.array([o.t for o in obs])
    noise = max(o.sigma for o in obs)
    dt_frame = float(np.median(np.diff(t))) if len(t) > 1 else 1 / 60

    def step_speed(j: int) -> float:  # obs j-1 -> j, px/s
        return float(np.linalg.norm(uv[j] - uv[j - 1]) / max(t[j] - t[j - 1], 1e-9))

    best_i, best_score = None, 1.0
    for i in range(min_points - 1, len(obs) - min_points):
        before = float(np.median([step_speed(j) for j in range(max(1, i - 2), i + 1)]))
        after = step_speed(i + 2)  # the first step entirely after a hit between obs i and i+1
        score = 0.0
        if before > 0 and after > 0 and abs(after - before) * dt_frame > 4 * noise:
            ratio = after / before
            score = np.log(ratio) / np.log(1.7) if ratio > 1 else np.log(1 / ratio) / np.log(2.5)
        v_b = (uv[i] - uv[max(0, i - 3)]) / max(t[i] - t[max(0, i - 3)], 1e-9)
        v_a = (uv[min(len(obs) - 1, i + 4)] - uv[i + 1]) / max(t[min(len(obs) - 1, i + 4)] - t[i + 1], 1e-9)
        nb, na = np.linalg.norm(v_b), np.linalg.norm(v_a)
        if nb * dt_frame > 2 * noise and na * dt_frame > 2 * noise:
            turn = np.degrees(np.arccos(np.clip(v_a @ v_b / (na * nb), -1, 1)))
            score = max(score, turn / 60.0)
        if score > best_score:
            best_i, best_score = i, score
    if best_i is None:
        return [track]

    # The step that straddles the hit is itself unusual, so the jump test can be a
    # point off. The point next to the hit sits near the contact, on both flights'
    # paths; only its timing tells which flight it belongs to. Give each boundary
    # point to the side whose own motion predicts it better at its timestamp.
    def predict(seg_t, seg, t_at):
        deg = 2 if len(seg_t) >= 4 else 1
        tt = seg_t - seg_t[0]
        return np.array([np.polyval(np.polyfit(tt, seg[:, a], deg), t_at - seg_t[0]) for a in (0, 1)])

    b = best_i + 1  # first index of the second part
    for _ in range(2):
        a_t, a_uv = t[max(0, b - 6) : b - 1], uv[max(0, b - 6) : b - 1]
        c_t, c_uv = t[b : b + 5], uv[b : b + 5]
        if len(a_t) >= 3 and len(c_t) >= 3 and b - 1 >= min_points:
            last = b - 1  # does the last point of the first part belong to the second?
            if np.linalg.norm(predict(c_t, c_uv, t[last]) - uv[last]) < np.linalg.norm(
                predict(a_t, a_uv, t[last]) - uv[last]
            ):
                b -= 1
                continue
        a_t, a_uv = t[max(0, b - 5) : b], uv[max(0, b - 5) : b]
        c_t, c_uv = t[b + 1 : b + 6], uv[b + 1 : b + 6]
        if (
            len(a_t) >= 3
            and len(c_t) >= 3
            and len(obs) - (b + 1) >= min_points
            and np.linalg.norm(predict(a_t, a_uv, t[b]) - uv[b])
            < np.linalg.norm(predict(c_t, c_uv, t[b]) - uv[b])
        ):
            b += 1
            continue
        break
    best_i = b - 1
    return split_at_hits(ImageTrack(obs[: best_i + 1]), min_points) + split_at_hits(
        ImageTrack(obs[best_i + 1 :]), min_points
    )


def find_incoming(track: ImageTrack, tracks: list[ImageTrack], max_gap_s: float, max_dist_px: float):
    """The track that ended shortly before `track` started, nearest to its start."""
    best, best_d = None, max_dist_px
    first = np.array([track.observations[0].u, track.observations[0].v])
    for other in tracks:
        if other is track or not 0.0 <= track.t_start - other.t_end <= max_gap_s:
            continue
        last = np.array([other.observations[-1].u, other.observations[-1].v])
        d = float(np.linalg.norm(first - last))
        if d < best_d:
            best, best_d = other, d
    return best


def analyze_3d(
    frames: Iterable[Frame],
    camera: CameraModel,
    cfg: Analyze3DConfig,
    terminal_velocity: float = 6.7,
    gravity: float = 9.81,
) -> list[Shot3D]:
    frames = iter(frames)
    first = next(frames, None)
    if first is None:
        return []
    h, w = first.image.shape[:2]
    cam = camera.for_frame_size(w, h)

    def all_frames():
        yield first
        yield from frames

    tracks = track_image_space(all_frames(), cfg)
    shots = []
    for track in tracks:
        if len(track.observations) < 6:
            continue
        incoming = find_incoming(track, tracks, cfg.incoming_gap_s, cfg.incoming_distance * w / 1920)
        if incoming is not None:
            # The image tracker may have handed the first point after the hit to the
            # incoming track (or the other way round): re-split the two together.
            parts = split_at_hits(ImageTrack(incoming.observations + track.observations))
            if len(parts) >= 2 and len(parts[-1].observations) >= 6:
                incoming, track = parts[-2], parts[-1]
            if len(incoming.observations) < 3:
                incoming = None
        try:
            fit = fit_flight(
                cam,
                track.observations,
                terminal_velocity=terminal_velocity,
                gravity=gravity,
                incoming=incoming.observations if incoming else None,
            )
        except (ValueError, RuntimeError, np.linalg.LinAlgError):
            continue
        shots.append(Shot3D(track, incoming, fit))
    return shots

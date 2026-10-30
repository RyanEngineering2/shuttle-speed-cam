"""Match footage: find shots in a video and measure them in 3D.

1. Track the shuttle in image space (pixels), with player-aware detection and the
   jitter check, so arms and rackets don't become shots. The image tracker can
   follow the shuttle straight through a hit, so tracks are split wherever the
   shuttle's image speed or direction jumps.
2. For each tracked flight, look for the incoming shuttle: a track that ended just
   before this one started, close by. The hit happened where the two meet, which
   tells the 3D fit when and where contact was.
3. Fit each flight in 3D through the calibrated camera (fit3d.py).

Videos re-uploaded at 60 fps are often 25 or 30 fps footage with frames repeated
two or three times. Repeated frames would make the shuttle look like it stops and
jumps, and their timestamps are wrong, so `find_repeated_frames` spots them and
the real frames are re-timed at the real frame rate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import cv2
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
    hinted: bool = False  # the user said where/when the hit was, or anchored both ends
    anchored: bool = False  # take-off spot and landing given: both ends of the flight pinned

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


STANDARD_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0)


@dataclass
class FrameTiming:
    keep: list[int]  # indices of the real (non-repeated) frames
    rate: float  # their real frame rate

    def time_of(self, index: float) -> float:
        """Re-timed time of a (possibly fractional) original frame index."""
        k = np.interp(index, self.keep, np.arange(len(self.keep)))
        return float(k / self.rate)


def median_background(
    video_path: str, start: int = 0, end: int | None = None, samples: int = 31
) -> np.ndarray | None:
    """The scene without its moving parts: the per-pixel median of frames spread over the clip.

    The motion detector otherwise learns the background from the first frame and
    takes a second or two to forget what was in it. A phone clip trimmed to start
    just before a smash doesn't have that long: where the players stood at the start
    lingers as ghost motion while the shuttle flies, and the smash is lost among
    them. In the median of the whole clip, anyone who moves about is gone.
    """
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    last = total - 1 if end is None else min(end, total - 1)
    if last - start < 10:
        cap.release()
        return None
    wanted = set(np.linspace(start, last, min(samples, last - start + 1)).round().astype(int).tolist())
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    stack = []
    for i in range(start, last + 1):
        if not cap.grab():
            break
        if i in wanted:
            ok, image = cap.retrieve()
            if ok:
                stack.append(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image)
    cap.release()
    if len(stack) < 5:
        return None
    return np.median(np.stack(stack), axis=0).astype(np.uint8)


def find_repeated_frames(video_path: str, start: int = 0, end: int | None = None) -> FrameTiming | None:
    """Does this video repeat frames? If so, which frames are real, and at what rate?

    Per frame we measure how much it changed from the one before (the 99.9th
    percentile of the change, after cancelling any whole-frame brightness shift,
    at reduced size for speed), relative to how much a typical real frame changes
    in this video. A repeat changes by almost nothing: under a tenth of that. A
    video counts as repeating when at least a quarter of its frames are repeats
    and almost none fall in the gap between (a tenth to 40%), which keeps real
    60 fps footage of a quiet scene from being mistaken for repeats.
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    prev, change, i = None, [], start
    while end is None or i <= end:
        ok, img = cap.read()
        if not ok:
            break
        g = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (480, 270), interpolation=cv2.INTER_AREA)
        g = g.astype(np.int16)
        if prev is not None:
            diff = g - prev
            change.append(float(np.percentile(np.abs(diff - round(float(diff.mean()))), 99.9)))
        prev = g
        i += 1
    cap.release()
    d = np.array(change)
    if len(d) < 30:
        return None
    typical = float(np.median(np.sort(d)[len(d) // 2 :])) or 1.0
    rel = d / typical
    if np.mean(rel < 0.1) < 0.25 or np.mean((rel >= 0.1) & (rel < 0.4)) > 0.03:
        return None
    changed = [start + 1 + j for j, x in enumerate(rel) if x >= 0.1]
    keep = [start] + changed  # the first frame we read may itself be a repeat; keep it anyway
    if len(changed) < 3:
        return None
    # Real frames arrive every 2.4 file frames (25 in 60), as alternating gaps of 2
    # and 3: the slope of a straight-line fit gives that spacing. Counting the first
    # frame read as new would skew the rate on a short stretch.
    spacing = float(np.polyfit(np.arange(len(changed)), changed, 1)[0])
    measured = fps / spacing
    rate = min(STANDARD_RATES, key=lambda r: abs(r - measured))
    if abs(rate - measured) / measured > 0.04:
        rate = measured
    return FrameTiming(keep, rate)


def track_image_space(
    frames: Iterable[Frame], cfg: Analyze3DConfig, background: np.ndarray | None = None
) -> list[ImageTrack]:
    """Shuttle flights in pixel coordinates. `background`: an 8-bit grayscale frame
    of the empty scene to start the motion detector from (see median_background)."""
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
            if background is not None and background.shape == (height, width):
                detector.prime(background)
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
    background: np.ndarray | None = None,
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

    tracks = track_image_space(all_frames(), cfg, background)
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

"""Link detections into tracks and turn finished tracks into shot speeds.

Each frame:
  1. Every live track predicts where the shuttle is now.
  2. Detections are matched to tracks by Mahalanobis distance, best match first,
     inside a gate. Matched tracks update.
  3. Leftover detections are paired with leftovers from the last two frames. A pair
     whose implied speed is plausible for a hit shuttle starts a new track. Slow
     things like players and a shuttle drifting down never get a track.
  4. A track that misses too many frames ends. If it was long and fast enough, it
     is smoothed and reported as a `Shot`.

The reported speed is the peak of the smoothed track. After the hit, drag only
slows the shuttle down, so that peak is at the start of the flight.

The hit usually happens between two frames, so a track often starts on the frame
*before* the hit, with the shuttle still on the racket. That point has the right
position (the hit point) but the wrong time, and smoothing it as if the shuttle
were already flying pulls the speed down by ~5%. So the first point gets special
treatment, see `_anchor_first_point`.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from .calibration import Calibration
from .config import TrackerConfig
from .detector import Detection
from .kalman import EKF, ConstantVelocityModel, DragModel, H, rts_smooth

KMH = 3.6

Measurement = tuple[float, np.ndarray, np.ndarray]  # (t, z, R): time, metres, covariance


@dataclass
class Shot:
    speed_kmh: float  # peak speed of the smoothed track
    raw_speed_kmh: float  # peak frame-to-frame speed, before any filtering
    t_start: float
    t_end: float
    # (t, x, y) measurements in metres
    measurements: list[tuple[float, float, float]] = field(default_factory=list)
    # (t, x, y, vx, vy) smoothed states over the flight. The first one may sit between
    # two frames: the moment the shuttle left the racket.
    smoothed: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    # True when this is a faster reading of the flight reported just before it.
    replaces_previous: bool = False

    @property
    def n_points(self) -> int:
        return len(self.measurements)

    @property
    def t_peak(self) -> float:
        """When the shuttle had the reported speed (usually the hit, or just after)."""
        return max(self.smoothed, key=lambda s: math.hypot(s[3], s[4]))[0]


class Track:
    def __init__(self, ekf: EKF, first: Measurement, second: Measurement):
        self.ekf = ekf
        self.measurements = [first, second]
        self.misses = 0

    @property
    def hits(self) -> int:
        return len(self.measurements)

    @property
    def t_start(self) -> float:
        return self.measurements[0][0]


class Tracker:
    def __init__(self, cfg: TrackerConfig, calibration: Calibration):
        self.cfg = cfg
        self.cal = calibration
        if cfg.model == "drag":
            self.model = DragModel(cfg.terminal_velocity, cfg.gravity)
        elif cfg.model == "cv":
            self.model = ConstantVelocityModel()
        else:
            raise ValueError(f"tracker model must be 'drag' or 'cv', not {cfg.model!r}")
        self.tracks: list[Track] = []
        # Unmatched detections from recent frames: (t, z, R, blob area in pixels).
        self.seeds: list[tuple[float, np.ndarray, np.ndarray, int]] = []
        self.last_t: float | None = None
        self.last_shot: Shot | None = None

    # -- per frame ---------------------------------------------------------------

    def step(self, t: float, detections: list[Detection]) -> list[Shot]:
        cfg = self.cfg
        measurements = []
        areas = []
        for d in detections:
            z = self.cal.to_world(d.x, d.y)
            R = self.cal.measurement_cov(d.x, d.y, cfg.pixel_noise)
            measurements.append((z, R))
            areas.append(d.area)

        for track in self.tracks:
            track.ekf.predict(t)

        # Greedy association, closest pair first.
        gate2 = cfg.gate**2
        pairs = []
        for ti, track in enumerate(self.tracks):
            for mi, (z, R) in enumerate(measurements):
                d2 = track.ekf.distance2(z, R)
                if d2 < gate2:
                    pairs.append((d2, ti, mi))
        pairs.sort()
        used_tracks: set[int] = set()
        used_meas: set[int] = set()
        for _, ti, mi in pairs:
            if ti in used_tracks or mi in used_meas:
                continue
            z, R = measurements[mi]
            track = self.tracks[ti]
            track.ekf.update(z, R)
            track.measurements.append((t, z, R))
            track.misses = 0
            used_tracks.add(ti)
            used_meas.add(mi)

        shots = []
        alive = []
        for ti, track in enumerate(self.tracks):
            if ti not in used_tracks:
                track.misses += 1
            confirmed = track.hits >= cfg.min_hits
            limit = cfg.max_misses if confirmed else 1
            if track.misses > limit or t - track.t_start > cfg.max_duration_s:
                shot = self._finish(track)
                if shot is not None:
                    shots.append(shot)
            else:
                alive.append(track)
        self.tracks = alive

        # Detections inside any track's gate are not used to start new tracks: they
        # are usually a second fragment of a shuttle that is already being tracked.
        near_a_track = {mi for _, _, mi in pairs}
        leftovers = [(t, z, R, areas[mi]) for mi, (z, R) in enumerate(measurements) if mi not in near_a_track]
        self._seed(t, leftovers)
        return [s for s in (self._emit(s) for s in shots) if s is not None]

    def flush(self) -> list[Shot]:
        """End every live track, for example at the end of a video file."""
        shots = [self._finish(track) for track in self.tracks]
        self.tracks = []
        self.seeds = []
        return [s for s in (self._emit(s) for s in shots if s is not None) if s is not None]

    # -- track birth -------------------------------------------------------------

    def _seed(self, t: float, leftovers) -> None:
        """Start candidate tracks from pairs of unmatched detections.

        Several earlier detections can pair with a new one at a plausible speed (the
        shuttle, a player's arm, a racket). Rather than guess, every plausible pair
        starts a candidate track: pairs from the previous frame first, then the
        most similar blob sizes. A wrong pair predicts the next position badly,
        misses, and is gone within two frames.
        """
        cfg = self.cfg
        lo = cfg.min_speed_kmh / KMH
        hi = cfg.max_speed_kmh / KMH * 1.2
        # Seeds from the last two frames, so one missed frame does not stop a track
        # from starting.
        frame_dt = t - self.last_t if self.last_t is not None and t > self.last_t else 0.02
        self.seeds = [s for s in self.seeds if 0 < t - s[0] < 2.5 * frame_dt]
        candidates = []
        for _, z, R, area in leftovers:
            for t0, z0, R0, area0 in self.seeds:
                size_change = abs(math.log(area / area0))
                if size_change > math.log(4):  # a shuttle's blob doesn't change size that much
                    continue
                v = (z - z0) / (t - t0)
                if lo <= float(np.hypot(*v)) <= hi:
                    candidates.append((-t0, size_change, t0, z0, R0, v, z, R))
        candidates.sort(key=lambda c: c[:2])
        room = min(4, cfg.max_tracks - len(self.tracks))
        for _, _, t0, z0, R0, v, z, R in candidates[: max(0, room)]:
            self.tracks.append(self._new_track(t0, z0, R0, v, t, z, R))
        self.seeds += leftovers
        self.last_t = t

    def _start_filter(self, t0, z0, R0, v) -> EKF:
        cfg = self.cfg
        speed = float(np.hypot(*v))
        # The two-point velocity is an average over the interval, and drag makes
        # the real speed at t0 higher, so start with a generous velocity spread.
        sigma_v = 0.3 * speed + 5.0
        P0 = np.zeros((4, 4))
        P0[:2, :2] = R0
        P0[2, 2] = P0[3, 3] = sigma_v**2
        return EKF(
            self.model,
            np.array([z0[0], z0[1], v[0], v[1]]),
            P0,
            t0,
            cfg.accel_noise,
            cfg.drag_uncertainty,
        )

    def _new_track(self, t0, z0, R0, v, t1, z1, R1) -> Track:
        ekf = self._start_filter(t0, z0, R0, v)
        ekf.predict(t1)
        ekf.update(z1, R1)
        return Track(ekf, (t0, z0, R0), (t1, z1, R1))

    # -- track death -------------------------------------------------------------

    def _smooth(self, meas: list[Measurement]):
        """Fresh filter + RTS smoother over a list of measurements."""
        (t0, z0, R0), (t1, z1, _) = meas[0], meas[1]
        ekf = self._start_filter(t0, z0, R0, (z1 - z0) / (t1 - t0))
        for t, z, R in meas[1:]:
            ekf.predict(t)
            ekf.update(z, R)
        return rts_smooth(ekf.steps)

    def _anchor_first_point(self, meas: list[Measurement], steps: int = 24):
        """Where on the trajectory is the first point, and when was the shuttle there?

        The first point of a track may come from just before the hit, with the
        shuttle still on the racket. Its *position* is then right (the hit point)
        but its *time* is not: the shuttle only left that spot later, between two
        frames. So the first point is used as a position anchor only. The rest of
        the track is smoothed, run backwards in time, and we find the moment it
        passed closest to the first point.

        Returns:
          "drop"         the trajectory never comes near the first point, so it was
                         something else;
          "in flight"    it passes the first point at the first point's own time:
                         an ordinary point, smooth it with the rest;
          (time, state)  it passes the first point later, between the first two
                         frames: the shuttle left the racket then.
        """
        xs, Ps = self._smooth(meas[1:])
        (t0, z0, R0), (t1, _, _) = meas[0], meas[1]
        dt = -(t1 - t0) / steps
        x, P_F = xs[0], np.eye(4)
        best = None
        for i in range(1, steps + 1):
            x, F = self.model.propagate(x, dt)
            P_F = F @ P_F
            # Closest in plain distance. (The statistical distance would favour the
            # earliest times, where the extrapolation is most uncertain.)
            gap = float(np.hypot(*(z0 - x[:2])))
            if best is None or gap < best[0]:
                best = (gap, i, x, P_F)
        _, i, x, P_F = best
        S = H @ P_F @ Ps[0] @ P_F.T @ H.T + R0
        y = z0 - x[:2]
        if float(y @ np.linalg.solve(S, y)) >= self.cfg.first_point_gate**2:
            return "drop"
        # Crossing at (or before) the first frame: an ordinary in-flight point.
        # Anywhere later, anchor it. Anchoring an in-flight point by mistake costs
        # almost nothing; smoothing a racket point as if it were flying costs ~5%.
        if i == steps:
            return "in flight"
        return t1 + i * dt, x

    def _finish(self, track: Track) -> Shot | None:
        cfg = self.cfg
        meas = track.measurements
        if len(meas) < cfg.min_hits:
            return None
        anchor = None
        fitted = meas
        if len(meas) > cfg.min_hits:
            first = self._anchor_first_point(meas)
            if first == "drop":
                meas = fitted = meas[1:]  # the first point was not part of this flight
            elif first != "in flight":
                anchor = first
                fitted = meas[1:]
        xs, _ = self._smooth(fitted)
        smoothed = [
            (t, float(x[0]), float(x[1]), float(x[2]), float(x[3])) for (t, _, _), x in zip(fitted, xs)
        ]
        if anchor is not None:
            t, x = anchor
            smoothed.insert(0, (t, float(x[0]), float(x[1]), float(x[2]), float(x[3])))
        peak = max(math.hypot(s[3], s[4]) for s in smoothed) * KMH

        meas = [(t, float(z[0]), float(z[1])) for t, z, _ in meas]
        path = 0.0
        raw_peak = 0.0
        for (ta, xa, ya), (tb, xb, yb) in itertools.pairwise(meas):
            dist = math.hypot(xb - xa, yb - ya)
            path += dist
            if tb > ta:
                raw_peak = max(raw_peak, dist / (tb - ta) * KMH)

        if path < cfg.min_path_m or not cfg.min_speed_kmh <= peak <= cfg.max_speed_kmh:
            return None
        return Shot(peak, raw_peak, meas[0][0], meas[-1][0], meas, smoothed)

    def _emit(self, shot: Shot) -> Shot | None:
        """Merge a shot with the previous one if it looks like the same flight.

        A slower duplicate is dropped. A faster one is reported with
        `replaces_previous` set, so the display can simply show the new number.
        """
        last = self.last_shot
        if last is not None and shot.t_start - last.t_end < self.cfg.merge_gap_s:
            if shot.speed_kmh <= last.speed_kmh:
                last.t_end = max(last.t_end, shot.t_end)
                return None
            shot.replaces_previous = True
        self.last_shot = shot
        return shot

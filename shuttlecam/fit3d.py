"""Fit a shuttle's 3D flight to its 2D track, seen through a calibrated camera.

One camera cannot see depth directly, but a shuttle's flight is tightly
constrained: it leaves the racket and then only drag and gravity act on it,

    dv/dt = -k |v| v - g z_hat,      k = g / v_t^2.

Scaling a flight toward or away from the camera changes how much drag and gravity
bend it, so only one depth fits the observed image track. The fit finds the
contact moment, the 3D contact point and the launch velocity that best reproduce
the shuttle's pixel position in every frame.

The contact itself is rarely visible: it falls between two frames, and the racket
hides the shuttle. The launch speed is therefore extrapolated back from the first
clear sighting, and a shuttle loses a lot of speed in that gap, so it is
reported as a range: the speeds for every contact moment that still fits the
track. The speed at the first sighting is measured directly and is much tighter.

Hints make the fit sturdier, the first one most of all:

* the incoming shuttle: the hit happens where the incoming flight ends, so its
  track (extrapolated a few frames) tells the fit when and where contact was;
* the pixel (and frame) where the racket met the shuttle;
* the smasher's take-off spot on the floor, which pins down depth;
* where and when the shuttle landed. The floor is a known plane, so even a camera
  far away locates the landing exactly. With the take-off spot, this anchors both
  ends of the flight, and the time between them fixes the speed (time of flight).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .camera3d import CameraModel

KMH = 3.6


@dataclass
class Observation:
    t: float  # seconds
    u: float  # pixels, in the camera's calibrated resolution
    v: float
    sigma: float = 1.5  # pixel noise (1 sigma)


@dataclass
class FlightFit:
    contact_time: float
    contact: np.ndarray  # 3D contact point, metres
    velocity: np.ndarray  # launch velocity, m/s
    speed_kmh: float  # launch speed, best fit
    speed_range_kmh: tuple[float, float]  # over every contact moment that fits
    first_seen_time: float
    first_seen_kmh: float  # speed at the first observation
    first_seen_range_kmh: tuple[float, float]
    rms_px: float
    landing: np.ndarray | None  # where it reaches the floor, if it does within 1.5 s
    landing_time: float | None
    k: float
    gravity: float
    observations: list[Observation] = field(default_factory=list)
    first_point_was_racket: bool = False  # first point was used as the contact hint instead
    # Distances from the camera (at the first sighting) that fit nearly as well as
    # the best one, as a fraction of it. Wide means one camera can't tell how far
    # away the shot was: add the take-off hint.
    depth_range: tuple[float, float] = (1.0, 1.0)
    # Farthest / nearest distance from the camera over the tracked flight. Below
    # about 1.5 the view has little perspective (see _summarize).
    distance_ratio: float = 1.0

    @property
    def depth_uncertain(self) -> bool:
        return self.depth_range[1] / self.depth_range[0] > 1.3

    def states(self, times) -> np.ndarray:
        """[x, y, z, vx, vy, vz] at the given times (>= contact time)."""
        return _integrate(
            self.contact, self.velocity, self.contact_time, np.asarray(times, float), self.k, self.gravity
        )


# -- physics -----------------------------------------------------------------------


def _integrate(p0, v0, t0: float, times: np.ndarray, k: float, g: float, h: float = 2e-3) -> np.ndarray:
    """RK4 integration of drag + gravity, sampled at ascending `times` >= t0."""
    x, y, z = (float(c) for c in p0)
    vx, vy, vz = (float(c) for c in v0)
    t = t0
    out = np.empty((len(times), 6))

    def acc(ax, ay, az):
        s = k * math.sqrt(ax * ax + ay * ay + az * az)
        return -s * ax, -s * ay, -s * az - g

    order = np.argsort(times)
    for idx in order:
        target = float(times[idx])  # plain floats: a wild trial step overflows quietly to inf
        while target - t > 1e-12:
            dt = min(h, target - t)
            a1 = acc(vx, vy, vz)
            v2 = (vx + 0.5 * dt * a1[0], vy + 0.5 * dt * a1[1], vz + 0.5 * dt * a1[2])
            a2 = acc(*v2)
            v3 = (vx + 0.5 * dt * a2[0], vy + 0.5 * dt * a2[1], vz + 0.5 * dt * a2[2])
            a3 = acc(*v3)
            v4 = (vx + dt * a3[0], vy + dt * a3[1], vz + dt * a3[2])
            a4 = acc(*v4)
            x += dt / 6 * (vx + 2 * v2[0] + 2 * v3[0] + v4[0])
            y += dt / 6 * (vy + 2 * v2[1] + 2 * v3[1] + v4[1])
            z += dt / 6 * (vz + 2 * v2[2] + 2 * v3[2] + v4[2])
            vx += dt / 6 * (a1[0] + 2 * a2[0] + 2 * a3[0] + a4[0])
            vy += dt / 6 * (a1[1] + 2 * a2[1] + 2 * a3[1] + a4[1])
            vz += dt / 6 * (a1[2] + 2 * a2[2] + 2 * a3[2] + a4[2])
            t += dt
        out[idx] = (x, y, z, vx, vy, vz)
    return out


# -- least squares -----------------------------------------------------------------


def _levenberg_marquardt(fun, x0: np.ndarray, max_iter: int = 80) -> tuple[np.ndarray, float]:
    """Plain Levenberg-Marquardt with a forward-difference Jacobian."""
    x = np.asarray(x0, float).copy()
    r = fun(x)
    cost = float(r @ r)
    lam = 1e-3
    for _ in range(max_iter):
        J = np.empty((len(r), len(x)))
        for j in range(len(x)):
            step = 1e-6 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += step
            J[:, j] = (fun(xp) - r) / step
        A = J.T @ J
        grad = J.T @ r
        improved = False
        while lam < 1e10:
            dx = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -grad)
            rn = fun(x + dx)
            cn = float(rn @ rn) if np.all(np.isfinite(rn)) else math.inf
            if cn < cost:
                done = cost - cn < 1e-10 * (1 + cost) and np.linalg.norm(dx) < 1e-8 * (1 + np.linalg.norm(x))
                x, r, cost = x + dx, rn, cn
                lam = max(lam / 3, 1e-12)
                improved = True
                break
            lam *= 4
        if not improved or done:
            break
    return x, cost


class _Problem:
    """Residuals for one fit: reprojection errors plus optional hints and sanity terms."""

    def __init__(
        self, camera, obs, k, g, gap, contact_hint, takeoff_hint, incoming=None, time_hint=None, landing=None
    ):
        self.camera = camera
        self.landing = landing  # (floor point xyz, time, time sigma) or None
        self.observations = obs
        self.t = np.array([o.t for o in obs])
        self.uv = np.array([(o.u, o.v) for o in obs])
        self.sig = np.array([o.sigma for o in obs])
        self.k, self.g, self.gap = k, g, gap
        self.t1 = float(self.t.min())
        self.contact_hint = contact_hint  # (u, v, sigma) or None
        self.takeoff_hint = takeoff_hint  # (x, y, sigma) or None
        self.incoming = incoming  # callable t -> (u, v, sigma) or None
        self.time_hint = time_hint  # (t, sigma) or None
        n = len(self.t)
        self.n_total = (
            2 * n
            + (2 if contact_hint else 0)
            + (2 if takeoff_hint else 0)
            + (2 if incoming else 0)
            + (1 if time_hint else 0)
            + (3 if landing else 0)
            + n
            + 1
        )

    def contact_time(self, a: float) -> float:
        return self.t1 - self.gap / (1.0 + math.exp(-max(min(a, 50.0), -50.0)))  # within (t1 - gap, t1)

    def residuals(self, tc: float, p0: np.ndarray, v0: np.ndarray) -> np.ndarray:
        times = self.t if self.landing is None else np.append(self.t, max(self.landing[1], tc))
        states = _integrate(p0, v0, tc, times, self.k, self.g)
        landed = None
        if self.landing is not None:
            states, landed = states[:-1], states[-1]
        self.last_states = states
        if not np.all(np.isfinite(states)):
            self.last_first_depth = math.inf
            return np.full(self.n_total, 1e3)
        cam = self.camera
        depth = cam.depth(states[:, :3])
        self.last_first_depth = float(depth[int(np.argmin(self.t))])
        if np.any(depth < 0.3) or cam.depth(p0)[0] < 0.3:
            return np.full(self.n_total, 1e3)  # behind the camera: not a real flight
        r = [((cam.project(states[:, :3]) - self.uv) / self.sig[:, None]).ravel()]
        if self.contact_hint is not None:
            u, v, s = self.contact_hint
            r.append((cam.project(p0)[0] - (u, v)) / s)
        if self.takeoff_hint is not None:
            x, y, s = self.takeoff_hint
            r.append((p0[:2] - (x, y)) / s)
        if self.incoming is not None:
            u, v, s = self.incoming(tc)
            r.append((cam.project(p0)[0] - (u, v)) / s)
        if self.time_hint is not None:
            t_hint, s = self.time_hint
            r.append([(tc - t_hint) / s])
        if landed is not None:
            # On the floor at the landing spot, at the landing time. The time is only
            # known to within a frame, so the tolerance grows with the speed then.
            where, _, s_t = self.landing
            vel = landed[3:]
            tol = np.array([0.15, 0.15, 0.05]) + np.abs(vel) * s_t
            r.append((landed[:3] - where) / tol)
        # Stay above the floor while it is being watched, and hit it from a sane height.
        r.append(np.minimum(states[:, 2] + 0.05, 0.0) / 0.02)
        r.append([min(p0[2] - 0.2, 0.0) / 0.05 + max(p0[2] - 4.5, 0.0) / 0.05])
        return np.concatenate(r)

    def free(self, x: np.ndarray) -> np.ndarray:
        return self.residuals(self.contact_time(x[0]), x[1:4], x[4:7])

    def fixed(self, tc: float):
        return lambda x: self.residuals(tc, x[0:3], x[3:6])

    def n_obs_residuals(self) -> int:
        return 2 * len(self.t)


def _initial_guesses(problem: _Problem, obs: list[Observation]):
    """Contact points and launch velocities to start the fit from."""
    cam, k, g = problem.camera, problem.k, problem.g
    first, last = obs[0], obs[-1]
    c1, d1 = cam.ray(first.u, first.v)
    cn, dn = cam.ray(last.u, last.v)
    span = last.t - first.t
    if problem.takeoff_hint is not None:
        # Depth where the first ray passes closest above the take-off spot.
        x, y, _ = problem.takeoff_hint
        horiz = d1[:2]
        s = float(np.dot(np.array([x, y]) - c1[:2], horiz) / max(np.dot(horiz, horiz), 1e-9))
        depths = [max(s * f, 0.5) for f in (0.8, 1.0, 1.25)]
    else:
        depths = [2.5, 4.0, 6.0, 9.0, 13.0, 18.0]
    tg = problem.gap / 2
    for D1 in depths:
        P1 = c1 + D1 * d1
        for ratio in (0.4, 0.7, 1.0, 1.4, 2.0, 3.0):
            Pn = cn + D1 * ratio * dn
            disp = Pn - P1 + np.array([0.0, 0.0, 0.5 * g * span**2])
            dist = float(np.linalg.norm(Pn - P1))
            if dist < 0.1 or span <= 0:
                continue
            direction = disp / np.linalg.norm(disp)
            v_first = (math.exp(min(k * dist, 30.0)) - 1.0) / (k * span)  # speed at first sighting
            denom = 1.0 - k * v_first * tg
            v0 = v_first / denom if denom > 0.25 else v_first * 4
            back = math.log1p(k * v0 * tg) / k
            yield np.concatenate([[0.0], P1 - direction * back, direction * v0])


def fit_flight(
    camera: CameraModel,
    observations: list[Observation],
    terminal_velocity: float = 6.7,
    gravity: float = 9.81,
    contact_px: tuple[float, float] | None = None,
    contact_sigma: float = 12.0,
    contact_time: float | None = None,
    takeoff_xy: tuple[float, float] | None = None,
    takeoff_sigma: float = 1.0,
    incoming: list[Observation] | None = None,
    landing_px: tuple[float, float] | None = None,
    landing_time: float | None = None,
    max_gap_s: float | None = None,
    profile_steps: int = 11,
    terminal_velocity_spread: float = 0.2,
) -> FlightFit:
    """Fit a 3D flight to time-stamped pixel observations of one shot.

    Hints (all optional): `contact_px` and `contact_time` for where and when the
    racket met the shuttle; `takeoff_xy`, the smasher's take-off spot on the floor
    (metres); `incoming`, observations of the shuttle on its way in before the hit;
    `landing_px` and `landing_time`, where and when it reached the floor.

    `max_gap_s` bounds how long before the first observation the contact can be:
    by default three frames, or back to the last incoming observation.

    Points at the end of the track that don't fit the flight (the shuttle landed
    or was hit again) are dropped. A first point that doesn't fit is taken to be
    the shuttle still on the racket and used as a contact hint instead.

    The reported ranges cover every contact moment and every distance from the
    camera that fit nearly as well as the best, and a terminal velocity
    `terminal_velocity_spread` m/s either side of the given one (shuttles differ).
    """
    obs = sorted(observations, key=lambda o: o.t)
    if len(obs) < 5:
        raise ValueError("need at least 5 observations")
    dts = np.diff([o.t for o in obs])
    frame = float(np.median(dts[dts > 0])) if np.any(dts > 0) else 1 / 60
    k = gravity / terminal_velocity**2
    hint = (contact_px[0], contact_px[1], contact_sigma) if contact_px is not None else None
    takeoff = (takeoff_xy[0], takeoff_xy[1], takeoff_sigma) if takeoff_xy is not None else None
    time_hint = (contact_time, 0.5 * frame) if contact_time is not None else None
    extrapolate = _incoming_extrapolator(sorted(incoming, key=lambda o: o.t), frame) if incoming else None
    landing = None
    if landing_px is not None and landing_time is not None:
        floor = camera.point_at_height(landing_px[0], landing_px[1], 0.0)
        if floor is None:
            raise ValueError("the landing pixel is above the horizon")
        landing = (floor, landing_time, 0.5 * frame)

    def gap_for(first_t: float) -> float:
        if max_gap_s is not None:
            return max_gap_s
        gap = 3.0 * frame
        if incoming:
            gap = max(gap, first_t - max(o.t for o in incoming) + 0.25 * frame)
        if contact_time is not None:
            gap = max(gap, first_t - contact_time + frame)
        return gap

    def problem_for(points, contact_hint):
        return _Problem(
            camera,
            points,
            k,
            gravity,
            gap_for(points[0].t),
            contact_hint,
            takeoff,
            extrapolate,
            time_hint,
            landing,
        )

    problem = problem_for(obs, hint)
    x, cost = _solve(problem, obs)

    # Drop trailing points that don't belong to this flight: far off it, or at a
    # time when the fitted flight is already below the floor (the shuttle landed).
    while len(obs) > 6:
        res = _point_residuals(problem, x)
        typical = float(np.median(res))
        landed = problem.last_states[-1, 2] < -0.1 and res[-1] > 2.0 * typical
        if res[-1] <= max(4.0, 3.0 * typical) and not landed:
            break
        obs = obs[:-1]
        problem = problem_for(obs, hint)
        x, cost = _solve(problem, obs, x)

    # Is the first point the shuttle still on the racket? It then sits near the
    # contact point, on the flight's path but at the wrong time. Either it doesn't
    # fit at all, or the fit can only take it in by bending the rest of the
    # flight. Compare with a fit that leaves it out.
    racket = False
    all_obs = obs
    if len(obs) > 6:
        res = _point_residuals(problem, x)
        rest_problem = problem_for(obs[1:], hint)
        x_rest, cost_rest = _solve(rest_problem, obs[1:])
        cost_with = float(np.sum(res[1:] ** 2))  # the same points, in the fit that kept the first
        noise = max(1.0, cost_rest / max(rest_problem.n_obs_residuals() - 7, 1))
        misfit = res[0] > max(4.0, 3.0 * float(np.median(res[1:])))
        if misfit or cost_with - cost_rest > 9.0 * noise:
            racket = hint is None
            hint = hint or (obs[0].u, obs[0].v, 3.0 * obs[0].sigma)
            obs = obs[1:]
            problem = problem_for(obs, hint)
            x, cost = _solve(problem, obs, x_rest)

    fit = _summarize(problem, x, cost, profile_steps, terminal_velocity_spread)
    fit.observations = list(all_obs)
    fit.first_point_was_racket = racket
    return fit


def calibrate_terminal_velocity(
    camera: CameraModel,
    observations: list[Observation],
    known_kmh: float,
    terminal_velocity: float = 6.7,
    tolerance_kmh: float = 0.5,
    bounds: tuple[float, float] = (4.0, 10.0),
    **fit_kwargs,
) -> tuple[float, FlightFit]:
    """The shuttle's terminal velocity that makes this shot's launch speed `known_kmh`.

    For a shot whose speed you trust from elsewhere: a radar gun, or a broadcast
    reading. Shuttles differ (feathers or nylon, wear, the air in the hall), and the
    terminal velocity sets how much speed the fit assumes was lost before the first
    sighting. Less drag, a lower launch speed for the same track. The launch speed
    falls steadily as the terminal velocity rises, so a bracketed search finds the
    match.

    `fit_kwargs` go to `fit_flight` (incoming track, contact, take-off, landing...).
    Returns the terminal velocity, rounded to 0.01 m/s, and the full fit at it.
    """
    lo_b, hi_b = bounds

    def miss(vt: float) -> float:
        # The search only needs the best fit: skip the ranges until the end.
        fit = fit_flight(
            camera,
            observations,
            terminal_velocity=vt,
            profile_steps=0,
            terminal_velocity_spread=0.0,
            **fit_kwargs,
        )
        return fit.speed_kmh - known_kmh

    a = min(max(float(terminal_velocity), lo_b), hi_b)
    fa = miss(a)
    best_vt, best_miss = a, fa
    if abs(fa) > tolerance_kmh:
        # Bracket the answer. Reading too fast means too much drag was assumed.
        step = 0.25 if fa > 0 else -0.25
        b, fb = a, fa
        while fa * fb > 0:
            nxt = min(max(b + step, lo_b), hi_b)
            if nxt == b:
                raise ValueError(
                    f"no terminal velocity between {lo_b:g} and {hi_b:g} m/s gives {known_kmh:.0f} km/h"
                )
            a, fa = b, fb
            b, fb = nxt, miss(nxt)
            step *= 2
        if abs(fb) < abs(best_miss):
            best_vt, best_miss = b, fb
        # Regula falsi, Illinois variant: halving a stuck end keeps it from stalling.
        side = 0
        for _ in range(20):
            if abs(best_miss) <= tolerance_kmh or abs(b - a) < 1e-3:
                break
            c = (fa * b - fb * a) / (fa - fb)
            fc = miss(c)
            if abs(fc) < abs(best_miss):
                best_vt, best_miss = c, fc
            if fc * fb > 0:  # c is on b's side
                b, fb = c, fc
                if side == -1:
                    fa /= 2
                side = -1
            elif fc * fa > 0:  # c is on a's side
                a, fa = c, fc
                if side == 1:
                    fb /= 2
                side = 1
            else:
                break
    vt = round(best_vt, 2)
    return vt, fit_flight(camera, observations, terminal_velocity=vt, **fit_kwargs)


def _incoming_extrapolator(incoming: list[Observation], frame: float):
    """Where the incoming shuttle would be at time t, with an uncertainty that grows
    the further past its last sighting we extrapolate."""
    pts = incoming[-10:]
    # A last point that bends away from the rest is usually the shuttle already hit.
    while len(pts) >= 5:
        t = np.array([o.t for o in pts[:-1]])
        cu = np.polyfit(t, [o.u for o in pts[:-1]], 2)
        cv = np.polyfit(t, [o.v for o in pts[:-1]], 2)
        miss = np.hypot(np.polyval(cu, pts[-1].t) - pts[-1].u, np.polyval(cv, pts[-1].t) - pts[-1].v)
        if miss <= 3.0 * pts[-1].sigma + 2.0:
            break
        pts = pts[:-1]
    t_last = pts[-1].t
    t = np.array([o.t - t_last for o in pts])
    deg = 2 if len(pts) >= 4 else 1
    cu = np.polyfit(t, [o.u for o in pts], deg)
    cv = np.polyfit(t, [o.v for o in pts], deg)
    base = max(o.sigma for o in pts)

    def at(tc: float):
        dt = tc - t_last
        return float(np.polyval(cu, dt)), float(np.polyval(cv, dt)), base + 2.0 + 1.5 * max(dt, 0.0) / frame

    return at


def _point_residuals(problem: _Problem, x: np.ndarray) -> np.ndarray:
    """Reprojection error of each observation, in its own standard deviations."""
    r = problem.free(x)[: problem.n_obs_residuals()].reshape(-1, 2)
    return np.hypot(r[:, 0], r[:, 1])


def _solve(problem: _Problem, obs: list[Observation], start: np.ndarray | None = None):
    """Best fit: from `start` if given, else from the four most promising guesses."""
    if start is not None:
        candidates = [start]
    else:
        guesses = sorted(
            (
                (float(np.sum(problem.free(x0) ** 2)), i, x0)
                for i, x0 in enumerate(_initial_guesses(problem, obs))
            ),
            key=lambda c: c[:2],
        )
        candidates = [x0 for _, _, x0 in guesses[:4]]
    best_x, best_cost = None, math.inf
    for x0 in candidates:
        x, cost = _levenberg_marquardt(problem.free, x0)
        if cost < best_cost:
            best_x, best_cost = x, cost
    if best_x is None:
        raise RuntimeError("could not fit a flight to these observations")
    return best_x, best_cost


def _summarize(problem: _Problem, best_x: np.ndarray, best_cost: float, profile_steps: int, vt_spread: float):
    camera, k, g = problem.camera, problem.k, problem.g
    tc = problem.contact_time(best_x[0])
    p0, v0 = best_x[1:4], best_x[4:7]
    t1 = problem.t1
    n_res = problem.n_obs_residuals()
    scale = max(1.0, best_cost / max(n_res - 7, 1))
    launch, first_seen = [], []

    def record(x_p0, x_v0, t_contact, kk=k):
        launch.append(np.linalg.norm(x_v0) * KMH)
        s1 = _integrate(x_p0, x_v0, t_contact, np.array([t1]), kk, g)[0]
        first_seen.append(np.linalg.norm(s1[3:]) * KMH)

    record(p0, v0, tc)

    # Profile over the contact moment: refit with it fixed across the window, and
    # keep every moment whose fit is nearly as good as the best.
    x_prev = np.concatenate([p0, v0])
    for frac in np.linspace(0.03, 0.97, profile_steps):
        tcf = t1 - problem.gap * frac
        x, cost = _levenberg_marquardt(problem.fixed(tcf), x_prev, max_iter=40)
        if cost <= best_cost + 4.0 * scale:
            record(x[0:3], x[3:6], tcf)

    # Profile over depth, a single camera's weakest direction: pin the distance from
    # the camera at the first sighting and refit the rest. Scaling a flight about
    # the camera leaves its image unchanged, which gives each refit a good start.
    centre = camera.position
    s_first = _integrate(p0, v0, tc, np.array([t1]), k, g)[0]
    d_best = float(camera.depth(s_first[None, :3])[0])
    depth_ok = [1.0]
    for ratio in (0.6, 0.72, 0.85, 0.93, 1.07, 1.18, 1.35, 1.6):
        target = d_best * ratio

        def pinned(x, target=target):
            r = problem.free(x)
            return np.append(r, (problem.last_first_depth - target) / (0.002 * target))

        x0 = np.concatenate([[best_x[0]], centre + ratio * (p0 - centre), ratio * v0])
        x, cost = _levenberg_marquardt(pinned, x0, max_iter=30)
        if cost - pinned(x)[-1] ** 2 <= best_cost + 4.0 * scale:
            depth_ok.append(ratio)
            record(x[1:4], x[4:7], problem.contact_time(x[0]))

    # Shuttles differ: refit with a slightly different terminal velocity each way.
    vt = math.sqrt(g / k)
    for vt_alt in (vt - vt_spread, vt + vt_spread) if vt_spread > 0 else ():
        alt = _Problem(
            camera,
            problem.observations,
            g / vt_alt**2,
            g,
            problem.gap,
            problem.contact_hint,
            problem.takeoff_hint,
            problem.incoming,
            problem.time_hint,
            problem.landing,
        )
        x, _ = _levenberg_marquardt(alt.free, best_x, max_iter=30)
        record(x[1:4], x[4:7], alt.contact_time(x[0]), alt.k)

    r = problem.residuals(tc, p0, v0)[:n_res].reshape(-1, 2) * problem.sig[:, None]
    rms = float(np.sqrt(np.mean(np.sum(r**2, axis=1))))
    # How much the shuttle's distance from the camera changes over the tracked
    # flight. Perspective is what lets one camera judge depth: a camera far away
    # (a broadcast camera 30 m back with a long lens) sees little change, and then
    # depth rests on physics and hints.
    dist = np.linalg.norm(problem.last_states[:, :3] - camera.position, axis=1)
    distance_ratio = float(dist.max() / max(dist.min(), 1e-6))

    # Landing: carry on from the last observation until it reaches the floor.
    landing, landing_t = None, None
    times = float(problem.t.max()) + np.arange(0.0, 1.5, 0.002)
    traj = _integrate(p0, v0, tc, times, k, g)
    below = np.nonzero(traj[:, 2] <= 0.0)[0]
    if len(below):
        i = int(below[0])
        if i == 0:
            landing, landing_t = traj[0, :3], float(times[0])
        else:
            a = traj[i - 1, 2] / (traj[i - 1, 2] - traj[i, 2])
            landing = traj[i - 1, :3] + a * (traj[i, :3] - traj[i - 1, :3])
            landing_t = float(times[i - 1] + a * (times[i] - times[i - 1]))

    return FlightFit(
        contact_time=tc,
        contact=np.asarray(p0, float),
        velocity=np.asarray(v0, float),
        speed_kmh=float(np.linalg.norm(v0) * KMH),
        speed_range_kmh=(float(min(launch)), float(max(launch))),
        first_seen_time=t1,
        first_seen_kmh=float(first_seen[0]),
        first_seen_range_kmh=(float(min(first_seen)), float(max(first_seen))),
        rms_px=rms,
        landing=landing,
        landing_time=landing_t,
        k=k,
        gravity=g,
        depth_range=(min(depth_ok), max(depth_ok)),
        distance_ratio=distance_ratio,
    )

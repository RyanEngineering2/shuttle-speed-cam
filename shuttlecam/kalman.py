"""Kalman filtering for a badminton shuttle.

A shuttle is not a ball. Its feathers give it huge drag for its mass, so it slows
down extremely fast: a 250 km/h smash loses about a third of its speed in the
first 33 ms. A constant-velocity Kalman filter treats that deceleration as noise
and lags behind. Here the motion model includes the physics instead:

    dv/dt = -k |v| v - g y_hat,      k = g / v_t^2

where v_t is the terminal velocity (about 6.7 m/s). The model is nonlinear, so
this is an extended Kalman filter (EKF): the state is propagated with RK4 and the
covariance with the Jacobian of the dynamics.

State: [x, y, vx, vy] in metres and m/s, in the vertical plane of flight
(x along the court, y up).

After a shot ends, a Rauch-Tung-Striebel (RTS) smoother runs backwards over the
whole track. The estimate at each point then uses every measurement, before and
after it. That matters most at the start of the track, where the shuttle is
fastest and the forward filter has had the least data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

H = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])


class DragModel:
    """Quadratic air drag plus gravity."""

    def __init__(self, terminal_velocity: float = 6.7, gravity: float = 9.81, max_step: float = 2.5e-3):
        self.k = gravity / terminal_velocity**2
        self.g = gravity
        self.max_step = max_step

    def deriv(self, s: np.ndarray) -> np.ndarray:
        vx, vy = s[2], s[3]
        speed = math.hypot(vx, vy)
        return np.array([vx, vy, -self.k * speed * vx, -self.k * speed * vy - self.g])

    def jacobian(self, s: np.ndarray) -> np.ndarray:
        """d(deriv)/d(state)."""
        vx, vy = s[2], s[3]
        speed = max(math.hypot(vx, vy), 1e-9)
        k = self.k
        a = np.zeros((4, 4))
        a[0, 2] = a[1, 3] = 1.0
        a[2, 2] = -k * (speed + vx * vx / speed)
        a[2, 3] = a[3, 2] = -k * vx * vy / speed
        a[3, 3] = -k * (speed + vy * vy / speed)
        return a

    def propagate(self, s: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
        """State after `dt` seconds, and the transition Jacobian F."""
        n = max(1, math.ceil(abs(dt) / self.max_step))
        h = dt / n
        F = np.eye(4)
        for _ in range(n):
            F = (np.eye(4) + self.jacobian(s) * h) @ F
            k1 = self.deriv(s)
            k2 = self.deriv(s + 0.5 * h * k1)
            k3 = self.deriv(s + 0.5 * h * k2)
            k4 = self.deriv(s + h * k3)
            s = s + (h / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)
        return s, F

    def drag_accel(self, s: np.ndarray) -> float:
        return self.k * (s[2] ** 2 + s[3] ** 2)


class ConstantVelocityModel:
    """Straight lines at constant speed. Only here as a baseline to compare against."""

    def propagate(self, s: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        return F @ s, F

    def drag_accel(self, s: np.ndarray) -> float:
        return 0.0


@dataclass
class Step:
    """One filter step, kept for the smoother."""

    t: float
    x_pred: np.ndarray
    P_pred: np.ndarray
    F: np.ndarray | None  # transition from the previous step (None for the first)
    x: np.ndarray  # filtered state (equals the prediction if there was no measurement)
    P: np.ndarray
    measured: bool


class EKF:
    def __init__(
        self,
        model,
        x0: np.ndarray,
        P0: np.ndarray,
        t0: float,
        accel_noise: float,
        drag_uncertainty: float,
    ):
        self.model = model
        self.x = np.asarray(x0, float)
        self.P = np.asarray(P0, float)
        self.t = t0
        self.accel_noise = accel_noise
        self.drag_uncertainty = drag_uncertainty
        self.steps = [Step(t0, self.x, self.P, None, self.x, self.P, True)]

    def process_noise(self, dt: float) -> np.ndarray:
        """Discrete white-noise acceleration, scaled up while drag is large."""
        sigma = self.accel_noise + self.drag_uncertainty * self.model.drag_accel(self.x)
        q = sigma**2
        Q = np.zeros((4, 4))
        Q[0, 0] = Q[1, 1] = dt**4 / 4
        Q[0, 2] = Q[2, 0] = Q[1, 3] = Q[3, 1] = dt**3 / 2
        Q[2, 2] = Q[3, 3] = dt**2
        return q * Q

    def predict(self, t: float) -> None:
        dt = t - self.t
        if dt <= 0:
            return
        Q = self.process_noise(dt)
        self.x, F = self.model.propagate(self.x, dt)
        self.P = F @ self.P @ F.T + Q
        self.t = t
        self.steps.append(Step(t, self.x, self.P, F, self.x, self.P, False))

    def innovation(self, z: np.ndarray, R: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return z - H @ self.x, H @ self.P @ H.T + R

    def distance2(self, z: np.ndarray, R: np.ndarray) -> float:
        """Squared Mahalanobis distance of a measurement from the prediction."""
        y, S = self.innovation(z, R)
        return float(y @ np.linalg.solve(S, y))

    def update(self, z: np.ndarray, R: np.ndarray) -> None:
        y, S = self.innovation(z, R)
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ y
        # Joseph form keeps P symmetric and positive definite.
        I_KH = np.eye(4) - K @ H
        self.P = I_KH @ self.P @ I_KH.T + K @ R @ K.T
        last = self.steps[-1]
        last.x, last.P, last.measured = self.x, self.P, True

    @property
    def speed(self) -> float:
        return math.hypot(self.x[2], self.x[3])


def rts_smooth(steps: list[Step]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Rauch-Tung-Striebel smoother over a finished track."""
    n = len(steps)
    xs = [s.x for s in steps]
    Ps = [s.P for s in steps]
    for i in range(n - 2, -1, -1):
        nxt = steps[i + 1]
        C = steps[i].P @ nxt.F.T @ np.linalg.inv(nxt.P_pred)
        xs[i] = steps[i].x + C @ (xs[i + 1] - nxt.x_pred)
        Ps[i] = steps[i].P + C @ (Ps[i + 1] - nxt.P_pred) @ C.T
    return xs, Ps

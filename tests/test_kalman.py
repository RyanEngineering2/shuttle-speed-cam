import math

import numpy as np
import pytest

from shuttlecam.kalman import EKF, ConstantVelocityModel, DragModel, rts_smooth


def test_drag_model_matches_closed_form_without_gravity():
    # With g = 0 the 1-D solution is v(t) = v0 / (1 + k v0 t), x(t) = ln(1 + k v0 t) / k.
    model = DragModel(terminal_velocity=6.7, gravity=9.81)
    k = model.k
    model.g = 0.0
    v0 = 250 / 3.6
    for t in (0.005, 0.033, 0.1, 0.3):
        s, _ = model.propagate(np.array([0.0, 0.0, v0, 0.0]), t)
        assert s[2] == pytest.approx(v0 / (1 + k * v0 * t), rel=1e-6)
        assert s[0] == pytest.approx(math.log(1 + k * v0 * t) / k, rel=1e-6)


def test_drag_model_reaches_terminal_velocity_falling():
    model = DragModel(terminal_velocity=6.7)
    s, _ = model.propagate(np.array([0.0, 10.0, 0.0, 0.0]), 5.0)
    assert s[3] == pytest.approx(-6.7, abs=1e-3)


@pytest.mark.parametrize("dt", [0.005, 0.0333])
def test_transition_jacobian_matches_finite_differences(dt):
    model = DragModel()
    s0 = np.array([0.3, 2.0, 50.0, -12.0])
    _, F = model.propagate(s0, dt)
    numeric = np.zeros((4, 4))
    eps = 1e-5
    for j in range(4):
        d = np.zeros(4)
        d[j] = eps
        numeric[:, j] = (model.propagate(s0 + d, dt)[0] - model.propagate(s0 - d, dt)[0]) / (2 * eps)
    # F is a first-order product over substeps, so allow a small mismatch.
    assert np.allclose(F, numeric, atol=0.02 * max(1.0, dt * 100))


def test_constant_velocity_model():
    s, F = ConstantVelocityModel().propagate(np.array([1.0, 2.0, 3.0, -4.0]), 0.5)
    assert np.allclose(s, [2.5, 0.0, 3.0, -4.0])
    assert F[0, 2] == F[1, 3] == 0.5


def _noisy_track(v0_kmh=300, fps=200, n=40, sigma=0.02, seed=0):
    truth_model = DragModel(terminal_velocity=6.5)
    rng = np.random.default_rng(seed)
    a = math.radians(-10)
    s = np.array([0.0, 2.5, v0_kmh / 3.6 * math.cos(a), v0_kmh / 3.6 * math.sin(a)])
    states, zs = [], []
    for i in range(n):
        states.append(s)
        zs.append(s[:2] + rng.normal(0, sigma, 2))
        s, _ = truth_model.propagate(s, 1 / fps)
    return states, zs


def test_smoother_beats_filter_at_the_start_of_the_track():
    fps, sigma = 200, 0.02
    errors_filter, errors_smooth = [], []
    for seed in range(10):
        states, zs = _noisy_track(fps=fps, sigma=sigma, seed=seed)
        R = np.eye(2) * sigma**2
        v_guess = (zs[1] - zs[0]) * fps
        P0 = np.diag([sigma**2, sigma**2, 30.0**2, 30.0**2])
        ekf = EKF(DragModel(6.7), np.r_[zs[0], v_guess], P0, 0.0, 30.0, 0.25)
        filtered_v = []
        for i, z in enumerate(zs[1:], 1):
            ekf.predict(i / fps)
            ekf.update(z, R)
            filtered_v.append(ekf.speed)
        xs, _ = rts_smooth(ekf.steps)
        true_v = math.hypot(*states[1][2:])
        errors_filter.append(abs(filtered_v[0] - true_v))
        errors_smooth.append(abs(math.hypot(*xs[1][2:]) - true_v))
    assert np.mean(errors_smooth) < 0.5 * np.mean(errors_filter)
    # And the smoothed estimate is good in absolute terms.
    v0 = 300 / 3.6
    assert np.mean(errors_smooth) < 0.02 * v0
    assert max(errors_smooth) < 0.06 * v0

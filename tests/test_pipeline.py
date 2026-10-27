"""End to end on simulated clips, where the true speed is known."""

import pytest

from shuttlecam.config import Config
from shuttlecam.pipeline import analyze
from shuttlecam.sim import SmashScene


def run(scene, **tracker):
    cfg = Config()
    for key, value in tracker.items():
        setattr(cfg.tracker, key, value)
    return analyze(scene.frames(), cfg, scene.calibration())


def true_speed_at_peak(scene, shot):
    """True speed when the tracker says the shuttle had its reported speed."""
    return scene.true_speed_kmh(max(shot.t_peak + scene.exposure_s / 2, scene.t_hit))


@pytest.mark.parametrize("speed", [150, 250, 350])
def test_measures_smash_speed_at_200_fps(speed):
    scene = SmashScene(speed_kmh=speed, fps=200, duration=0.35)
    shots = run(scene)
    assert len(shots) == 1
    shot = shots[0]
    assert shot.speed_kmh == pytest.approx(true_speed_at_peak(scene, shot), rel=0.03)
    assert shot.speed_kmh == pytest.approx(speed, rel=0.04)


@pytest.mark.parametrize("hit", [0.0802, 0.0815, 0.0829])
def test_hit_between_frames(hit):
    # The hit lands between frames 16 (0.080 s) and 17 (0.085 s), so the track's
    # first point is the shuttle still on the racket. It must anchor the start of
    # the flight without dragging the speed down.
    scene = SmashScene(speed_kmh=280, fps=200, duration=0.35, t_hit=hit)
    (shot,) = run(scene)
    assert shot.t_start < hit
    assert shot.speed_kmh == pytest.approx(true_speed_at_peak(scene, shot), rel=0.03)
    assert shot.speed_kmh == pytest.approx(280, rel=0.04)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_hit_between_frames_at_120_fps(seed):
    scene = SmashScene(speed_kmh=150, fps=120, duration=0.35, seed=seed)
    (shot,) = run(scene)
    assert shot.speed_kmh == pytest.approx(true_speed_at_peak(scene, shot), rel=0.03)


def test_30_fps_sees_the_shuttle_too_late():
    # Why the ESP32 version was dropped: at 30 fps the first usable frame comes up
    # to 33 ms after the hit, when a 250 km/h smash is already well below 200 km/h.
    scene = SmashScene(speed_kmh=250, fps=30, duration=0.5)
    (shot,) = run(scene)
    seen = true_speed_at_peak(scene, shot)
    assert shot.speed_kmh == pytest.approx(seen, rel=0.03)  # right about what it saw...
    assert shot.speed_kmh < 0.8 * 250  # ...but it saw the shuttle too late.
    assert shot.raw_speed_kmh < shot.speed_kmh  # and plain frame differences are worse


def test_drag_model_beats_constant_velocity():
    scene = SmashScene(speed_kmh=250, fps=120, duration=0.35)
    frames = list(scene.frames())
    cal = scene.calibration()
    drag = Config()
    cv = Config()
    cv.tracker.model = "cv"
    cv.tracker.accel_noise = 500.0
    (drag_shot,) = analyze(frames, drag, cal)
    (cv_shot,) = analyze(frames, cv, cal)
    assert abs(drag_shot.speed_kmh - 250) < abs(cv_shot.speed_kmh - 250)


def test_no_shot_without_a_smash():
    # Only a slow shuttle drifting down and a player swaying.
    scene = SmashScene(speed_kmh=25, fps=200, duration=0.35)
    assert run(scene) == []

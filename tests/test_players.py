"""Player-aware detection and the jagged-track check, for crowded match footage."""

import cv2
import numpy as np
import pytest

from shuttlecam.calibration import Calibration
from shuttlecam.config import Config, DetectorConfig, TrackerConfig
from shuttlecam.detector import Detection, MotionDetector
from shuttlecam.pipeline import analyze
from shuttlecam.sim import SmashScene
from shuttlecam.tracker import Tracker

# -- detector ----------------------------------------------------------------------


def frames_with_player(margin):
    """A big blob (a lunging player) with a small blob (a hand) beside it, and a
    small blob (a shuttle) far away. Returns the detections of the last frame."""
    rng = np.random.default_rng(0)
    bg = np.clip(cv2.GaussianBlur(rng.normal(90, 30, (240, 320)), (0, 0), 5), 0, 255)
    det = MotionDetector(DetectorConfig(player_area=300, player_margin=margin))
    found = []
    for i in range(6):
        img = bg.copy()
        px = 60 + 12 * i
        cv2.ellipse(img, (px, 150), (14, 40), 0, 0, 360, 200, -1)  # player
        cv2.circle(img, (px + 22, 115 - 3 * (i % 2)), 2, 240, -1)  # hand, right next to it
        cv2.circle(img, (200 + 15 * i, 60), 2, 240, -1)  # shuttle, far away
        found = det(np.clip(img, 0, 255).astype(np.uint8))
    return found


def test_small_blobs_next_to_a_player_are_flagged():
    found = frames_with_player(margin=10)
    hand = [d for d in found if d.x < 160]
    shuttle = [d for d in found if d.x > 160]
    assert hand and all(d.near_player for d in hand)
    assert len(shuttle) == 1 and not shuttle[0].near_player


def test_player_mode_off_flags_nothing():
    assert not any(d.near_player for d in frames_with_player(margin=0))


# -- tracker -----------------------------------------------------------------------

FPS = 100.0
CAL = Calibration.from_scale(0.01, (1000, 1000))  # 1 px = 1 cm, y up


def track(points, near_player=lambda i: False, **cfg):
    """Feed one detection per frame at the given pixel positions."""
    tracker = Tracker(TrackerConfig(model="cv", accel_noise=50.0, **cfg), CAL)
    shots = []
    for i, (x, y) in enumerate(points):
        shots += tracker.step(i / FPS, [Detection(x, y, 6, 100.0, near_player(i))])
    return shots + tracker.flush()


def straight(n=20, speed_px=25.0):
    # 25 px per frame at 100 fps = 25 m/s = 90 km/h
    return [(100 + speed_px * i, 500.0) for i in range(n)]


def test_smooth_track_is_kept():
    (shot,) = track(straight())
    assert shot.jitter < 0.3
    assert shot.speed_kmh == pytest.approx(90, rel=0.02)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_jagged_track_is_rejected(seed):
    # Moves like a shuttle on average, but wobbles by +-5 px from frame to frame,
    # far more than the 1.5 px detector noise: an arm or a racket, not a shuttle.
    rng = np.random.default_rng(seed)
    wobbly = [(x + rng.uniform(-5, 5), y + rng.uniform(-5, 5)) for x, y in straight(24)]
    (shot,) = track(wobbly, max_jitter=99)  # it is tracked...
    assert shot.jitter > 1.2
    assert track(wobbly) == []  # ...but rejected as too jagged for a shuttle


def test_near_player_detections_cannot_start_a_track():
    assert track(straight(), near_player=lambda i: True) == []


def test_near_player_detections_can_continue_a_track():
    # Starts in the open, then passes a player for the rest of the flight.
    (shot,) = track(straight(), near_player=lambda i: i >= 2)
    assert shot.n_points == 20


def test_track_can_start_from_the_racket():
    # The first point sits next to the player (the shuttle on the racket); the
    # second is in the open. The pair may start a track, keeping the hit point.
    (shot,) = track(straight(), near_player=lambda i: i == 0)
    assert shot.n_points == 20


# -- end to end --------------------------------------------------------------------


@pytest.mark.parametrize("speed", [150, 300])
def test_player_mode_does_not_change_smash_speed(speed):
    scene = SmashScene(speed_kmh=speed, fps=200, duration=0.35)
    frames = list(scene.frames())
    plain = Config()
    players = Config()
    players.detector.player_margin = 12
    players.tracker.max_jitter = 0.6
    (a,) = analyze(frames, plain, scene.calibration())
    (b,) = analyze(frames, players, scene.calibration())
    assert b.speed_kmh == pytest.approx(a.speed_kmh, rel=0.01)

import cv2
import numpy as np
import pytest

from shuttlecam.config import DetectorConfig
from shuttlecam.detector import MotionDetector


def scene(seed=0):
    rng = np.random.default_rng(seed)
    bg = cv2.GaussianBlur(rng.normal(90, 30, (240, 320)), (0, 0), 5)
    return np.clip(bg, 0, 255)


def frame(bg, dot=None, gain=1.0):
    img = bg.copy()
    if dot is not None:
        cv2.circle(img, (int(dot[0]), int(dot[1])), 2, 235, -1)
    return np.clip(img * gain, 0, 255).astype(np.uint8)


def test_finds_a_moving_shuttle_where_it_is_now():
    bg = scene()
    det = MotionDetector(DetectorConfig())
    path = [(40 + 12 * i, 60 + 3 * i) for i in range(8)]
    for p in path[:-1]:
        det(frame(bg, p))
    found = det(frame(bg, path[-1]))
    assert len(found) == 1
    assert (found[0].x, found[0].y) == pytest.approx(path[-1], abs=1.0)


def test_ignores_global_flicker():
    bg = scene()
    det = MotionDetector(DetectorConfig())
    for i in range(10):
        assert det(frame(bg, gain=1.0 + 0.05 * (-1) ** i)) == []


def test_no_ghost_where_a_slow_shuttle_used_to_be():
    # A shuttle drifting 1 px per frame sits on the same pixels for several frames.
    # When it leaves, the empty spot must not be reported.
    bg = scene()
    det = MotionDetector(DetectorConfig())
    for i in range(20):
        det(frame(bg, (100 + i, 100)))
    assert det(frame(bg)) == []


def test_ignore_rects_mask_out_regions():
    bg = scene()
    cfg = DetectorConfig(ignore_rects=[[0, 0, 160, 240]])
    det = MotionDetector(cfg)
    for x in range(20, 140, 15):
        assert det(frame(bg, (x, 120))) == []


def test_big_blobs_are_rejected():
    bg = scene()
    det = MotionDetector(DetectorConfig(max_area=100))
    for i in range(5):
        img = bg.copy()
        cv2.circle(img, (60 + 30 * i, 120), 15, 240, -1)  # a player-sized blob
        assert det(np.clip(img, 0, 255).astype(np.uint8)) == []

import numpy as np
import pytest

from shuttlecam.calibration import Calibration

IMAGE = [(102, 400), (99, 240), (330, 410), (335, 248)]
WORLD = [(0, 0), (0, 2), (1.98, 0), (1.98, 2)]


def test_homography_maps_the_clicked_points():
    cal = Calibration.from_points(IMAGE, WORLD, (640, 480))
    for (u, v), (x, y) in zip(IMAGE, WORLD):
        assert cal.to_world(u, v) == pytest.approx([x, y], abs=1e-4)
        assert cal.to_image(x, y) == pytest.approx([u, v], abs=1e-4)


def test_needs_four_points():
    with pytest.raises(ValueError):
        Calibration.from_points(IMAGE[:3], WORLD[:3], (640, 480))


def test_scale_calibration_flips_y_up():
    cal = Calibration.from_scale(0.01, (640, 480))
    assert cal.to_world(0, 480) == pytest.approx([0, 0])
    assert cal.to_world(100, 380) == pytest.approx([1.0, 1.0])


def test_other_frame_size_gives_the_same_world_point():
    cal = Calibration.from_points(IMAGE, WORLD, (640, 480))
    small = cal.for_frame_size(320, 240)
    assert small.to_world(330 / 2, 410 / 2) == pytest.approx(cal.to_world(330, 410))


def test_jacobian_matches_finite_differences():
    cal = Calibration.from_points(IMAGE, WORLD, (640, 480))
    u, v, e = 200.0, 300.0, 1e-4
    numeric = np.column_stack(
        [
            (cal.to_world(u + e, v) - cal.to_world(u - e, v)) / (2 * e),
            (cal.to_world(u, v + e) - cal.to_world(u, v - e)) / (2 * e),
        ]
    )
    assert cal.jacobian(u, v) == pytest.approx(numeric, rel=1e-5)


def test_save_and_load(tmp_path):
    cal = Calibration.from_points(IMAGE, WORLD, (640, 480))
    path = tmp_path / "calibration.json"
    cal.save(path)
    loaded = Calibration.load(path)
    assert loaded.image_size == (640, 480)
    assert np.allclose(loaded.H, cal.H)

"""3D mode: court calibration, the 3D flight fit, and the match-footage pipeline.

Everything here runs on a synthetic smash filmed from behind the baseline
(sim.Smash3DScene), where the true flight is known exactly.
"""

import numpy as np
import pytest

from shuttlecam.analyze3d import ImageTrack, analyze_3d, split_at_hits
from shuttlecam.camera3d import CameraModel, calibrate_camera
from shuttlecam.cli import main
from shuttlecam.config import Analyze3DConfig, load_config
from shuttlecam.court import landmarks
from shuttlecam.fit3d import Observation, fit_flight
from shuttlecam.sim import Smash3DScene

# -- court and camera ----------------------------------------------------------------


def test_landmarks():
    pts = landmarks()
    assert pts["near_base/left_doubles"] == (-3.05, -6.70, 0.0)
    assert pts["far_short/centre"] == (0.0, 1.98, 0.0)
    assert pts["post_right_top"] == (3.05, 0.0, 1.55)
    assert len(pts) == 6 * 5 + 5


def test_calibration_recovers_the_camera():
    scene = Smash3DScene(size=(1920, 1080))
    clicked = scene.landmark_pixels(noise_px=1.0, seed=3)
    cal = calibrate_camera(clicked, scene.size)
    assert cal.camera.f == pytest.approx(scene.camera.f, rel=0.01)
    assert np.allclose(cal.camera.position, scene.camera.position, atol=0.1)
    assert cal.rms_px < 2.0


def test_calibration_needs_known_landmarks():
    with pytest.raises(ValueError, match="unknown landmark"):
        calibrate_camera({"halfway/centre": (1, 2)}, (1920, 1080))
    with pytest.raises(ValueError, match="at least 6"):
        calibrate_camera({"near_base/centre": (1, 2)}, (1920, 1080))


def test_camera_save_load_and_rescale(tmp_path):
    cam = Smash3DScene().camera
    cam.save(tmp_path / "camera.json")
    loaded = CameraModel.load(tmp_path / "camera.json")
    p = np.array([[1.0, 2.0, 1.5]])
    assert np.allclose(loaded.project(p), cam.project(p))
    half = loaded.for_frame_size(960, 540)
    assert np.allclose(half.project(p), cam.project(p) / 2)


# -- the 3D fit ---------------------------------------------------------------------


def observe(scene, times, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for t in times:
        u, v = scene.camera.project([scene.truth(t)[:3]])[0] + rng.normal(0, noise, 2)
        out.append(Observation(float(t), float(u), float(v), 1.5))
    return out


def flight_frames(scene, first_frame_after_hit=1, to_landing=True):
    t_land = scene.landing[0]
    frames = np.arange(0, 120) / scene.fps
    out = frames[frames > scene.t_hit][first_frame_after_hit - 1 :]
    return out[out < t_land] if to_landing else out


def incoming_frames(scene):
    frames = np.arange(0, 120) / scene.fps
    return frames[frames < scene.t_hit]


def test_fit_recovers_the_true_flight():
    scene = Smash3DScene(size=(960, 540))
    obs = observe(scene, flight_frames(scene, first_frame_after_hit=2))
    inc = observe(scene, incoming_frames(scene))
    fit = fit_flight(scene.camera, obs, terminal_velocity=6.5, incoming=inc)
    assert fit.speed_kmh == pytest.approx(scene.speed_kmh, rel=0.01)
    assert fit.contact_time == pytest.approx(scene.t_hit, abs=0.001)
    assert np.allclose(fit.contact, scene.contact, atol=0.05)
    assert fit.rms_px < 0.1


@pytest.mark.parametrize("speed, seed", [(320, 1), (220, 2)])
def test_ranges_cover_the_truth_with_noise_and_the_wrong_drag(speed, seed):
    # The filter assumes 6.7 m/s; the simulated shuttle has 6.5 m/s.
    scene = Smash3DScene(size=(960, 540), speed_kmh=speed, seed=seed)
    obs = observe(scene, flight_frames(scene, first_frame_after_hit=2), noise=1.0, seed=seed)
    inc = observe(scene, incoming_frames(scene), noise=1.0, seed=seed + 10)
    fit = fit_flight(scene.camera, obs, incoming=inc)
    lo, hi = fit.speed_range_kmh
    assert lo <= speed <= hi
    assert hi / lo < 1.25
    flo, fhi = fit.first_seen_range_kmh
    assert flo * 0.98 <= scene.true_speed_kmh(fit.first_seen_time) <= fhi * 1.02


def test_a_few_points_after_landing_dont_spoil_the_fit():
    # The detector sees the shuttle for a frame or two after it lands, then it stops
    # moving and disappears.
    scene = Smash3DScene(size=(960, 540))
    flight = flight_frames(scene, first_frame_after_hit=2)
    landed = flight[-1] + np.arange(1, 4) / scene.fps
    obs = observe(scene, np.concatenate([flight, landed]))
    inc = observe(scene, incoming_frames(scene))
    fit = fit_flight(scene.camera, obs, terminal_velocity=6.5, incoming=inc)
    assert fit.speed_kmh == pytest.approx(scene.speed_kmh, rel=0.02)
    # The landing spot itself is pulled a little by the points of the shuttle lying
    # still, but stays within a metre.
    assert fit.landing is not None and np.allclose(fit.landing[:2], scene.landing[1][:2], atol=1.0)


def test_first_point_on_the_racket_becomes_the_contact_hint():
    scene = Smash3DScene(size=(960, 540))
    racket = observe(scene, [scene.t_hit - 0.6 / scene.fps])  # just before the hit
    obs = racket + observe(scene, flight_frames(scene, first_frame_after_hit=2))
    fit = fit_flight(scene.camera, obs, terminal_velocity=6.5)
    assert fit.first_point_was_racket
    assert fit.first_seen_kmh == pytest.approx(scene.true_speed_kmh(fit.first_seen_time), rel=0.03)


# -- match footage pipeline -----------------------------------------------------------


def test_split_at_the_hit_even_when_the_direction_doesnt_change():
    # Incoming at 8 px/frame straight down, then the smash, also straight down but
    # much faster: only the speed jump marks the hit (between two frames, at 0.255 s).
    t = np.arange(30) / 60
    hit = 0.255
    after = np.log1p(15 * np.clip(t - hit, 0, None)) / 15 * 2400  # fast, then slowing: drag
    v = np.where(t < hit, 100 + 480 * t, 100 + 480 * hit + after)
    track = ImageTrack([Observation(float(a), 500.0, float(b), 1.0) for a, b in zip(t, v)])
    first, second = split_at_hits(track)
    assert first.t_end < hit < second.t_start


@pytest.fixture(scope="module")
def rendered_shots():
    scene = Smash3DScene(size=(960, 540))
    return scene, analyze_3d(scene.frames(), scene.camera, Analyze3DConfig())


def test_analyze_3d_finds_and_measures_the_smash(rendered_shots):
    scene, shots = rendered_shots
    smashes = [s for s in shots if s.launch_reliable]
    assert len(smashes) == 1
    lo, hi = smashes[0].fit.speed_range_kmh
    assert lo <= scene.speed_kmh <= hi
    assert smashes[0].fit.contact_time == pytest.approx(scene.t_hit, abs=0.004)


def test_the_incoming_lift_has_no_known_launch_speed(rendered_shots):
    _, shots = rendered_shots
    lifts = [s for s in shots if s.track.t_end <= 0.21]
    assert lifts and not any(s.launch_known for s in lifts)


def test_cli_calibrate3d_and_analyze3d_with_a_track(tmp_path, capsys):
    import cv2

    scene = Smash3DScene(size=(960, 540))
    frame = next(scene.frames()).image
    cv2.imwrite(str(tmp_path / "frame.png"), frame)
    points = " ".join(f"{n}={u:.1f},{v:.1f}" for n, (u, v) in scene.landmark_pixels(noise_px=0.5).items())
    main(
        ["calibrate3d", str(tmp_path / "frame.png"), "--points", points, "-o", str(tmp_path / "camera.json")]
    )

    def write(path, times):
        with open(path, "w") as f:
            f.write("frame,u,v\n")
            f.writelines(f"{o.t * scene.fps:.0f},{o.u:.2f},{o.v:.2f}\n" for o in observe(scene, times))

    write(tmp_path / "track.csv", flight_frames(scene, first_frame_after_hit=2))
    write(tmp_path / "incoming.csv", incoming_frames(scene))
    main(
        [
            "analyze3d",
            "--camera",
            str(tmp_path / "camera.json"),
            "--track",
            str(tmp_path / "track.csv"),
            "--incoming",
            str(tmp_path / "incoming.csv"),
            "--fps",
            "60",
        ]
    )
    out = capsys.readouterr().out
    speed = float(out.split("launch speed")[1].split("km/h")[0])
    assert speed == pytest.approx(scene.speed_kmh, rel=0.05)


def test_cli_known_speed_tunes_the_drag(tmp_path, capsys):
    # This shuttle has more drag (terminal velocity 6.2 m/s) than the 6.7 assumed, so
    # the plain fit reads it too slow. Given its true speed, the drag is tuned to match.
    scene = Smash3DScene(size=(960, 540), terminal_velocity=6.2)
    scene.camera.save(tmp_path / "camera.json")
    for name, times in (
        ("track", flight_frames(scene, first_frame_after_hit=3)),
        ("in", incoming_frames(scene)),
    ):
        with open(tmp_path / f"{name}.csv", "w") as f:
            f.write("frame,u,v\n")
            f.writelines(f"{o.t * scene.fps:.2f},{o.u:.2f},{o.v:.2f}\n" for o in observe(scene, times))
    args = ["analyze3d", "--camera", str(tmp_path / "camera.json"), "--fps", "60"]
    args += ["--track", str(tmp_path / "track.csv"), "--incoming", str(tmp_path / "in.csv")]

    def launch_speed(out):
        return float(out.split("launch speed")[1].split("km/h")[0])

    main(args)
    assert launch_speed(capsys.readouterr().out) < 0.97 * scene.speed_kmh

    drag = str(tmp_path / "drag.toml")
    main([*args, "--known-speed", f"{scene.speed_kmh:.1f}", "--save-drag", drag])
    out = capsys.readouterr().out
    assert "terminal velocity" in out
    assert load_config(drag).tracker.terminal_velocity == pytest.approx(6.2, abs=0.15)

    main(["-c", drag, *args])  # the saved drag, used for the next run
    assert launch_speed(capsys.readouterr().out) == pytest.approx(scene.speed_kmh, rel=0.01)


# -- re-uploaded and broadcast footage -------------------------------------------------


def write_video(path, frames, fps):
    import cv2

    h, w = frames[0].shape
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h), isColor=False)
    for f in frames:
        out.write(f)
    out.release()


def test_repeated_frames_are_found_and_retimed(tmp_path):
    from shuttlecam.analyze3d import find_repeated_frames

    scene = Smash3DScene(size=(480, 270), fps=25.0, duration=2.0)
    real = [f.image for f in scene.frames()]
    # 25 fps footage uploaded at 60 fps: each frame shown 2 or 3 times (2.4 on average).
    shown = [real[int(i * 25 / 60)] for i in range(int(len(real) * 60 / 25))]
    write_video(tmp_path / "reupload.avi", shown, 60)
    timing = find_repeated_frames(str(tmp_path / "reupload.avi"))
    assert timing is not None and timing.rate == 25.0
    assert abs(len(timing.keep) - len(real)) <= 1
    assert timing.time_of(timing.keep[10]) == pytest.approx(10 / 25)


def test_genuine_60fps_is_left_alone(tmp_path):
    from shuttlecam.analyze3d import find_repeated_frames

    scene = Smash3DScene(size=(480, 270), fps=60.0, duration=1.0)
    write_video(tmp_path / "real60.avi", [f.image for f in scene.frames()], 60)
    assert find_repeated_frames(str(tmp_path / "real60.avi")) is None


def test_take_off_and_landing_anchor_a_far_away_camera():
    # A broadcast camera 30 m back and 11 m up with a long lens, 25 fps, shuttle first
    # seen 3 frames after the hit: the track alone can't judge depth, but the
    # take-off spot and the landing (both on the floor) pin the flight.
    scene = Smash3DScene(
        size=(960, 540),
        speed_kmh=300,
        heading_deg=175,
        elevation_deg=-12,
        contact=(1.3, 7.5, 2.9),
        camera_position=(0.1, -32.6, 11.3),
        look_at=(0.6, 0.0, 0.0),
        focal=4341.0,
        fps=25.0,
    )
    t_land, land = scene.landing
    times = [k / 25 for k in range(int(scene.t_hit * 25) + 3, 200) if k / 25 < t_land]
    obs = observe(scene, times, noise=1.0, seed=1)
    fit = fit_flight(
        scene.camera,
        obs,
        max_gap_s=4 / 25,
        takeoff_xy=(scene.contact[0] + 0.2, scene.contact[1] + 0.4),
        landing_px=tuple(scene.camera.project([land])[0]),
        landing_time=t_land,
    )
    lo, hi = fit.speed_range_kmh
    assert lo <= scene.speed_kmh <= hi
    assert fit.speed_kmh == pytest.approx(scene.speed_kmh, rel=0.1)
    assert fit.distance_ratio < 1.3  # and it knows the view has little perspective

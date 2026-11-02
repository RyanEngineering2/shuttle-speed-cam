"""The phone web app (`shuttlecam serve`): passcode, uploads, marking the court, measuring."""

import io

import pytest

pytest.importorskip("flask")

from shuttlecam.sim import Smash3DScene
from shuttlecam.web.server import create_app

CODE = "12345678"
HEADER = {"X-Shuttlecam": "1"}


@pytest.fixture
def client(tmp_path):
    return create_app(tmp_path / "data", CODE, inline_jobs=True).test_client()


def login(client, code=CODE):
    return client.post("/api/login", json={"passcode": code})


def upload(client, data: bytes, name: str, **form):
    form = {"video": (io.BytesIO(data), name), **form}
    return client.post("/api/clips", data=form, headers=HEADER, content_type="multipart/form-data")


def test_everything_needs_the_passcode(client):
    assert client.get("/api/state").status_code == 401
    assert login(client, "00000000").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/state").status_code == 200
    # Changes also need a header that a form on another site can't send.
    assert client.post("/api/logout").status_code == 400
    assert client.post("/api/logout", headers=HEADER).status_code == 200
    assert client.get("/api/state").status_code == 401


def test_guessing_the_passcode_is_slowed_down(client):
    for _ in range(8):
        assert login(client, "0").status_code == 401
    assert login(client).status_code == 429


def test_bad_files_and_ids_are_refused(client):
    login(client)
    r = upload(client, b"not a video", "clip.mp4")
    assert r.status_code == 400 and "video" in r.json["error"]
    assert upload(client, b"MZ...", "clip.exe").status_code == 400
    assert client.get("/api/clips/notanid").status_code == 404
    assert client.get("/api/setups/" + "0" * 32 + "/frame.jpg").status_code == 404
    assert client.get("/api/clips/..%2F..%2Fsecret.key").status_code == 404


def test_upload_mark_the_court_and_measure(client, tmp_path):
    import cv2

    scene = Smash3DScene(size=(960, 540))
    video = tmp_path / "smash.avi"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), scene.fps, scene.size, isColor=False)
    for frame in scene.frames():
        out.write(frame.image)
    out.release()
    login(client)

    # The first clip from a new camera spot waits for the court to be marked.
    r = upload(client, video.read_bytes(), "smash.avi", player="Ryan")
    assert r.status_code == 200, r.json
    clip, setup = r.json["clip"], r.json["setup"]
    assert clip["status"] == "needs_calibration" and setup["status"] == "needs_calibration"
    assert client.get(f"/api/setups/{setup['id']}/frame.jpg").status_code == 200

    points = scene.landmark_pixels(noise_px=0.5)
    few = dict(list(points.items())[:5])
    assert (
        client.post(f"/api/setups/{setup['id']}/calibrate", json={"points": few}, headers=HEADER).status_code
        == 400
    )
    r = client.post(f"/api/setups/{setup['id']}/calibrate", json={"points": points}, headers=HEADER)
    assert r.status_code == 200 and r.json["rms_px"] < 2
    assert client.get(f"/api/setups/{setup['id']}/overlay.jpg").status_code == 200

    # Accepting the calibration measures the waiting clip.
    assert client.post(f"/api/setups/{setup['id']}/accept", headers=HEADER).status_code == 200
    done = client.get(f"/api/clips/{clip['id']}").json
    assert done["status"] == "done", done.get("error")
    best = done["result"]["shots"][done["result"]["best"]]
    assert best["launch_reliable"]
    lo, hi = best["launch_range_kmh"]
    assert lo <= scene.speed_kmh <= hi
    assert done["player"] == "Ryan" and done["has_plot"]
    assert client.get(f"/api/clips/{clip['id']}/plot.png").status_code == 200

    # The next clip from the same spot is measured straight away.
    state = client.get("/api/state").json
    assert state["active_setup"] == setup["id"]
    r = upload(client, video.read_bytes(), "again.avi", setup="current")
    assert r.json["clip"]["status"] == "done"

    assert client.delete(f"/api/clips/{clip['id']}", headers=HEADER).status_code == 200
    assert client.get(f"/api/clips/{clip['id']}").status_code == 404


def test_the_camera_guide_pictures_are_served(client):
    import xml.etree.ElementTree as ET

    page = client.get("/").get_data(as_text=True)
    for name in ("guide-side.svg", "guide-top.svg", "guide-frame.svg"):
        assert f"/static/{name}" in page
        r = client.get(f"/static/{name}")
        assert r.status_code == 200
        assert ET.fromstring(r.get_data()).tag.endswith("svg")  # well-formed


def write_scene(path, scene, shift=(0, 0)):
    import cv2
    import numpy as np

    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), scene.fps, scene.size, isColor=False)
    m = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
    for frame in scene.frames():
        out.write(cv2.warpAffine(frame.image, m, scene.size, borderMode=cv2.BORDER_REPLICATE))
    out.release()
    return path.read_bytes()


def test_swapped_post_taps_and_a_moved_phone_are_caught(client, tmp_path):
    scene = Smash3DScene(size=(960, 540))
    login(client)
    setup = upload(client, write_scene(tmp_path / "a.avi", scene), "a.avi").json["setup"]

    # Tapping a post's top and bottom the wrong way round is undone.
    points = scene.landmark_pixels(noise_px=0.5)
    points["post_right_top"], points["post_right_base"] = points["post_right_base"], points["post_right_top"]
    r = client.post(f"/api/setups/{setup['id']}/calibrate", json={"points": points}, headers=HEADER)
    assert r.status_code == 200 and r.json["rms_px"] < 2
    assert any("right net post" in n for n in r.json["notes"])
    client.post(f"/api/setups/{setup['id']}/accept", headers=HEADER)

    # A clip filmed after the phone moved gets a new camera spot, with the old points
    # shifted to match as a starting point, instead of being measured with the old one.
    r = upload(client, write_scene(tmp_path / "b.avi", scene, shift=(4, 12)), "b.avi", setup="current")
    clip, moved = r.json["clip"], r.json["setup"]
    assert clip["status"] == "needs_calibration" and moved["id"] != setup["id"]
    assert moved["moved_px"] == pytest.approx(12.6, abs=1.5)
    u, v = moved["points"]["near_short/centre"]
    u0, v0 = points["near_short/centre"]
    assert (u - u0, v - v0) == pytest.approx((4, 12), abs=1.5)


def test_help_after_no_speed_check_the_court_then_tap_the_shuttle(client, tmp_path):
    scene = Smash3DScene(size=(960, 540))
    login(client)
    clip = upload(client, write_scene(tmp_path / "a.avi", scene), "a.avi").json["clip"]
    points = scene.landmark_pixels(noise_px=0.5)
    client.post(f"/api/setups/{clip['setup']}/calibrate", json={"points": points}, headers=HEADER)
    client.post(f"/api/setups/{clip['setup']}/accept", headers=HEADER)

    # Help: the clip gets its own camera spot, starting from the old points, and waits
    # for the taps even once the court is accepted.
    r = client.post(f"/api/clips/{clip['id']}/help", headers=HEADER)
    help_setup = r.json["setup"]
    assert r.json["clip"]["status"] == "needs_help" and help_setup["id"] != clip["setup"]
    assert help_setup["points"]["near_short/centre"] == pytest.approx(list(points["near_short/centre"]))
    assert (
        client.post(f"/api/clips/{clip['id']}/shuttle", json={"after": [1, 1, 1]}, headers=HEADER).status_code
        == 400
    )
    client.post(
        f"/api/setups/{help_setup['id']}/calibrate", json={"points": help_setup["points"]}, headers=HEADER
    )
    client.post(f"/api/setups/{help_setup['id']}/accept", headers=HEADER)
    assert client.get(f"/api/clips/{clip['id']}").json["status"] == "needs_help"

    frame = client.get(f"/api/clips/{clip['id']}/frame/15.jpg")
    assert frame.status_code == 200 and frame.mimetype == "image/jpeg"

    # Tap the shuttle just after the hit: it's followed from there and measured.
    k = int(scene.t_hit * scene.fps) + 3
    u, v = scene.camera.project([scene.truth(k / scene.fps)[:3]])[0]
    bad = client.post(f"/api/clips/{clip['id']}/shuttle", json={"after": [5000, 5, k]}, headers=HEADER)
    assert bad.status_code == 400
    done = client.post(
        f"/api/clips/{clip['id']}/shuttle", json={"after": [u + 3, v - 2, k]}, headers=HEADER
    ).json
    assert done["status"] == "done", done.get("error")
    best = done["result"]["shots"][done["result"]["best"]]
    assert best["guided"] and best["launch_reliable"]
    lo, hi = best["launch_range_kmh"]
    assert lo <= scene.speed_kmh <= hi

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

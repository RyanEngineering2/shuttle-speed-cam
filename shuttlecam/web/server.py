"""The web app behind `shuttlecam serve`: film a smash on a phone, upload it, get the speed.

It runs on the PC that does the processing. A phone reaches it on the same network,
or from anywhere through a tunnel such as Tailscale Funnel (docs/web-app.md). Every
API call needs the passcode.

The flow:

* The first clip from a new camera position starts a **setup**. The phone shows a
  frame from that clip and the player taps court landmarks on it, which calibrates
  the camera exactly as `shuttlecam calibrate3d` does.
* Every clip filmed from that position is then measured in the background with
  `shuttlecam analyze3d`, one at a time. Each runs in its own process with a time
  limit, so a broken or huge file can't take the server down.

Everything lives in one data folder: setups/<id>/ (frame, camera, overlay) and
clips/<id>/ (video, results, plot), each with a small JSON record.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2
from flask import Flask, abort, jsonify, request, send_file, session

from ..camera3d import calibrate_camera, draw_court
from ..court import landmarks, segments

MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_CLIP_SECONDS = 30.0
JOB_TIMEOUT_S = 15 * 60
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
ID = re.compile(r"^[0-9a-f]{32}$")
STATIC = Path(__file__).parent / "static"


# -- storage -------------------------------------------------------------------------


class Store:
    """JSON records in folders, one per setup and per clip."""

    def __init__(self, root: Path):
        self.root = Path(root)
        for sub in ("setups", "clips"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def dir(self, kind: str, item_id: str) -> Path:
        if not ID.match(item_id or ""):
            abort(404)
        return self.root / kind / item_id

    def load(self, kind: str, item_id: str) -> dict:
        path = self.dir(kind, item_id) / "record.json"
        if not path.exists():
            abort(404)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, kind: str, record: dict) -> dict:
        folder = self.root / kind / record["id"]
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "record.json.tmp"
        with self.lock:
            tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
            os.replace(tmp, folder / "record.json")
        return record

    def all(self, kind: str) -> list[dict]:
        out = []
        for path in (self.root / kind).glob("*/record.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda r: r["created"], reverse=True)

    def state(self) -> dict:
        path = self.root / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def set_state(self, **values) -> None:
        with self.lock:
            state = self.state()
            state.update(values)
            (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")


def probe(path: Path) -> dict:
    """Frame rate, size and length of a video, as OpenCV reads it (rotation applied)."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError("this file can't be read as a video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ok, frame = cap.read()
    cap.release()
    if not ok or fps <= 0:
        raise ValueError("this file can't be read as a video")
    h, w = frame.shape[:2]
    return {
        "fps": round(fps, 3),
        "frames": frames,
        "width": w,
        "height": h,
        "seconds": round(frames / fps, 2),
    }


def grab_frame(path: Path, seconds: float):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(seconds * fps))
    ok, frame = cap.read()
    if not ok:  # some files can't seek: take the first frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError("couldn't read a frame from the video")
    return frame


# -- the background worker -------------------------------------------------------------


@dataclass
class Worker:
    """Measures clips one at a time, each in its own `shuttlecam analyze3d` process."""

    store: Store
    config_path: str | None = None
    jobs: queue.Queue = field(default_factory=queue.Queue)
    inline: bool = False  # tests: run each job right away, in the calling thread

    def start(self) -> None:
        for clip in self.store.all("clips"):  # pick up where a restart left off
            if clip["status"] in ("queued", "running"):
                clip["status"] = "queued"
                self.store.save("clips", clip)
                self.jobs.put(clip["id"])
        threading.Thread(target=self._loop, daemon=True, name="shuttlecam-worker").start()

    def submit(self, clip_id: str) -> None:
        if self.inline:
            self.process(clip_id)
        else:
            self.jobs.put(clip_id)

    def _loop(self) -> None:
        while True:
            clip_id = self.jobs.get()
            try:
                self.process(clip_id)
            except Exception as e:  # noqa: BLE001 - one bad clip must not stop the worker
                print(f"clip {clip_id}: {e}", file=sys.stderr)
                try:
                    clip = self.store.load("clips", clip_id)
                    clip.update(status="failed", error="the server hit an error measuring this clip")
                    self.store.save("clips", clip)
                except Exception:  # noqa: BLE001, S110 - the clip may have been deleted meanwhile
                    pass

    def process(self, clip_id: str) -> None:
        store = self.store
        clip = store.load("clips", clip_id)
        setup = store.load("setups", clip["setup"])
        folder = store.dir("clips", clip_id)
        camera = store.dir("setups", setup["id"]) / "camera.json"
        clip.update(status="running", started=time.time())
        store.save("clips", clip)
        cmd = [sys.executable, "-m", "shuttlecam"]
        if self.config_path:
            cmd += ["-c", str(Path(self.config_path).resolve())]
        cmd += ["analyze3d", str(folder / clip["video"]), "--camera", str(camera)]
        cmd += ["--json", str(folder / "result.json"), "--plot", str(folder / "plot.png")]
        if clip.get("fps"):
            cmd += ["--fps", str(clip["fps"])]
        try:
            run = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=JOB_TIMEOUT_S,
                cwd=folder,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            (folder / "log.txt").write_text(run.stdout + run.stderr, encoding="utf-8")
            result_file = folder / "result.json"
            if run.returncode == 0 and result_file.exists():
                clip["result"] = json.loads(result_file.read_text(encoding="utf-8"))
                clip["status"] = "done"
            else:
                last = (run.stderr or run.stdout).strip().splitlines()[-1:] or ["no output"]
                clip.update(status="failed", error=f"analysis failed: {last[0][:300]}")
        except subprocess.TimeoutExpired:
            clip.update(
                status="failed", error=f"took longer than {JOB_TIMEOUT_S // 60} minutes; try a shorter clip"
            )
        clip["finished"] = time.time()
        clip["has_plot"] = (folder / "plot.png").exists()
        store.save("clips", clip)


# -- the app ---------------------------------------------------------------------------


def create_app(
    data_dir: str | Path,
    passcode: str,
    config_path: str | None = None,
    terminal_velocity: float = 6.7,
    inline_jobs: bool = False,
) -> Flask:
    store = Store(Path(data_dir))
    worker = Worker(store, config_path, inline=inline_jobs)
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config.update(
        SECRET_KEY=_secret_key(store.root),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=30 * 24 * 3600,
    )
    app.extensions["shuttlecam"] = {"store": store, "worker": worker}
    failed_logins: list[float] = []

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return resp

    @app.before_request
    def guard():
        path = request.path
        if not path.startswith("/api/") or path == "/api/login":
            return None
        if not session.get("ok"):
            return jsonify(error="log in first"), 401
        # Changes need a header a cross-site form can't send, on top of SameSite cookies.
        if request.method != "GET" and request.headers.get("X-Shuttlecam") != "1":
            return jsonify(error="missing request header"), 400
        return None

    @app.get("/")
    def index():
        return send_file(STATIC / "index.html")

    @app.post("/api/login")
    def login():
        now = time.time()
        failed_logins[:] = [t for t in failed_logins if now - t < 600]
        if len(failed_logins) >= 8:
            return jsonify(error="too many wrong passcodes; wait 10 minutes"), 429
        given = str((request.get_json(silent=True) or {}).get("passcode", ""))
        if not hmac.compare_digest(given.encode(), passcode.encode()):
            failed_logins.append(now)
            return jsonify(error="wrong passcode"), 401
        session.permanent = True
        session["ok"] = True
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/state")
    def state():
        setups = store.all("setups")
        active = store.state().get("active_setup")
        return jsonify(
            setups=setups[:20],
            active_setup=active
            if any(s["id"] == active and s["status"] == "ready" for s in setups)
            else None,
            clips=store.all("clips")[:100],
            landmarks={k: list(v) for k, v in landmarks().items()},
            segments=[[list(a), list(b)] for a, b in segments()],
            terminal_velocity=terminal_velocity,
            queue=worker.jobs.qsize(),
        )

    @app.post("/api/clips")
    def upload():
        file = request.files.get("video")
        if file is None or not file.filename:
            return jsonify(error="no video in the upload"), 400
        ext = Path(file.filename).suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            return jsonify(error=f"'{ext or 'no extension'}' isn't a video type this accepts"), 400
        fps = request.form.get("fps", "")
        try:
            fps = float(fps) if fps else None
        except ValueError:
            return jsonify(error="the frame rate must be a number"), 400
        if fps is not None and not 10 <= fps <= 1000:
            return jsonify(error="the frame rate must be between 10 and 1000"), 400
        clip_id = uuid.uuid4().hex
        folder = store.root / "clips" / clip_id
        folder.mkdir(parents=True)
        video = f"video{ext}"
        file.save(folder / video)
        try:
            info = probe(folder / video)
            if info["seconds"] > MAX_CLIP_SECONDS:
                raise ValueError(
                    f"the clip is {info['seconds']:.0f} s long; trim it to under {MAX_CLIP_SECONDS:.0f} s"
                )
        except ValueError as e:
            _remove(folder)
            return jsonify(error=str(e)), 400

        setup_id = request.form.get("setup", "")
        active = store.state().get("active_setup")
        if setup_id == "current" and active:
            setup_id = active
        if ID.match(setup_id or "") and (store.root / "setups" / setup_id / "record.json").exists():
            setup = store.load("setups", setup_id)
        else:  # a new camera position: this clip's frame is the one to calibrate on
            setup = _new_setup(store, folder / video, info)
        clip = {
            "id": clip_id,
            "created": time.time(),
            "player": str(request.form.get("player", ""))[:40].strip(),
            "video": video,
            "original_name": Path(file.filename).name[:120],
            "info": info,
            "fps": fps,
            "setup": setup["id"],
            "status": "queued" if setup["status"] == "ready" else "needs_calibration",
        }
        store.save("clips", clip)
        if clip["status"] == "queued":
            worker.submit(clip_id)
            clip = store.load("clips", clip_id)
        return jsonify(clip=clip, setup=setup)

    @app.get("/api/clips/<clip_id>")
    def clip(clip_id):
        return jsonify(store.load("clips", clip_id))

    @app.delete("/api/clips/<clip_id>")
    def delete_clip(clip_id):
        record = store.load("clips", clip_id)
        if record["status"] == "running":
            return jsonify(error="it's being measured right now; try again in a moment"), 409
        _remove(store.dir("clips", clip_id))
        return jsonify(ok=True)

    @app.get("/api/clips/<clip_id>/plot.png")
    def plot(clip_id):
        path = store.dir("clips", clip_id) / "plot.png"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/png", max_age=3600)

    @app.get("/api/setups/<setup_id>/<name>.jpg")
    def setup_image(setup_id, name):
        if name not in ("frame", "overlay"):
            abort(404)
        path = store.dir("setups", setup_id) / f"{name}.jpg"
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/jpeg", max_age=0)

    @app.post("/api/setups/<setup_id>/calibrate")
    def calibrate(setup_id):
        setup = store.load("setups", setup_id)
        raw = (request.get_json(silent=True) or {}).get("points") or {}
        known = landmarks()
        try:
            points = {str(k): (float(v[0]), float(v[1])) for k, v in raw.items() if k in known}
        except (TypeError, ValueError, IndexError):
            return jsonify(error="points must be {landmark: [u, v]}"), 400
        if len(points) < 6:
            return jsonify(error=f"mark at least 6 landmarks ({len(points)} so far)"), 400
        size = (setup["width"], setup["height"])
        try:
            cal = calibrate_camera(points, size)
        except (ValueError, cv2.error) as e:
            return jsonify(error=f"calibration failed: {e}"), 400
        folder = store.dir("setups", setup_id)
        cam = cal.camera
        cam.save(folder / "camera.json", extra={"rms_px": round(cal.rms_px, 2), "landmarks": points})
        image = cv2.imread(str(folder / "frame.jpg"))
        draw_court(image, cam, points, thickness=max(1, round(setup["width"] / 640)))
        cv2.imwrite(str(folder / "overlay.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        x, y, z = (float(c) for c in cam.position)
        setup.update(
            status="calibrated",
            points={k: list(v) for k, v in points.items()},
            rms_px=round(cal.rms_px, 2),
            errors_px={k: round(v, 1) for k, v in cal.errors_px.items()},
            camera_position=[round(x, 2), round(y, 2), round(z, 2)],
            calibrated=time.time(),
        )
        store.save("setups", setup)
        return jsonify(setup)

    @app.post("/api/setups/<setup_id>/accept")
    def accept(setup_id):
        setup = store.load("setups", setup_id)
        if setup["status"] not in ("calibrated", "ready"):
            return jsonify(error="calibrate it first"), 400
        setup["status"] = "ready"
        store.save("setups", setup)
        store.set_state(active_setup=setup_id)
        for clip in store.all("clips"):
            if clip["setup"] == setup_id and clip["status"] == "needs_calibration":
                clip["status"] = "queued"
                store.save("clips", clip)
                worker.submit(clip["id"])
        return jsonify(setup)

    @app.errorhandler(413)
    def too_big(_):
        return jsonify(
            error=f"the file is over {MAX_UPLOAD_BYTES // 2**20} MB; trim it or film at 1080p"
        ), 413

    if not inline_jobs:
        worker.start()
    return app


def _new_setup(store: Store, video: Path, info: dict) -> dict:
    setup = {
        "id": uuid.uuid4().hex,
        "created": time.time(),
        "status": "needs_calibration",
        "width": info["width"],
        "height": info["height"],
    }
    folder = store.root / "setups" / setup["id"]
    folder.mkdir(parents=True)
    frame = grab_frame(video, min(0.5, info["seconds"] / 2))
    cv2.imwrite(str(folder / "frame.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return store.save("setups", setup)


def _remove(folder: Path) -> None:
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    folder.rmdir()


def _secret_key(root: Path) -> bytes:
    path = root / "secret.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def _passcode(root: Path, given: str | None) -> str:
    path = Path(root) / "passcode.txt"
    if given:
        path.write_text(given, encoding="utf-8")
        return given
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    code = f"{secrets.randbelow(10**8):08d}"
    path.write_text(code, encoding="utf-8")
    return code


def _keep_awake(on: bool) -> None:
    """Stop Windows from sleeping while the server runs (the screen may still turn off)."""
    if sys.platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if on else 0))


def serve(
    data_dir: str,
    host: str = "127.0.0.1",
    port: int = 8000,
    passcode: str | None = None,
    config_path: str | None = None,
    terminal_velocity: float = 6.7,
) -> None:
    from waitress import serve as waitress_serve

    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    code = _passcode(root, passcode)
    app = create_app(root, code, config_path, terminal_velocity)
    print(f"Smash speed web app on http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    print(f"  passcode: {code}   (change it with --passcode)")
    print(f"  data:     {root}")
    print(
        f"  shuttle terminal velocity {terminal_velocity:.2f} m/s"
        + (f" (from {config_path})" if config_path else "")
    )
    if host == "127.0.0.1":
        print("  phones reach it through Tailscale Funnel: tailscale funnel --bg " + str(port))
    sys.stdout.flush()  # show this now even when the output goes to a file or a pipe
    _keep_awake(True)
    try:
        waitress_serve(app, host=host, port=port, threads=8, max_request_body_size=MAX_UPLOAD_BYTES + 2**20)
    finally:
        _keep_awake(False)

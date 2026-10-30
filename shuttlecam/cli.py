"""Command line: `python -m shuttlecam <command>` (or just `shuttlecam`)."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

from .calibration import Calibration
from .config import Config, load_config
from .tracker import Shot


def _load_calibration(path: str) -> Calibration:
    if not Path(path).exists():
        sys.exit(
            f"No calibration file at '{path}'.\n"
            "Take a frame with `shuttlecam snapshot`, then run `shuttlecam calibrate`.\n"
            "See docs/calibration.md."
        )
    return Calibration.load(path)


def _parse_pairs(text: str) -> list[tuple[float, float]]:
    """'1,2 3,4' -> [(1.0, 2.0), (3.0, 4.0)]"""
    pairs = []
    for item in text.split():
        a, b = item.split(",")
        pairs.append((float(a), float(b)))
    return pairs


class ShotLog:
    """Appends every shot to a CSV file."""

    FIELDS = ("time", "speed_kmh", "raw_speed_kmh", "duration_s", "points")

    def __init__(self, path: str):
        self.path = Path(path)
        new = not self.path.exists()
        self.file = open(self.path, "a", newline="")  # noqa: SIM115 - open for the whole run
        self.writer = csv.writer(self.file)
        if new:
            self.writer.writerow(self.FIELDS)

    def write(self, shot: Shot) -> None:
        self.writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                f"{shot.speed_kmh:.1f}",
                f"{shot.raw_speed_kmh:.1f}",
                f"{shot.t_end - shot.t_start:.3f}",
                shot.n_points,
            ]
        )
        self.file.flush()

    def close(self) -> None:
        self.file.close()


# -- commands ----------------------------------------------------------------------


def cmd_run(cfg: Config, args) -> None:
    from .camera import open_source
    from .display import open_display
    from .pipeline import Pipeline

    calibration = _load_calibration(args.calibration or cfg.calibration)
    display = open_display(args.display or cfg.display.backend, cfg.display.port, cfg.display.baud)
    source = open_source(cfg.camera, args.source)
    pipeline = Pipeline(cfg, calibration)
    log = ShotLog(cfg.log)

    print(f"shuttlecam running at up to {getattr(source, 'fps', cfg.camera.fps):.0f} fps. Ctrl+C to stop.")
    display.show_text("READY")
    best = 0.0
    frames, since = 0, time.monotonic()
    try:
        for frame in source:
            for shot in pipeline.process(frame):
                best = max(best, shot.speed_kmh)
                display.show_speed(shot.speed_kmh, best)
                log.write(shot)
            frames += 1
            now = time.monotonic()
            if args.stats and now - since >= 10:
                print(f"  processing {frames / (now - since):.0f} fps")
                frames, since = 0, now
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
        display.close()
        log.close()


def cmd_analyze(cfg: Config, args) -> None:
    from .camera import VideoSource
    from .pipeline import analyze

    if args.fps:
        cfg.camera.file_fps = args.fps
    calibration = _load_calibration(args.calibration or cfg.calibration)
    source = VideoSource(cfg.camera, args.video)
    print(f"{args.video}: {source.fps:.0f} fps")
    shots = analyze(source, cfg, calibration)
    source.close()

    if not shots:
        print("No shots found.")
        return
    for i, shot in enumerate(shots, 1):
        print(
            f"  shot {i}: {shot.speed_kmh:6.1f} km/h   (frame-to-frame peak {shot.raw_speed_kmh:.0f} km/h, "
            f"{shot.n_points} points, t={shot.t_start:.3f}s)"
        )
    if args.plot:
        from .plot import plot_shot

        fastest = max(shots, key=lambda s: s.speed_kmh)
        plot_shot(fastest, args.plot, title=Path(args.video).name)
        print(f"Saved {args.plot}")


def cmd_calibrate(cfg: Config, args) -> None:
    import cv2

    image = cv2.imread(args.image, cv2.IMREAD_GRAYSCALE)
    if image is None:
        sys.exit(f"Could not read {args.image}")
    world = _parse_pairs(args.world)
    if args.points:
        pixels = _parse_pairs(args.points)
    else:
        import matplotlib.pyplot as plt  # needs a desktop and an interactive backend

        plt.imshow(image, cmap="gray")
        plt.title(
            f"Click the {len(world)} points in this order: " + "  ".join(f"({x},{y})" for x, y in world)
        )
        pixels = plt.ginput(len(world), timeout=0)
        plt.close()
    if len(pixels) != len(world):
        sys.exit(f"Got {len(pixels)} image points for {len(world)} world points.")

    h, w = image.shape
    cal = Calibration.from_points(pixels, world, (w, h))
    cal.save(args.output)
    print(f"Saved {args.output}")
    for (u, v), (x, y) in zip(pixels, world):
        ex, ey = cal.to_world(u, v)
        print(f"  ({u:7.1f}, {v:7.1f}) px -> ({ex:6.3f}, {ey:6.3f}) m   [wanted ({x}, {y})]")


def cmd_snapshot(cfg: Config, args) -> None:
    import cv2

    from .camera import open_source

    source = open_source(cfg.camera, args.source)
    try:
        for i, frame in enumerate(source):
            if i >= 10:  # let exposure settle
                cv2.imwrite(args.output, frame.image)
                print(f"Saved {args.output} ({frame.image.shape[1]}x{frame.image.shape[0]})")
                break
    finally:
        source.close()


def cmd_simulate(cfg: Config, args) -> None:
    from .sim import SmashScene

    scene = SmashScene(speed_kmh=args.speed, fps=args.fps, angle_deg=args.angle, seed=args.seed)
    scene.write_video(args.output)
    cal_path = str(Path(args.output).with_suffix(".calibration.json"))
    scene.calibration().save(cal_path)
    print(f"Saved {args.output} and {cal_path}")
    print(f"  launch speed {args.speed:.0f} km/h at {args.fps:.0f} fps")
    print(f"  try: shuttlecam analyze {args.output} --calibration {cal_path} --plot shot.png")


def _parse_named_points(text: str) -> dict[str, tuple[float, float]]:
    """'near_short/centre=1012,839 post_left_top=711,606' -> {name: (u, v)}"""
    points = {}
    for item in text.split():
        name, uv = item.split("=")
        u, v = uv.split(",")
        points[name] = (float(u), float(v))
    return points


def cmd_calibrate3d(cfg: Config, args) -> None:
    import json

    import cv2
    import numpy as np

    from .camera3d import calibrate_camera
    from .court import landmarks, segments

    if args.list:
        print("Landmarks (metres; X right, Y away from the camera with the net at 0, Z up):")
        for name, (x, y, z) in landmarks().items():
            print(f"  {name:28s} ({x:+5.2f}, {y:+5.2f}, {z:4.2f})")
        return
    if not args.image:
        sys.exit("Give a frame from the video (or use --list).")
    image = cv2.imread(args.image)
    if image is None:
        sys.exit(f"Could not read {args.image}")
    h, w = image.shape[:2]
    points: dict[str, tuple[float, float]] = {}
    if args.points_file:
        points.update({k: tuple(v) for k, v in json.loads(Path(args.points_file).read_text()).items()})
    if args.points:
        points.update(_parse_named_points(args.points))
    if args.click:
        import matplotlib.pyplot as plt  # needs a desktop and an interactive backend

        for name in args.click:
            plt.imshow(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            plt.title(f"Click: {name}")
            (uv,) = plt.ginput(1, timeout=0)
            plt.close()
            points[name] = (float(uv[0]), float(uv[1]))

    cal = calibrate_camera(points, (w, h))
    cam = cal.camera
    cam.save(args.output, extra={"rms_px": round(cal.rms_px, 2), "landmarks": points})
    x, y, z = cam.position
    print(f"Saved {args.output}")
    print(f"  focal length {cam.f:.0f} px ({2 * np.degrees(np.arctan(w / 2 / cam.f)):.0f} deg across)")
    print(f"  camera at X={x:+.2f} Y={y:+.2f} m, {z:.2f} m high; landmarks fit to {cal.rms_px:.2f} px RMS")
    for name, err in cal.errors_px.items():
        flag = "   <- check this one" if err > max(5.0, 3 * cal.rms_px) else ""
        print(f"    {name:28s} {err:5.1f} px{flag}")
    if args.overlay:
        for a, b in segments():
            if min(cam.depth([a, b])) > 0:
                (u1, v1), (u2, v2) = cam.project([a, b])
                cv2.line(image, (int(u1), int(v1)), (int(u2), int(v2)), (0, 0, 255), 1, cv2.LINE_AA)
        for u, v in points.values():
            cv2.circle(image, (int(u), int(v)), 6, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.imwrite(args.overlay, image)
        print(f"  court model drawn over the frame: {args.overlay}")


def _read_track_csv(path: str, fps: float, sigma: float):
    """CSV with columns frame,u,v[,sigma] -> observations."""
    from .fit3d import Observation

    obs = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            s = float(row["sigma"]) if row.get("sigma") else sigma
            obs.append(Observation(float(row["frame"]) / fps, float(row["u"]), float(row["v"]), s))
    return obs


def _print_shot(i: int, shot, frame_of) -> None:
    """One shot's report. `frame_of(t)` turns a time back into a video frame number."""
    f = shot.fit
    obs = f.observations
    lo, hi = f.speed_range_kmh
    flo, fhi = f.first_seen_range_kmh
    print(f"shot {i}: frames {frame_of(obs[0].t):.0f}-{frame_of(obs[-1].t):.0f}, {len(obs)} points")
    if shot.launch_reliable:
        print(f"  launch speed   {f.speed_kmh:4.0f} km/h   (range {lo:.0f}-{hi:.0f})")
    elif shot.launch_known:
        print(f"  launch speed   not reliable (anywhere from {lo:.0f} to {hi:.0f} km/h)")
    else:
        print("  launch speed   unknown: the hit wasn't seen (no incoming shuttle; try --contact)")
    ms = (f.first_seen_time - f.contact_time) * 1000
    print(
        f"  first tracked  {f.first_seen_kmh:4.0f} km/h   (range {flo:.0f}-{fhi:.0f}), frame {frame_of(f.first_seen_time):.0f}",
        end="",
    )
    print(f", {ms:.0f} ms after the hit" if shot.launch_known else "")
    x, y, z = f.contact
    print(f"  hit            frame {frame_of(f.contact_time):.1f}, {z:.2f} m high at X={x:+.2f} Y={y:+.2f}")
    if shot.incoming is not None:
        o = shot.incoming.observations
        print(
            f"  incoming       timed from the track at frames {frame_of(o[0].t):.0f}-{frame_of(o[-1].t):.0f}"
        )
    if f.landing is not None:
        print(
            f"  lands          X={f.landing[0]:+.2f} Y={f.landing[1]:+.2f} at frame {frame_of(f.landing_time):.0f}"
        )
    note = (
        "distance from the camera poorly determined: add --takeoff"
        if f.depth_uncertain
        else "depth well determined"
    )
    print(f"  fit            {f.rms_px:.2f} px RMS; {note}")
    if f.distance_ratio < 1.5 and not shot.anchored:
        print(
            f"  view           little perspective: the shuttle's distance from the camera only changes "
            f"{f.distance_ratio:.1f}x,\n                 so depth rests on physics. Add --takeoff and --landing to anchor both ends."
        )


def cmd_analyze3d(cfg: Config, args) -> None:
    import dataclasses

    import cv2
    import numpy as np

    from .analyze3d import ImageTrack, Shot3D, analyze_3d, find_repeated_frames
    from .camera import VideoSource
    from .camera3d import CameraModel
    from .fit3d import fit_flight

    camera = CameraModel.load(args.camera)
    vt = cfg.tracker.terminal_velocity
    fps = args.fps
    timing = None

    if args.track:
        if not fps:
            sys.exit("--track needs --fps")
        obs = _read_track_csv(args.track, fps, cfg.analyze3d.pixel_noise)
        incoming = _read_track_csv(args.incoming, fps, cfg.analyze3d.pixel_noise) if args.incoming else None
        frame_dt = 1 / fps
        cam = camera
    else:
        cam_cfg = dataclasses.replace(cfg.camera, process_width=0, file_fps=args.fps)  # full resolution
        source = VideoSource(cam_cfg, args.video)
        fps = source.fps
        start_index = int(args.start * fps) if args.start else 0
        end_index = int(args.end * fps) if args.end else None
        if not args.keep_repeats:
            timing = find_repeated_frames(args.video, start_index, end_index)
        if start_index:
            source.cap.set(cv2.CAP_PROP_POS_FRAMES, start_index)
        keep = set(timing.keep) if timing else None

        def frames():
            for frame in source:
                index = start_index + frame.index
                if end_index is not None and index > end_index:
                    return
                if keep is not None and index not in keep:
                    continue  # a repeat of the previous frame
                t = timing.time_of(index) if timing else index / fps
                yield dataclasses.replace(frame, t=t, index=index)

        w = int(source.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(source.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cam = camera.for_frame_size(w, h)
        frame_dt = 1 / timing.rate if timing else 1 / fps

    def time_of(frame_no: float) -> float:
        return timing.time_of(frame_no) if timing else frame_no / fps

    def frame_of(t: float) -> float:
        if timing:
            return float(np.interp(t * timing.rate, np.arange(len(timing.keep)), timing.keep))
        return t * fps

    def hint(text: str | None):
        """'U,V' or 'U,V@FRAME' -> ((u, v), time or None)"""
        if not text:
            return None, None
        uv, _, frame_no = text.partition("@")
        return tuple(float(v) for v in uv.split(",")), (time_of(float(frame_no)) if frame_no else None)

    contact_px, contact_time = hint(args.contact)
    landing_px, landing_time = hint(args.landing)
    if landing_px is not None and landing_time is None:
        sys.exit("--landing needs the frame too: U,V@FRAME")
    takeoff = None
    if args.takeoff:
        u, v = (float(c) for c in args.takeoff.split(","))
        floor = cam.point_at_height(u, v, 0.0)
        if floor is None:
            sys.exit("--takeoff pixel is above the horizon")
        takeoff = (float(floor[0]), float(floor[1]))
    anchored = takeoff is not None and landing_px is not None
    hints = {
        "contact_px": contact_px,
        "contact_time": contact_time,
        "takeoff_xy": takeoff,
        "landing_px": landing_px,
        "landing_time": landing_time,
    }
    hinted = contact_px is not None or anchored
    window = args.hit_window * frame_dt if args.hit_window else None

    if args.track:
        fit = fit_flight(cam, obs, terminal_velocity=vt, incoming=incoming, max_gap_s=window, **hints)
        shots = [Shot3D(ImageTrack(obs), ImageTrack(incoming) if incoming else None, fit, hinted=hinted)]
        shots[0].anchored = anchored
    else:
        if timing:
            print(
                f"{args.video}: a {fps:.0f} fps file with repeated frames: {timing.rate:g} real frames per second, "
                f"re-timed (--keep-repeats turns this off)"
            )
        print(f"{args.video}: tracking at {w}x{h}...")
        shots = analyze_3d(frames(), camera, cfg.analyze3d, terminal_velocity=vt)
        source.close()
        if args.no_incoming:
            for shot in shots:
                shot.incoming = None
        if any(v is not None for v in hints.values()) and shots:
            # Hints describe one shot: the one ending nearest the landing (in the image
            # and in time), else starting nearest the contact, else the fastest.
            def gap(o, uv, t):  # pixels, plus 200 px per second apart
                return float(np.hypot(o.u - uv[0], o.v - uv[1])) + (
                    200 * abs(o.t - t) if t is not None else 0
                )

            if landing_px is not None:
                target = min(shots, key=lambda s: gap(s.track.observations[-1], landing_px, landing_time))
            elif contact_px is not None:
                target = min(shots, key=lambda s: gap(s.track.observations[0], contact_px, contact_time))
            else:
                target = max(shots, key=lambda s: s.fit.first_seen_kmh)
            target.fit = fit_flight(
                cam,
                target.track.observations,
                terminal_velocity=vt,
                incoming=target.incoming.observations if target.incoming else None,
                max_gap_s=window,
                **hints,
            )
            target.hinted = hinted
            target.anchored = anchored

    shots = [s for s in shots if s.fit.first_seen_kmh >= args.min_speed or s.hinted]
    shown = shots if args.all or args.track else [s for s in shots if s.reliable or s.hinted]
    hidden = len(shots) - len(shown)
    if not shown:
        print("No shots found." + (f" ({hidden} poor fits hidden; --all shows them.)" if hidden else ""))
        return
    for i, shot in enumerate(shown, 1):
        _print_shot(i, shot, frame_of)
    if hidden:
        print(
            f"({hidden} other tracks hidden: poor fits, usually players or half-hidden shots. --all shows them.)"
        )
    if args.plot:
        from .plot import plot_flight3d

        best = max(shown, key=lambda s: s.fit.speed_kmh if s.launch_reliable else s.fit.first_seen_kmh)
        image = None
        if args.video:
            cap = cv2.VideoCapture(args.video)
            cap.set(cv2.CAP_PROP_POS_FRAMES, round(frame_of(best.fit.first_seen_time)))
            ok, frame = cap.read()
            cap.release()
            if ok:
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        plot_flight3d(best.fit, cam, args.plot, image=image, title=Path(args.video or args.track).name)
        print(f"Saved {args.plot}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shuttlecam", description="Badminton smash speed camera.")
    parser.add_argument("-c", "--config", help="TOML config file (see config.example.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="live: camera -> speed -> P10 display")
    p.add_argument("--source", help="picamera2, a video file, or a webcam index")
    p.add_argument("--display", choices=["serial", "console", "none"])
    p.add_argument("--calibration", help="calibration file (default from config)")
    p.add_argument("--stats", action="store_true", help="print processing frame rate every 10 s")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("analyze", help="find shots in a recorded video")
    p.add_argument("video")
    p.add_argument("--fps", type=float, help="real capture rate, for slow-motion clips")
    p.add_argument("--calibration", help="calibration file (default from config)")
    p.add_argument("--plot", help="save a figure of the fastest shot to this file")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("calibrate", help="build a calibration from 4+ points on the flight plane")
    p.add_argument("image", help="a frame from the camera (see `snapshot`)")
    p.add_argument("--world", required=True, help='plane positions in metres, e.g. "0,0 0,2 1.98,0 1.98,2"')
    p.add_argument("--points", help="matching pixel positions; omit to click them instead")
    p.add_argument("-o", "--output", default="calibration.json")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("calibrate3d", help="calibrate a camera from court landmarks (for analyze3d)")
    p.add_argument("image", nargs="?", help="a frame from the video")
    p.add_argument(
        "--points", help='landmark=u,v pairs, e.g. "near_short/centre=1012,839 post_left_top=711,606"'
    )
    p.add_argument("--points-file", help="JSON file of {landmark: [u, v]}")
    p.add_argument(
        "--click", nargs="+", metavar="LANDMARK", help="click these landmarks in the frame instead"
    )
    p.add_argument("--list", action="store_true", help="list the landmark names")
    p.add_argument("--overlay", help="save the frame with the fitted court drawn on it")
    p.add_argument("-o", "--output", default="camera.json")
    p.set_defaults(func=cmd_calibrate3d)

    p = sub.add_parser("analyze3d", help="measure shots in match footage, in 3D (any camera angle)")
    p.add_argument("video", nargs="?", help="the video (omit when using --track)")
    p.add_argument("--camera", required=True, help="camera file from calibrate3d")
    p.add_argument("--fps", type=float, help="real capture rate (needed with --track)")
    p.add_argument("--start", type=float, help="start at this many seconds into the video")
    p.add_argument("--end", type=float, help="stop at this many seconds into the video")
    p.add_argument("--contact", help="where the racket met the shuttle: U,V or U,V@FRAME")
    p.add_argument("--takeoff", help="pixel U,V of the smasher's feet at take-off (pins down distance)")
    p.add_argument("--track", help="CSV of frame,u,v[,sigma]: the shot's track, instead of detecting it")
    p.add_argument("--incoming", help="CSV of the incoming shuttle before the hit (with --track)")
    p.add_argument(
        "--min-speed", type=float, default=60.0, help="hide shots first tracked slower than this (km/h)"
    )
    p.add_argument("--all", action="store_true", help="also show tracks whose fit is poor")
    p.add_argument(
        "--landing", help="where and when the shuttle reached the floor: U,V@FRAME (anchors the flight)"
    )
    p.add_argument("--keep-repeats", action="store_true", help="don't drop repeated frames (see docs)")
    p.add_argument("--no-incoming", action="store_true", help="don't time the hit from an incoming track")
    p.add_argument(
        "--hit-window",
        type=float,
        help="how many frames before the first sighting the hit may be (default 3)",
    )
    p.add_argument("--plot", help="save a figure of the fastest shot")
    p.set_defaults(func=cmd_analyze3d)

    p = sub.add_parser("snapshot", help="save one camera frame, for calibrating")
    p.add_argument("--source", help="picamera2, a video file, or a webcam index")
    p.add_argument("-o", "--output", default="frame.png")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("simulate", help="render a synthetic smash with a known speed")
    p.add_argument("-o", "--output", default="smash.mp4")
    p.add_argument("--speed", type=float, default=250.0, help="launch speed, km/h")
    p.add_argument("--fps", type=float, default=200.0)
    p.add_argument("--angle", type=float, default=-12.0, help="launch angle, degrees (negative = down)")
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(func=cmd_simulate)

    args = parser.parse_args(argv)
    args.func(load_config(args.config), args)


if __name__ == "__main__":
    main()

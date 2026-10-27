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

import pytest

from shuttlecam.camera import pick_sensor_mode, process_size
from shuttlecam.cli import main
from shuttlecam.config import load_config, save_terminal_velocity
from shuttlecam.display import SerialDisplay, speed_line, text_line


def test_config_overrides_and_rejects_typos(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[camera]\nfps = 120\nsize = [1280, 720]\n[display]\nbackend = "serial"\n')
    cfg = load_config(path)
    assert cfg.camera.fps == 120
    assert cfg.camera.size == (1280, 720)
    assert cfg.display.backend == "serial"
    assert cfg.tracker.model == "drag"  # untouched default

    path.write_text("[camera]\nfsp = 120\n")
    with pytest.raises(ValueError, match="camera.fsp"):
        load_config(path)


def test_example_config_loads():
    load_config("config.example.toml")


def test_save_terminal_velocity_keeps_the_rest_of_the_file(tmp_path):
    new = tmp_path / "drag.toml"
    save_terminal_velocity(new, 7.244, "from a radar gun")
    assert load_config(new).tracker.terminal_velocity == 7.24
    assert "# from a radar gun" in new.read_text()

    full = tmp_path / "config.toml"
    full.write_text("[camera]\nfps = 120\n\n[tracker]  # the filter\nterminal_velocity = 6.7\ngate = 5.0\n")
    save_terminal_velocity(full, 6.9)
    cfg = load_config(full)
    assert (cfg.tracker.terminal_velocity, cfg.tracker.gate, cfg.camera.fps) == (6.9, 5.0, 120)

    no_tracker = tmp_path / "other.toml"
    no_tracker.write_text("[display]\nbackend = 'serial'\n")
    save_terminal_velocity(no_tracker, 6.5)
    cfg = load_config(no_tracker)
    assert (cfg.tracker.terminal_velocity, cfg.display.backend) == (6.5, "serial")


def test_process_size_keeps_aspect():
    assert process_size(640, 480, 320) == (320, 240)
    assert process_size(1920, 1080, 480) == (480, 270)
    assert process_size(320, 240, 640) == (320, 240)


def test_sensor_mode_choice():
    modes = [
        {"size": (640, 480), "fps": 206.65, "bit_depth": 10},
        {"size": (1640, 1232), "fps": 41.85, "bit_depth": 10},
        {"size": (3280, 2464), "fps": 21.19, "bit_depth": 10},
    ]
    assert pick_sensor_mode(modes, (640, 480), 200)["size"] == (640, 480)
    assert pick_sensor_mode(modes, (1280, 960), 30)["size"] == (1640, 1232)
    assert pick_sensor_mode(modes, (1280, 960), 120)["size"] == (640, 480)  # fastest fallback


class FakePort:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def close(self):
        pass


def test_display_protocol():
    assert speed_line(245.4, 301.6) == "SPEED 245 302"
    assert text_line("Smash  ✓ mantap!") == "TEXT Smash  ? mantap!"
    port = FakePort()
    display = SerialDisplay("unused", serial_port=port)
    display.show_speed(210, 250)
    display.show_text("READY")
    display.clear()
    assert port.data == b"SPEED 210 250\nTEXT READY\nCLEAR\n"


def test_cli_simulate_then_analyze(tmp_path, capsys):
    video = str(tmp_path / "smash.avi")
    main(["simulate", "-o", video, "--speed", "200"])
    main(["analyze", video, "--calibration", str(tmp_path / "smash.calibration.json")])
    out = capsys.readouterr().out
    assert "shot 1:" in out
    speed = float(out.split("shot 1:")[1].split("km/h")[0])
    assert speed == pytest.approx(200, rel=0.04)

import pytest

from shuttlecam.camera import pick_sensor_mode, process_size
from shuttlecam.cli import main
from shuttlecam.config import load_config
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

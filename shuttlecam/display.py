"""Show speeds on the P10 LED matrix (or the terminal).

The P10 panels are driven by an ESP32 running firmware/p10-display. HUB12 panels
need a steady refresh from a hardware timer, which a busy Linux box cannot give,
so the Pi only sends short text lines over UART:

    SPEED <kmh> <best_kmh>     big number, "KM/H", and the session best
    TEXT <message>             scrolling message
    CLEAR                      blank the panels

Lines starting with '#' are comments and are ignored by the firmware.
"""

from __future__ import annotations

import re


def speed_line(kmh: float, best_kmh: float) -> str:
    return f"SPEED {round(kmh)} {round(best_kmh)}"


def text_line(text: str) -> str:
    # The panel font is ASCII only; keep it short and on one line.
    clean = re.sub(r"[^\x20-\x7e]", "?", text).strip()
    return f"TEXT {clean[:60]}"


class SerialDisplay:
    def __init__(self, port: str, baud: int = 115200, serial_port=None):
        if serial_port is None:
            import serial  # pyserial

            serial_port = serial.Serial(port, baud, timeout=0, write_timeout=0.1)
        self.port = serial_port

    def _send(self, line: str) -> None:
        self.port.write((line + "\n").encode("ascii"))

    def show_speed(self, kmh: float, best_kmh: float) -> None:
        self._send(speed_line(kmh, best_kmh))

    def show_text(self, text: str) -> None:
        self._send(text_line(text))

    def clear(self) -> None:
        self._send("CLEAR")

    def close(self) -> None:
        self.port.close()


class ConsoleDisplay:
    def show_speed(self, kmh: float, best_kmh: float) -> None:
        print(f"  >>> {kmh:5.0f} km/h      (best {best_kmh:.0f} km/h)", flush=True)

    def show_text(self, text: str) -> None:
        print(f"  [{text}]", flush=True)

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass


class NullDisplay(ConsoleDisplay):
    def show_speed(self, kmh: float, best_kmh: float) -> None:
        pass

    def show_text(self, text: str) -> None:
        pass


def open_display(backend: str, port: str = "/dev/serial0", baud: int = 115200):
    if backend == "serial":
        return SerialDisplay(port, baud)
    if backend == "console":
        return ConsoleDisplay()
    if backend == "none":
        return NullDisplay()
    raise ValueError(f"display backend must be serial, console or none, not {backend!r}")

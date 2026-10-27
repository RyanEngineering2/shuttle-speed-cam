# P10 display node

An ESP32 DevKit that drives HUB12 P10 LED panels with the
[DMD32](https://github.com/Qudor-Engineer/DMD32) library and shows what the
Raspberry Pi sends it.

HUB12 panels have no frame memory. The rows have to be scanned all the time, and
any pause shows as flicker. A hardware timer on a microcontroller does that
easily. A Linux box running a camera pipeline does not, so the Pi hands the job
to this board.

## Build and flash

```bash
pip install platformio
pio run -d firmware/p10-display -t upload
pio device monitor -b 115200     # optional: type commands by hand
```

Using one panel instead of two? Change `-DPANELS_ACROSS=2` to `1` in
`platformio.ini`.

## Protocol

One ASCII line per command at 115200 baud, on UART2 (RX = GPIO16) or the USB port:

| Command | Shows |
|---|---|
| `SPEED 245 278` | 245 in large digits (it flashes for about a second, then stays), plus `KM/H` and a crown next to the best speed, 278 |
| `TEXT READY` | a scrolling message |
| `CLEAR` | nothing |
| `TEST` | a test pattern for a second |
| `# ...` | ignored (comments and debug output) |

With two panels:

```
┌────────────────────────────────┬────────────────────────────────┐
│  ███  ████ ████        K M / H │                                │
│    █     █ █   ←── big digits  │  ♛ 2 7 8   ← session best      │
└────────────────────────────────┴────────────────────────────────┘
```

## Wiring

See [docs/hardware.md](../../docs/hardware.md#wiring).

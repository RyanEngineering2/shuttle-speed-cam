# Web app: measure smashes at the court with a phone

`shuttlecam serve` runs a small website on a PC. At the court, anyone with the link
and the passcode films a smash on their phone, uploads the clip, and gets the speed
back. The PC does the processing ([3D mode](3d-mode.md)), so the phone needs nothing
installed.

```
phone ──https──> Tailscale Funnel ──> PC: shuttlecam serve ──> shuttlecam analyze3d
```

## On the PC

```bash
pip install -e ".[web]"               # Flask, Waitress, Matplotlib
shuttlecam serve                      # or: shuttlecam -c drag.toml serve
```

It prints the address and a passcode (random the first time, then kept; set your
own with `--passcode`). Clips, results and camera spots go in `~/shuttlecam-web`
(`--data` to change it). While it runs, the PC won't go to sleep. Processing
takes about a minute per clip on a desktop PC, one clip at a time.

With `-c drag.toml`, every clip uses a shuttle drag you tuned with
`analyze3d --known-speed` ([3D mode](3d-mode.md#tuning-the-drag-to-a-known-speed)).
The page footer shows which terminal velocity is in use.

### Reaching it from the court

By default the server only listens on the PC itself. Pick one way in:

| | Phones need | Link |
|---|---|---|
| **Tailscale Funnel** (recommended) | nothing | permanent `https://<pc>.<tailnet>.ts.net` |
| Tailscale, private | the Tailscale app, signed in to your account | the same link |
| Same Wi-Fi | to be on the PC's network | `http://<pc-ip>:8000`, after `serve --host 0.0.0.0` |

Tailscale Funnel, once:

```bash
tailscale login                       # sign in to your (free) Tailscale account
tailscale funnel --bg 8000            # approve Funnel when it gives you a link
tailscale funnel status               # shows the public link
```

The Funnel setting survives restarts. To take the site offline, close the server
or run `tailscale funnel --https=443 off`.

## At the court

1. **Film.** Phone on a stand behind one baseline, landscape, with the court lines
   and both net posts in view. 60 fps or faster; 1080p is plenty.
2. **Trim** each clip to start about a second before the smash, with the shuttle
   coming in, and end after it lands. Upload the original from the camera roll,
   not a copy sent through a chat app: recompression can wipe out the shuttle.
3. **Upload.** The first clip from a new camera spot asks you to **mark the court**:
   tap each point on a frame from your clip, then drag to fine-tune it under the
   magnifier. Skip any you can't see; at least 6 are needed, and a net post top
   helps most. The page then draws the fitted court over the frame: if the red
   lines sit on the real ones, accept it.
4. **Read the speed.** Every later clip from the same spot is measured straight
   away. The result shows the launch speed with its likely range, where the hit
   was and where the shuttle landed, plus the fitted flight. "Best per player"
   ranks everyone's fastest smash.

How the number is shown:

| Shown | Meaning |
|---|---|
| **336 km/h** | launch speed, measured with a tight range |
| **~477 km/h** (amber) | a rough estimate: the range is wide (far camera, few frames) |
| **≥116 km/h** (amber) | the hit wasn't timed (no incoming shuttle in the clip), so only the speed when first seen is known |

Move the phone, and pick "New camera spot" on the next upload.

## Tested

Momota's jump smash from [3D mode](3d-mode.md#how-well-it-works), cut to a 2 s clip
the way a phone would trim it, uploaded as HEVC `.mov` (iPhone format), with the
court marked by tapping on a 375 px wide phone screen:

| | Phone flow | Desktop, full clip |
|---|---|---|
| Court marking | 10 points, 2.85 px; camera 6.0 m behind the baseline, 1.8 m up | 19 points, 1.8 px; 5.9 m, 1.8 m |
| Launch speed | **337 km/h** (315–345) | 336 km/h (325–343) |
| Hit | 2.81 m high | 2.82 m high |

Short clips needed one change in the analysis: the motion detector learns the
empty court from the first frame, and a clip that starts a second before the smash
doesn't give it time to forget where the players stood. Clips under 4 s now start
it from the median of the clip's frames instead (`analyze3d --background`).

## Security

The site is on the public internet when Funnel is on, so:

- Every request needs the passcode; wrong guesses are limited to 8 per 10 minutes.
- Changes also need a request header that another website can't send, on top of
  same-site cookies.
- Uploads must be videos OpenCV can read, up to 500 MB and 30 s; they're stored
  under random names. Each clip is measured in its own process with a 15-minute
  limit.
- The server listens on 127.0.0.1 unless you say otherwise: only Tailscale (or this
  PC) can reach it.

Share the passcode only with the people you play with, and change it with
`--passcode` if it gets around.

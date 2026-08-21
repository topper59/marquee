# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A now-playing display for a 128x64 HUB75 LED matrix, driven by a Raspberry Pi 4B
mounted on an Adafruit RGB Matrix Bonnet. It polls Plex for active sessions and
renders poster art, title, season/episode code, user, time remaining, and a
progress bar. The whole app is a single file, `plex_matrix.py`.

**This code cannot run or be meaningfully tested on the dev machine.** It imports
`rgbmatrix`, needs the BDF fonts at `/opt/rpi-rgb-led-matrix/fonts`, and talks to
a Plex server on a LAN the dev machine may not share. All verification happens on
the Pi. The dev machine has no PIL either — anything touching images must run
over SSH.

## The deployment target

| | |
|---|---|
| Host | `ssh root@192.168.2.129` (hostname `nowplaying`) |
| Service | `plex-matrix.service` (systemd, enabled) |
| App dir | `/opt/plex-matrix/` — only `plex_matrix.py` plus a venv |
| Interpreter | `/opt/plex-matrix/venv/bin/python` (3.13) |
| Config | `/etc/plex-matrix.env`, mode 600, loaded via `EnvironmentFile` |

The Pi is on WiFi and the first SSH after idle often fails with "no route to
host" while it still answers ping. Retry once with a longer `ConnectTimeout`
before concluding anything is wrong.

There is no git repo, on the Pi or here. `pi/etc/` holds local copies of the unit
file and env file; the env copy is gitignored because it carries real tokens.

## Commands

```bash
./deploy.sh              # syntax-check, push plex_matrix.py, restart, tail logs
./deploy.sh --env        # also push pi/etc/plex-matrix.env
./deploy.sh --unit       # also push the unit file + daemon-reload
```

`deploy.sh` runs `python3 -m py_compile` first, which is the only check that
works locally — it compiles without importing, so the missing `rgbmatrix` and
`PIL` don't matter.

```bash
ssh root@192.168.2.129 'journalctl -u plex-matrix.service -f'
ssh root@192.168.2.129 'systemctl restart plex-matrix.service'
```

The service logs its computed layout on startup (`Layout: title y=... `), which
is the quickest way to confirm a font or spacing change took effect.

## Testing without the panel

There are no unit tests. Two patterns cover verification, both run on the Pi:

**Logic tests** — stub out `rgbmatrix` before loading the module, then exercise
the pure functions (`State` cycling, `wrap_two_lines`, `format_remaining`,
`compute_text_layout`):

```python
m = types.ModuleType("rgbmatrix"); m.RGBMatrix = object; m.RGBMatrixOptions = object
m.graphics = types.SimpleNamespace(Font=object, Color=object, DrawText=None)
sys.modules["rgbmatrix"] = m
```

To load *real* fonts in the same script, `from rgbmatrix import graphics as
REAL_G` **before** installing the stub, then call `REAL_G.Font()`.

**Visual preview** — PIL can read the same BDF fonts via `PIL.BdfFontFile`, so
the panel can be mocked offscreen at 128x64 and scaled up to a PNG. This is how
layout and font changes get judged without photographing the panel. Parse
`FONT_ASCENT`/`FONT_DESCENT` out of the BDF to convert rgbmatrix's baseline
coordinates into PIL's top-left ones. Assert that text blocks do not overlap
rather than only eyeballing the render.

## Architecture

`main()` starts three threads over one lock-guarded `State`:

- **`fetcher_loop`** — polls Plex `/status/sessions` every `POLL_SECONDS`,
  replaces the session list, then lazily fetches any missing posters.
- **`ha_poller_loop`** — polls Home Assistant every `HA_POLL_SECONDS` and sets a
  single `dim` flag.
- **`render_loop`** — runs on the main thread at ~20fps, owns all drawing.

The render loop must stay cheap. Anything expensive is precomputed and cached on
the `Session` — see `make_paused_poster`, which builds the dimmed pause overlay
once per session rather than dimming 4096 pixels per frame.

### Plex specifics

- Access is **unauthenticated**: the Pi is in the PMS "allowed without auth"
  network list. `PLEX_TOKEN` exists in the config as an optional fallback if that
  setting ever changes.
- TLS verification is **off by default**. Plex's `*.plex.direct` cert cannot
  match the bare LAN IP in `PLEX_URL`. The alternative is the long
  `192-168-1-3.<hash>.plex.direct:32400` hostname, which verifies properly but
  depends on public DNS — the tradeoff is deliberate.
- Posters are fetched at `POSTER_SUPERSAMPLE` times the target size and reduced
  locally with LANCZOS. **Do not ask Plex to scale straight to 64px** — its
  scaler plus the tiny JPEG is visibly soft. `minSize=1` puts the short edge on
  the requested size so the existing center-crop lands exactly.

### Rendering constraints

- Left 64x64 is poster art; right 64x64 is text. `RX = 64`.
- Vertical positions are **computed**, not hardcoded — `compute_text_layout`
  distributes text blocks using each font's real `.height`/`.baseline`, so
  swapping a font rebalances the panel automatically. Don't reintroduce magic
  baseline constants.
- Widths are **measured**, not assumed — `text_width` sums per-glyph advances.
  The title font (`helvR12` at one point, `7x14B` currently) may be proportional,
  so `len(s) * W` is wrong.
- `wrap_two_lines` never revisits a line once it has moved on. An earlier greedy
  version let a short word jump back up to fill line one, which silently
  reordered episode titles.

### Home Assistant

Dims the panel only when the living-room TV is on **and** the sun is below the
horizon. Both states come from HA; `HA_TOKEN` is required at import time.

## Configuration

All tuning is env vars in `/etc/plex-matrix.env`, read at import. `HA_TOKEN` is
the only hard requirement (`os.environ[...]`); everything else has a default.

`PLEX_URL` `PLEX_TOKEN` `PLEX_VERIFY_SSL` `POLL_SECONDS` `CYCLE_SECONDS`
`HTTP_TIMEOUT` `STALE_AFTER_FAILURES` `HA_URL` `HA_TOKEN` `HA_TV_ENTITY`
`HA_POLL_SECONDS` `BRIGHTNESS_NORMAL` `BRIGHTNESS_DIM` `SCHEDULE_START`
`SCHEDULE_STOP` `FONT_DIR` `LOG_LEVEL`

Visual constants (`PAUSE_DIM`, `REMAIN_FG`, `POSTER_SUPERSAMPLE`, `DOTS_Y`,
`BAR_Y0/Y1`, `TEXT_REGION_BOTTOM`) are module-level, not env — edit and redeploy.

## Hardware notes

`isolcpus=3` in `/boot/firmware/cmdline.txt` reserves a core for the matrix
refresh thread, and the unit pins the process to `CPUAffinity=0 1 2`. These two
must stay consistent. Sustained CPU around 45% of one core is normal — that is
the library's software PWM, not a bug. `build_matrix()` holds the panel timing
options (`gpio_slowdown`, `pwm_bits`, `pwm_lsb_nanoseconds`); changing them risks
flicker, so change one at a time and look at the panel.

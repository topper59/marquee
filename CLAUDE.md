# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A now-playing display for a 128x64 HUB75 LED matrix, driven by a Raspberry Pi 4B
mounted on an Adafruit RGB Matrix Bonnet. It polls Plex for active sessions and
renders poster art, title, season/episode code, user, time remaining, and a
progress bar. It is being productized: an unconfigured device boots into an
open WiFi AP with a captive-portal setup wizard, and a settings web UI stays
available afterwards at http://nowplaying.local.

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
| App dir | `/opt/plex-matrix/` — `nowplaying/` package, `tests/`, venv |
| Interpreter | `/opt/plex-matrix/venv/bin/python` (3.13), runs `-m nowplaying` |
| Config | `/var/lib/nowplaying/config.json`, mode 600 |
| OS | Debian 13 (trixie), NetworkManager 1.52 |

The Pi is on WiFi and the first SSH after idle often fails with "no route to
host" while it still answers ping. Retry once with a longer `ConnectTimeout`
before concluding anything is wrong.

**wlan0 is the Pi's only network link.** Never test AP/station transitions
(`network.manage`, nmcli experiments) without (1) an ethernet cable plugged in
as an out-of-band SSH path and (2) a dead-man revert:
`systemd-run --on-active=180 --unit=nm-revert nmcli con up <home-profile>`,
cancelled only after SSH is confirmed alive. The AP profile is autoconnect=no
and station profiles autoconnect=yes, so a power cycle always recovers to
station mode.

`pi/etc/` holds local copies of the unit file, the captive dnsmasq conf, and
the legacy env file; the env copy is gitignored because it carries real tokens.

## Commands

```bash
./deploy.sh              # compileall, rsync package+tests, restart, tail logs
./deploy.sh --deps       # also pip install -r requirements.txt in the Pi venv
./deploy.sh --env        # also push pi/etc/plex-matrix.env (legacy)
./deploy.sh --unit       # also push unit + captive dnsmasq conf + daemon-reload
```

```bash
ssh root@192.168.2.129 'journalctl -u plex-matrix.service -f'
ssh root@192.168.2.129 '/opt/plex-matrix/venv/bin/python /opt/plex-matrix/tests/on_pi_smoke.py'
```

The service logs its computed layout on startup (`Layout: title y=...`), which
is the quickest way to confirm a font or spacing change took effect.

## Architecture

Package `nowplaying/`, one process, one service. `app.py` builds `Config`,
then starts threads over one lock-guarded `State`:

- **`plex/client.py: fetcher_loop`** — polls `/status/sessions`, replaces the
  session list, lazily fetches missing posters. Rebuilds its `Plex` client when
  the config's plex section changes.
- **`ha.py: ha_poller_loop`** — optional Home Assistant integration; sets the
  single `dim` flag (TV on AND sun below horizon). Idles when disabled.
- **`netmgr.py: NetManager`** — WiFi provisioning state machine over nmcli
  (AP mode + captive portal when unprovisioned). Inert while config
  `network.manage` is false — which it is on this device until signed off.
- **`resetbtn.py: ResetButton`** — GPIO25 (Bonnet #25 pad): short press = info
  page; 10s hold = factory reset (wipes config + WiFi profiles, restarts into
  setup). GPIO24 is NOT free — it is the E address line on this panel.
- **`web/server.py`** — Flask on port 80 (werkzeug make_server, daemon
  thread): setup wizard when unprovisioned, settings page otherwise, JSON API
  under `/api/`. Captive-portal probes redirect in AP mode. Optional password
  (sha256, session cookie). Secrets are redacted with a `•••` sentinel.
- **`display/render.py: render_loop`** — main thread, ~20fps, owns all
  drawing. `DisplayMode` status pages (setup/link-code/error/info/reset) are
  dispatched ahead of the schedule gate at ~2fps.

The render loop must stay cheap. Anything expensive is precomputed and cached
on the `Session` — see `make_paused_poster`. Display settings are re-derived
only when `Config.generation` moves.

### Config

`config.py` owns `/var/lib/nowplaying/config.json`: `get()` returns a deep
snapshot, `update()` takes `{dotted.path: value}`, validates all-or-nothing,
bumps `generation`, saves atomically. Threads snapshot per loop iteration —
most settings apply live; `matrix.*` and `web.*` need a restart
(`RESTART_REQUIRED`), which the UI flags and performs via detached
`systemd-run`. On first boot with no config.json, `/etc/plex-matrix.env` is
migrated once (the `.factory-reset` marker suppresses this). Visual constants
(`PAUSE_DIM`, `POSTER_SUPERSAMPLE`, `DOTS_Y`, …) and font paths remain module
constants in config.py — edit and redeploy.

### Plex specifics

- This device sits in the PMS "allowed without auth" list, so `plex.token` is
  empty here; other people's servers get 401 and go through the plex.tv link
  flow (`plex/auth.py`, PIN + polling; the code shows on panel and portal).
- LAN TLS verification is **off**: Plex's `*.plex.direct` cert cannot match a
  bare LAN IP. plex.tv calls in `auth.py` use verified TLS — keep that split.
- GDM discovery (UDP 32414) only crosses the local subnet; this house's PMS
  (192.168.1.3) is on a different subnet than the Pi (192.168.2.x), so manual
  entry and the post-sign-in resources lookup are first-class paths.
- Posters are fetched at `POSTER_SUPERSAMPLE`× and reduced locally with
  LANCZOS. **Do not ask Plex to scale straight to 64px** — visibly soft.
  `minSize=1` puts the short edge on the requested size for the center-crop.

### Provisioning (netmgr)

AP = nmcli profile `nowplaying-ap`, `ipv4.method shared` (NM spawns dnsmasq),
10.42.0.1/24. Captive DNS wildcard lives in
`/etc/NetworkManager/dnsmasq-shared.d/captive.conf` — only loaded for shared
connections, inert in station mode. brcmfmac usually cannot scan while the AP
is up, so the neighborhood is scanned *before* raising the AP and cached for
the portal. Join failures roll back to AP mode with the error surfaced on
panel and portal.

### Rendering constraints

- Left 64x64 is poster art; right 64x64 is text. `RX = 64`.
- Vertical positions are **computed**, not hardcoded — `compute_text_layout`
  distributes blocks using each font's real `.height`/`.baseline`. Don't
  reintroduce magic baseline constants.
- Widths are **measured** per-glyph (`text_width`); fonts may be proportional.
- `wrap_two_lines` never revisits a line once it has moved on (prevents
  silent word reordering).

## Testing without the panel

`tests/on_pi_smoke.py` runs on the Pi and covers the pure logic: it stubs
`rgbmatrix` before import, exercises wrap/layout/state/config/migration, and
drives the `NetManager` state machine with a scripted fake `run_cmd` (no real
nmcli calls). Add tests there for any new pure logic.

**Visual preview** — PIL can read the same BDF fonts via `PIL.BdfFontFile`, so
the panel can be mocked offscreen at 128x64 and scaled up to a PNG (run on the
Pi; the dev machine has no PIL). Parse `FONT_ASCENT`/`FONT_DESCENT` from the
BDF to convert baseline coordinates. Assert that text blocks do not overlap
rather than only eyeballing the render.

**Live AP testing** — see the wlan0 warning above. Stage manually first
(service stopped, AP via nmcli, phone join, DHCP lease, wildcard DNS check,
captive sheet against `python -m http.server 80`) before the integrated flow.

## Hardware notes

`isolcpus=3` in `/boot/firmware/cmdline.txt` reserves a core for the matrix
refresh thread, and the unit pins the process to `CPUAffinity=0 1 2`. These two
must stay consistent. Sustained CPU around 45% of one core is normal — that is
the library's software PWM, not a bug. `build_matrix()` reads panel timing
options from config `matrix.*` (`gpio_slowdown`, `pwm_bits`,
`pwm_lsb_nanoseconds`); changing them risks flicker, so change one at a time
and look at the panel. `hardware_mapping` is `adafruit-hat-pwm` here (PWM
solder bridge done); DIY kits without the mod need `adafruit-hat`.

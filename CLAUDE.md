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

### Factory reset without the button

`nowplaying/factoryreset.py` is the shared implementation (the GPIO hold path
and the web UI's Device section both call it) and doubles as a CLI. The web
path is `POST /api/factory-reset` with `{"confirm": true}` (plus optional
`keep_wifi`); it schedules the same detached `systemd-run` shown below rather
than wiping inline, so the HTTP reply gets out before the WiFi profile
disappears.

From SSH, run it detached too, or the `nmcli con delete` kills the session
mid-command:

```bash
# Wipe settings, KEEP WiFi — device stays online and SSH survives.
# This is the one for iterating on the wizard: setup restarts at the Plex step.
ssh root@192.168.2.129 'systemd-run --collect --unit=np-reset \
  -p WorkingDirectory=/opt/plex-matrix \
  /opt/plex-matrix/venv/bin/python -m nowplaying.factoryreset --keep-wifi -y'

# Full reset — also deletes the WiFi profiles. The Pi comes back in AP mode and
# SSH over the LAN is GONE until it is re-provisioned.
ssh root@192.168.2.129 'systemd-run --collect --unit=np-reset \
  -p WorkingDirectory=/opt/plex-matrix \
  /opt/plex-matrix/venv/bin/python -m nowplaying.factoryreset -y'
```

`WorkingDirectory` (or `PYTHONPATH=/opt/plex-matrix`) is required — the venv
does not have the package installed, only on the path. Without `-y` the CLI
prompts; an unrecognised argument exits 2 without touching anything.

Before ever running the full form, back the profile up so a failed portal run
is not a stranded device — it survives the reset because it is outside
NetworkManager's store:

```bash
cp /etc/NetworkManager/system-connections/nowplaying-wifi.nmconnection \
   /root/wifi-backup.nmconnection
# recover (from the setup AP: ssh root@10.42.0.1) with
cp /root/wifi-backup.nmconnection \
   /etc/NetworkManager/system-connections/nowplaying-wifi.nmconnection
chmod 600 /etc/NetworkManager/system-connections/nowplaying-wifi.nmconnection
nmcli con reload && nmcli con up nowplaying-wifi
```

sshd listens on 0.0.0.0, so the setup AP (`NowPlaying-Setup-<MAC4>`, open,
10.42.0.1) is a working out-of-band path when no ethernet is plugged in.

## Architecture

Package `nowplaying/`, one process, one service. `app.py` builds `Config`,
then starts threads over one lock-guarded `State`:

- **`plex/client.py: fetcher_loop`** — polls `/status/sessions`, applies the
  session filter, replaces the session list, lazily fetches missing posters.
  Rebuilds its `Plex` client when the config's plex section changes. It owns
  `State.plex_offline`, set once `stale_after_failures` polls in a row have
  failed and cleared by any success, a server change, or the URL being
  cleared.
- **`plex/filters.py`** — pure, no imports from the rest of the package, so
  `config.py` can borrow its vocabulary and the smoke test can drive it
  directly. `plex.filter` holds allow-lists (`users`, `players`,
  `media_types`) and deny-lists (`ignore_users`, `ignore_players`) plus
  `hide_paused`; empty means "everything", deny beats allow, and matching is
  case- and whitespace-insensitive because these are typed on a phone. Plex
  item types collapse onto movie/episode/track/other.
- **`ha.py: ha_poller_loop`** — optional Home Assistant integration; sets the
  single `dim` flag. The TV being on is always required; `ha.require_sunset`
  (default true) adds the `sun.sun below_horizon` condition. Idles when
  disabled.
- **`netmgr.py: NetManager`** — WiFi provisioning state machine over nmcli
  (AP mode + captive portal when unprovisioned). Provisioning is inert while
  config `network.manage` is false, but static addressing still applies (see
  below), so the thread runs either way. It is **true** on this device as of 2026-08-22,
  and `nowplaying-wifi` is the only station profile — so a full factory reset
  really does cost LAN SSH access.
- **`resetbtn.py: ResetButton`** — GPIO25 (Bonnet #25 pad): short press = info
  page; 10s hold = factory reset (wipes config + WiFi profiles, restarts into
  setup). GPIO24 is NOT free — it is the E address line on this panel. The
  wipe itself lives in `factoryreset.py`, shared with the CLI.
- **`web/server.py`** — Flask on port 80 (werkzeug make_server, daemon
  thread): setup wizard when unprovisioned, settings page otherwise, JSON API
  under `/api/`. The settings page treats each section as its own form: Save
  commits only the visible section and leaving one offers to discard its
  edits, so a change you can no longer see can never ride along with an
  unrelated save. Captive-portal probes redirect in AP mode. Optional password
  (sha256, session cookie). Secrets are redacted with a `•••` sentinel.
  `web.theme` (`auto`/`light`/`dark`) is rendered into `<html data-theme>` by
  a context processor so the first paint is already right; app.css keeps the
  dark values in one `--dk-*` block and maps them from both the forced-dark
  selector and the `prefers-color-scheme` query.
- **`display/render.py: render_loop`** — main thread, ~20fps, owns all
  drawing. `DisplayMode` status pages (setup/link-code/error/info/reset) are
  dispatched ahead of the schedule gate at ~2fps.

The render loop must stay cheap. Anything expensive is precomputed and cached
on the `Session` — see `make_paused_poster`. Display settings are re-derived
only when `Config.generation` moves.

### Config

`config.py` owns `/var/lib/nowplaying/config.json`: `get()` returns a deep
snapshot, `update()` takes `{dotted.path: value}` (nested subsections like
`plex.filter.users` included), validates all-or-nothing,
bumps `generation`, saves atomically. Validation is two-stage: per-path
normalizers in `_VALIDATORS`, then `_validate_combined()` on a merged
candidate for rules that span fields (a static address needs an address *and*
a same-subnet gateway). Combined rules only fire when the patch touches their
inputs, so a hand-edited file cannot make unrelated settings unsavable. Threads snapshot per loop iteration —
most settings apply live; `matrix.*` and `web.*` need a restart
(`RESTART_REQUIRED`), which the UI flags and performs via detached
`systemd-run`. On first boot with no config.json, `/etc/plex-matrix.env` is
migrated once (the `.factory-reset` marker suppresses this). Visual constants
(`PAUSE_DIM`, `POSTER_SUPERSAMPLE`, `DOTS_Y`, …) and font paths remain module
constants in config.py — edit and redeploy.

### Plex specifics

- The house PMS routinely has several people streaming at once, so the
  session filter is not hypothetical — an unfiltered panel cycles through
  strangers' movies. `/api/status` reports `player` and `type` per session so
  the settings page can offer the real strings to click rather than making
  someone guess Plex's spelling of a username.
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

**Static addressing** — `network.ipv4_method` (`auto`/`manual`) plus
`ipv4_address`/`ipv4_prefix`/`ipv4_gateway`/`ipv4_dns` are pushed onto the
*active* station profile (`nmcli -f GENERAL.CONNECTION dev show wlan0`, so a
hand-provisioned device works too) with `nmcli con mod` + `con up`. They apply
only when the values change — the profile already persists them — and after a
portal join, which rebuilds the profile from scratch. wlan0 is the only link,
so anything short of a confirmed reconnect reverts the profile to DHCP, brings
it back up, and rewrites `ipv4_method` to `auto` so the settings page stops
claiming a static address the device is not using. The failure text lands in
`netmgr.ipv4_error` → `/api/status`, because the browser was almost certainly
disconnected at the moment it happened; it therefore outlives the attempt and
is cleared only explicitly — the Dismiss link, a new addressing save, or a
successful apply (`POST /api/network/clear-error`).

### Rendering constraints

- Left 64x64 is poster art; right 64x64 is text. `RX = 64`.
- The progress bar and time remaining come from `Session.live_offset_ms`,
  which advances the last polled `viewOffset` by the wall time since it was
  sampled. Without it both step visibly once per `poll_seconds`. Paused
  sessions hold still, and the next poll resets the base so drift cannot
  accumulate. `Session.progress` is still the raw poll value — the API
  reports it; the panel does not use it.
- An unreachable Plex server draws "Can't reach Plex" in the idle branch
  rather than as a `DisplayMode` status page, deliberately: status pages are
  drawn ahead of the schedule gate, and a notice that lights the panel at 3am
  is worse than the wrong caption. `Nothing playing` and `plex_offline` are
  different claims and must not be conflated.
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

**Settings-page logic** — `tests/web_ui_test.js` runs on the *dev* machine
(`npm install jsdom && node tests/web_ui_test.js`), building the page from the
real templates and driving the real `picker.js`/`app.js` with `fetch` and
`confirm` stubbed. It covers the UI's logic rather than its looks: Save is
scoped to the visible section, the unsaved-changes guard on navigation,
static-IP validation, and the filter rules (checkbox groups, click-to-add
names, revert). Note that several checkboxes cannot share one `data-path` —
Save would keep only the last — so a group writes through a single hidden
input that owns the path (`data-group`, `syncGroups`/`bindGroups`). `node_modules` is gitignored — install jsdom wherever
you run it.

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

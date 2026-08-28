# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Marquee** — a now-playing display for a 128x64 HUB75 LED matrix, driven by a Raspberry Pi 4B
mounted on an Adafruit RGB Matrix Bonnet. It polls Plex for active sessions and
renders poster art, title, season/episode code, user, time remaining, and a
progress bar. It is being productized: an unconfigured device boots into an
open WiFi AP with a captive-portal setup wizard, and a settings web UI stays
available afterwards at http://marquee.local.

**This code cannot run or be meaningfully tested on the dev machine.** It imports
`rgbmatrix`, needs the BDF fonts at `/opt/rpi-rgb-led-matrix/fonts`, and talks to
a Plex server on a LAN the dev machine may not share. All verification happens on
the Pi. The dev machine has no PIL either — anything touching images must run
over SSH.

## The deployment target

| | |
|---|---|
| Host | `ssh root@192.168.2.129` (hostname `marquee`) |
| Service | `marquee.service` (systemd, enabled) |
| App dir | `/opt/marquee/` — `marquee/` package, `tests/`, venv |
| Interpreter | `/opt/marquee/venv/bin/python` (3.13), runs `-m marquee` |
| Config | `/var/lib/marquee/config.json`, mode 600 |
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

`pi/etc/` holds local copies of the unit file and the captive dnsmasq conf.

### Hostname

`marquee.local` comes from avahi following the system hostname, and the
hostname is pinned in the **cloud-init seed** at `/boot/firmware/user-data`:

```yaml
hostname: marquee
fqdn: marquee
prefer_fqdn_over_hostname: false
```

`hostnamectl set-hostname` alone does not hold. cloud-init re-runs
`update_hostname`/`update_etc_hosts` every boot, and the merge order puts the
datasource's user-data **above** anything in `/etc/cloud/cloud.cfg.d/`, so a
`preserve_hostname: true` drop-in there loses. Worse, its cached datasource
(`/var/lib/cloud/instance/obj.pkl`, a pickle that text greps skip) keeps
serving the old name until it is deleted — and with the pickle gone but no
explicit `fqdn:`, cloud-init falls back to reverse DNS, which handed back
`nowplaying` from the router and set the hostname straight back. Pinning both
`hostname` and `fqdn` in the seed short-circuits every fallback. Changing the
device's name again means editing that file and deleting `obj.pkl`.

### The name

The product is **Marquee**. It was called `plex-matrix` (unit, `/opt`) and
`nowplaying` (package, state dir, hostname, AP SSID) at different times; both
were renamed on 2026-08-24 — service, app dir, package, `/var/lib`, hostname,
mDNS name, and the `marquee-wifi` / `marquee-ap` nmcli profiles all say
`marquee` now. Loggers use `getLogger(__name__)` rather than a hardcoded
string, so a future rename cannot leave one behind.

Anything that still says `plex-matrix` is a bug. (The env-file migration
era is over: the one field device was migrated, then the whole legacy layer
was removed before the repo went public.)

## Commands

```bash
./deploy.sh              # compileall, rsync package+tests, restart, tail logs
./deploy.sh --deps       # also pip install -r requirements.txt in the Pi venv
./deploy.sh --unit       # also push unit + captive dnsmasq conf + daemon-reload
```

```bash
ssh root@192.168.2.129 'journalctl -u marquee.service -f'
ssh root@192.168.2.129 '/opt/marquee/venv/bin/python /opt/marquee/tests/on_pi_smoke.py'
```

The service logs its computed layout on startup (`Layout: title y=...`), which
is the quickest way to confirm a font or spacing change took effect.

### Software updates (marquee/update.py, release/)

An update is one signed `.mqup` file: `payload.tar.gz` (manifest, `marquee/`
package, requirements.txt) + a raw Ed25519 signature over the payload bytes.
Two delivery paths — the device pulls it from GitHub Releases
(`update.UPDATE_REPO`, checked daily when `updates.auto_check`), or the user
uploads the same file on the settings page — converge on
`/var/lib/marquee/updates/pending.mqup` and one applier. The public key
embedded in `update.py` is the entire trust boundary; the private key is
**only** at `~/.config/marquee-release/signing.key` on the dev machine (plus
offline backup), never in the repo or CI. Downgrades are refused by version
compare against the *signed* manifest; the CLI `--force` exists for bench
work and is not reachable from the web API.

`/opt/marquee/marquee` is a **symlink** into `/opt/marquee/versions/<v>/`.
The applier (`python -m marquee.update apply`, detached via systemd-run like
restart/factory-reset, because it restarts the service it was asked from)
unpacks beside the running version, runs pip only when requirements.txt
changed (before the flip, so a pip failure aborts cleanly), flips the
symlink, restarts, then watches `ActiveState`/`NRestarts` for ~20s — a
version that does not stay up is flipped back automatically. Every outcome
is written to `updates/last_result.json` because the browser that asked was
disconnected mid-install; the settings page reports it on next load.
`versions/dev` is deploy.sh's target and is never pruned; releases keep
current + previous.

Cutting a release: bump `__version__` in `marquee/__init__.py`, commit, then
`release/release.sh "notes"` (builds+signs `dist/marquee-<v>.mqup`, verifies
it with the shipped verifier, tags, `gh release create`). `release/build.py
--keygen` is one-time and refuses to overwrite — a new key orphans every
device in the field.

### OS updates, firewall, SSH toggle

`pi/etc/20auto-upgrades` + `52marquee-upgrades` run unattended-upgrades on
the **Debian security origin only**, never auto-rebooting: the app watches
`/var/run/reboot-required` (`/api/status: reboot_required`) and the Device
page offers a full reboot (`POST /api/reboot` — distinct from the app
restart, which a kernel update ignores).

`pi/etc/nftables.conf` is default-drop input: 80, mDNS, DHCP client, and
53/67 for the AP-mode captive portal (harmless in station mode — nothing
listens). **Port 22 is open at the firewall on purpose**; SSH availability
is the sshd *service*. The settings Remote-access card toggles it
(`POST /api/ssh`): enabling requires choosing a root password, set via
`systemd-run --pipe chpasswd` because the unit's CapabilityBoundingSet is
too tight for the shadow rewrite (password via stdin, never argv), and
writes `/etc/ssh/sshd_config.d/20-marquee.conf` as a receipt. Factory reset
disables SSH **only when that receipt exists** — this dev Pi's
hand-configured SSH has no receipt, which is what keeps reset tests safe to
run over SSH. Never create that drop-in on the dev device.

Changing the firewall: `deploy.sh --unit` pushes and applies it (after
`nft -c`), but test a rule change behind a dead-man first:
`systemd-run --on-active=120 --unit=nft-revert nft flush ruleset`, confirm a
NEW ssh connection works, then stop the timer. Established connections
survive a bad ruleset; new ones are the test.

### SD-card image (image/, GitHub Actions)

`.github/workflows/build-image.yml` builds a flashable Raspberry Pi OS Lite
image with pi-gen on every published release (and on demand); the
`image/stage-marquee` stage installs the app **in the updater's layout**
(`versions/<v>` + symlink), builds rgbmatrix from source into the venv, and
applies the same `pi/etc/` files the dev deploy uses — the workflow copies
them in so image and dev device cannot drift. SSH is off in the image, the
journal capped, onboard audio off (the matrix needs its PWM), `isolcpus=3`
matching the unit's `CPUAffinity`. No secrets in CI: the image carries only
the update public key. WiFi regulatory domain is baked as US — revisit
before selling abroad. The built image is renamed from pi-gen's `<date>-marquee-…`
to `marquee-<version>-…` after `__version__` in the staged source, so the
filename says what is inside it rather than when it was built.

### Factory reset without the button

`marquee/factoryreset.py` is the shared implementation (the GPIO hold path
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
  -p WorkingDirectory=/opt/marquee \
  /opt/marquee/venv/bin/python -m marquee.factoryreset --keep-wifi -y'

# Full reset — also deletes the WiFi profiles. The Pi comes back in AP mode and
# SSH over the LAN is GONE until it is re-provisioned.
ssh root@192.168.2.129 'systemd-run --collect --unit=np-reset \
  -p WorkingDirectory=/opt/marquee \
  /opt/marquee/venv/bin/python -m marquee.factoryreset -y'
```

`WorkingDirectory` (or `PYTHONPATH=/opt/marquee`) is required — the venv
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
nmcli con reload && nmcli con up marquee-wifi
```

**The profile *files* are still `nowplaying-*.nmconnection`.** `nmcli con mod
… connection.id` renames the connection, not the file backing it, and nothing
keys on the filename — NM matches on the id inside, so `nmcli con up
marquee-wifi` and `nmcli con delete marquee-wifi` both work. Renaming the
files means moving them out from under NM's inotify watch on the Pi's only
link; not worth it for cosmetics.

sshd listens on 0.0.0.0, so the setup AP (`Marquee-Setup-<MAC4>`, open,
10.42.0.1) is a working out-of-band path when no ethernet is plugged in.

## Architecture

Package `marquee/`, one process, one service. `app.py` builds `Config`,
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
- **`gallery.py: Gallery`** — the two cycling idle galleries. *Recently
  played* is written by the fetcher as sessions appear (a record of the room,
  not of the server) and is **persisted**, because it is the one list a
  restart cannot rebuild; *recently added* is pulled from Plex on a slow timer
  and is not. Art is cached as 64x64 PNG under `STATE_DIR/art/<slug>/`, keyed
  on a hash of the Plex thumb path so new artwork invalidates itself — a few
  hundred KB all told, on disk rather than in RAM so the gallery is back
  immediately after a restart while Plex is still waking up. Each gallery owns
  its own lock and its own art subdirectory: `prune_art()` deletes whatever
  the gallery's items no longer reference, so a shared directory would have
  the two galleries deleting each other's posters every poll. The render loop
  only ever calls `items()`, which returns a snapshot — it must never block on
  a disk read with a frame due, which is also why these are read *without*
  holding the `State` lock. The fetcher only does the extra work when
  `display.idle_mode` is one of `config.GALLERY_MODES`.
- **`ha.py: ha_poller_loop`** — optional Home Assistant integration; sets
  `State.dim` or `State.ha_blank`, never both. The TV being on is always
  required; `ha.require_sunset` (default true) adds the `sun.sun
  below_horizon` condition. `ha.tv_action` picks what that does: `dim` (the
  original behaviour) or `off`, which blanks the panel through the same gate
  as the display schedule — a theater room cannot use the dim brightness,
  since any lit panel is a distraction in a dark room. `/api/status` carries
  `ha_blank` so the settings page can say why the panel is dark, for the same
  reason `plex_offline` is there — a blanked panel and a broken one look
  identical. Idles when disabled.
  Dimming is not HA-only: `display.dim_start`/`dim_stop` do it on the clock,
  and the render loop ORs the two.
- **`netmgr.py: NetManager`** — WiFi provisioning state machine over nmcli
  (AP mode + captive portal when unprovisioned). Provisioning is inert while
  config `network.manage` is false, but static addressing still applies (see
  below), so the thread runs either way. It is **true** on this device as of 2026-08-22,
  and `marquee-wifi` is the only station profile — so a full factory reset
  really does cost LAN SSH access.
- **`resetbtn.py: ResetButton`** — GPIO25 (Bonnet #25 pad): short press = info
  page; 10s hold = factory reset (wipes config + WiFi profiles, restarts into
  setup). GPIO24 is NOT free — it is the E address line on this panel. The
  wipe itself lives in `factoryreset.py`, shared with the CLI.
- **`web/server.py`** — Flask on port 80 (werkzeug make_server, daemon
  thread): setup wizard when unprovisioned, settings page otherwise, JSON API
  under `/api/`. Two `before_request` gates run ahead of the password check.
  `block_cross_site` refuses any mutating request carrying a foreign `Origin`
  or `Sec-Fetch-Site: cross-site` — header-less callers (curl, the Home
  Assistant `rest_command` in the README) pass, because a request with no
  browser behind it is not one another site provoked. `restrict_ap_mode`
  serves only `AP_ALLOWED_EXACT`/`AP_ALLOWED_PREFIXES` while the AP is up:
  the setup AP is **open**, so until the device is on a network its owner
  controls, an anonymous caller in radio range must not be able to reach
  `/api/ssh`, factory reset, reboot, updates, or `/api/password`. Endpoints
  whose body has a meaningful falsy default (`/api/ssh`, `/api/password`)
  reject a missing body outright rather than reading it as "off"/"clear".
  `web.password` is refused by `/api/settings` (`SETTINGS_DENY`) — it is
  hashed only by `/api/password`, and a raw value written here would lock
  everyone out. `/login` locks an address out for `LOGIN_LOCKOUT_S` after
  `LOGIN_MAX_FAILURES`. The settings page treats each section as its own form: Save
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

`config.py` owns `/var/lib/marquee/config.json`: `get()` returns a deep
snapshot, `update()` takes `{dotted.path: value}` (nested subsections like
`plex.filter.users` included), validates all-or-nothing,
bumps `generation`, saves atomically. Validation is two-stage: per-path
normalizers in `_VALIDATORS`, then `_validate_combined()` on a merged
candidate for rules that span fields (a static address needs an address *and*
a same-subnet gateway). Combined rules only fire when the patch touches their
inputs, so a hand-edited file cannot make unrelated settings unsavable. Threads snapshot per loop iteration —
most settings apply live; `matrix.*` and `web.*` need a restart
(`RESTART_REQUIRED`), which the UI flags and performs via detached
`systemd-run`. Visual constants
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

AP = nmcli profile `marquee-ap`, `ipv4.method shared` (NM spawns dnsmasq),
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

### Two time windows, opposite defaults

`_in_window` does the wrap-around-midnight arithmetic; what *equal endpoints*
mean is the caller's decision, and the two callers disagree on purpose:
`is_within_schedule` (display on/off) reads them as **always**, while
`is_within_dim_window` reads them as **never**. An unset schedule has to leave
the panel on; an unset dim window has to leave the brightness alone. Note the
old behaviour only treated `00:00`/`00:00` as always-on, so `07:00`/`07:00`
silently meant "never on" — equal endpoints are now always-on whatever the
time.

### The panel override

`display.override` (`none`/`on`/`off`) plus `display.override_until` (a unix
time, `0` meaning "until I say otherwise") is the manual "not right now"
control — set from the home page card and from `POST /api/panel`, not saved
with the rest of the display section. It lives in config so it survives a
restart: someone who silenced the panel for the evening does not want a power
blip undoing it.

`config.effective_override()` is shared by the render loop and the web API so
the panel and the page can never disagree about whether an "off for an hour"
is still running. An expired override reads as `none` rather than being
rewritten — the render loop must not do file I/O, and a stale line in
config.json costs nothing until someone next looks.

`GET/POST /api/panel` is also the documented Home Assistant hook (README):
the HA integration in settings is one-directional (HA tells the display about
the TV), and this is the other direction.

### Rendering constraints

- One 64x64 half is poster art, the other is text. `display.poster_side`
  (`left`/`right`) swaps them; the render loop derives `poster_x`/`text_x`
  from it in the `Config.generation` block and every draw is relative to
  those, so nothing else hardcodes 0/64. `RX = 64` stays as the module-level
  default. The poster is drawn *after* the title on purpose: a scrolling
  title used to overrun its own half and the poster image was what painted
  over the overrun. The sub-pixel path below clips the title at the source
  instead, so the ordering is now belt-and-braces rather than load-bearing —
  but the DrawText fallback still overruns, so keep it.
- `display.accent` is the one colour to change: the progress bar takes it
  neat, the time remaining at `REMAIN_SCALE`, and the setup/link screens use
  it for their headings. Errors stay red. Both are rebuilt only when
  `Config.generation` moves, because `graphics.Color` allocates and this loop
  runs 60×/second. The web UI keeps its own amber — a user-picked accent
  behind `--on-accent` button text is a contrast problem, not a feature.
- `display.idle_mode` is `clock` / `blank` / `poster` / `recent_played` /
  `recent_added` (see the gallery section below). `poster` holds
  `State.last_poster` — stashed by `State.replace` at the moment the session
  list goes empty, which is the last instant the artwork still exists — and
  puts the clock in the text half. It falls back to the clock before anything
  has played. The held poster is drawn **at full strength**: there used to be
  an `IDLE_POSTER_DIM` multiply on top, which was a second dimming nobody
  could reach from the settings page. Panel brightness already carries the
  schedule, the dim window, and Home Assistant; the idle screen follows them
  like everything else.
- An outage outranks every idle mode, `blank` included: a dark panel is
  exactly what someone would misread as "it broke".
- Gallery captions are drawn in `FONT_TINY` (4x6), not 5x7: "Recently
  Played" measures 75px in 5x7 against a 64px half and `DrawText` does not
  clip, so it silently ran off the panel.
- The render loop runs at **60fps** (`config.SCROLL_FRAME_MS`), not the 20 it
  used to. The panel itself refreshes at ~120Hz — measured on the Pi,
  `SwapOnVSync` blocks a steady 8.33ms (p95 8.35, max 8.37) — so 20fps was
  discarding five sixths of the positions a moving title could occupy, and
  that, not any panel setting, is what made scrolling look chunky. 60 divides
  into 120: pick frame rates that land on a whole number of panel refreshes
  rather than beating against them. Drawing a frame costs 0.26ms, so the
  whole loop is ~1.5% of a core.
- `display.scroll_speed` (`slow`/`normal`/`fast`) indexes
  `config.SCROLL_SPEEDS` for **pixels per second**; the render loop converts
  to px/frame in the `Config.generation` block so the frame rate can change
  without silently retuning every speed. `scroll_offset` is a **float**
  accumulator — at 60fps every speed is well under a pixel per frame, so an
  int accumulator would simply never move.
- A scrolling title is drawn **sub-pixel**, not with `DrawText`.
  `graphics.DrawText` can only land a glyph on a whole pixel, so a title
  creeping along at 20px/s lurches a full pixel at a time however high the
  frame rate goes — raising the frame rate alone got it from 2px lurches to
  1px, and this is what removes the last of it. `make_title_phases`
  supersamples the string `config.SCROLL_SUBPIXEL`× horizontally and
  box-filters it back down at each of N starting offsets; the loop then does
  `divmod(int(offset * SUB), SUB)` — whole pixels pick the 64px window, the
  remainder picks the phase — and blits with `SetImage`. Cached on the
  `Session` like `make_paused_poster` and carried across polls in
  `State.replace` keyed on the **title**, not `thumb_path`. Measured on the
  Pi: 3ms to build the four phases once per title, 0.144ms per frame against
  0.065ms for the `DrawText` it replaced, so about +0.5% of a core.
  - This works because PIL reads the same BDF via `BdfFontFile` and agrees
    with `text_width` to the pixel (checked across proportional strings), and
    because PIL's `text()` at top=0 lines up with the rgbmatrix cell — blit
    at `title_y - font.baseline`.
  - `load_pil_title_font()` returns None if the BDF will not compile, and the
    scroll branch falls back to whole-pixel `DrawText`. Keep that fallback:
    it is the difference between a slightly chunkier title and a blank half.
  - What the box filter produces is *coverage*, and coverage is not a panel
    value: the matrix library luminance-corrects every byte through CIE1931
    before the PWM sees it, so a stem split across two columns at 128/128 emits
    **37%** of the light the same stem emits on a whole pixel at 255. At
    40px/s a stem crosses a boundary about twenty times a second, which is
    what made a fast scroll pulse. `_COVERAGE_LUT` inverts that curve
    (framebuffer.cc's table, toe included) before the colorize, so partial
    columns come out as bright as whole ones. The smoke test asserts <2%
    spread in *emitted light* across phases — summing 8-bit values instead
    would have called the pulsing version conserved, and did.
- Panel timing was measured and is **not** the lever: `gpio_slowdown` 2 is
  marginally *worse* than the configured 4 (8.60 vs 8.14ms), `pwm_bits` 9
  buys only 6% refresh over 11 and costs two bits of depth, and
  `limit_refresh_rate_hz=120` sits right at what the panel reaches unlimited
  (123Hz), so it is close to a no-op. Leave all three alone; the frame rate
  the loop feeds the panel is the thing that matters.
- The progress bar and time remaining are drawn straight from the polled
  `viewOffset`. **Do not interpolate between polls** — this was tried and
  reverted. PMS only refreshes `viewOffset` about every 10s (measured: clean
  `+10.0` steps with zeros between, on every concurrent session), so with a
  5s poll the interpolated position runs ahead and the next poll hands back
  the same stale value, making the time remaining visibly count down and then
  jump back up. It buys nothing even when it works: `format_remaining` is
  whole minutes above 60s, and the bar is 62px, which on a 90-minute movie is
  ~87s per pixel.
- An unreachable Plex server draws "Can't reach Plex" in the idle branch
  rather than as a `DisplayMode` status page, deliberately: status pages are
  drawn ahead of the schedule gate, and a notice that lights the panel at 3am
  is worse than the wrong caption. `Nothing playing` and `plex_offline` are
  different claims and must not be conflated.
- Vertical positions are **computed**, not hardcoded — `compute_text_layout`
  distributes blocks using each font's real `.height`/`.baseline`. Don't
  reintroduce magic baseline constants. `display.show_user` exploits this:
  hiding the user line passes three fonts instead of four and the remaining
  rows re-spread over the same region, so there is no hole where it was.
  Layout therefore lives in the `Config.generation` block, not at loop start,
  and the `Layout:` log line reprints on every settings save.
- Widths are **measured** per-glyph (`text_width`); fonts may be proportional.
- `wrap_two_lines` never revisits a line once it has moved on (prevents
  silent word reordering).

## Testing without the panel

`tests/on_pi_smoke.py` runs on the Pi and covers the pure logic: it stubs
`rgbmatrix` before import, exercises wrap/layout/state/config/update logic, and
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

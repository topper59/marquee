# Marquee

<img width="2081" height="1279" alt="image" src="https://github.com/user-attachments/assets/c85c6d93-5b13-4dd9-a1bd-6ceb2c2ae71c" />

https://github.com/user-attachments/assets/95c3e3f0-16a0-438e-9ac2-e73b4c57d87b

A now-playing display for Plex on a 128×64 HUB75 LED matrix, driven by a
Raspberry Pi 4 with an Adafruit RGB Matrix Bonnet. One half of the panel is
poster art; the other shows the title, episode, who's watching, time
remaining, and a progress bar.

- **Setup from a phone** — an unconfigured device boots into an open WiFi
  hotspot with a captive-portal wizard: join the WiFi, pick your Plex server
  (or sign in at plex.tv/link), done.
- **Settings web UI** at `http://marquee.local` — session filters (whose
  streams appear), brightness, schedules and dimming, layout, accent colour,
  idle modes, static IP, optional password.
- **Home Assistant** (optional) — dim the panel while the TV is on.
- **Signed software updates** — the device checks GitHub Releases daily
  (opt-out) and installs on request from the settings page, with automatic
  rollback if the new version fails to start. Offline devices can install
  the same `.mqup` file by upload.

## Hardware

- Raspberry Pi 4B + Adafruit RGB Matrix Bonnet (PWM bridge soldered)
- 128×64 HUB75 LED matrix panel
- [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) built
  on the Pi, with its BDF fonts at `/opt/rpi-rgb-led-matrix/fonts`

## Development

The app is the `marquee/` Python package, run as one systemd service
(`marquee.service`) on the Pi; it cannot run on a dev machine (needs
`rgbmatrix`, the panel, and a Plex server). `./deploy.sh` pushes to a Pi
over SSH and restarts. Logic tests: `tests/on_pi_smoke.py` on the Pi,
`node tests/web_ui_test.js` for the settings UI (needs `npm install jsdom`).

Releases are signed update bundles built by `release/build.py`; see
CLAUDE.md for the full architecture notes.

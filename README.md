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
- **Home Assistant** (optional) — dim or blank the panel while the TV is on,
  and drive the panel the other way from HA automations (see below).
- **Panel override** — turn the panel off for an hour, for the evening, or
  until you turn it back on, straight from the home page. Survives a restart.
- **Signed software updates** — the device checks GitHub Releases daily
  (opt-out) and installs on request from the settings page, with automatic
  rollback if the new version fails to start. Offline devices can install
  the same `.mqup` file by upload.

## Controlling the panel from Home Assistant

The Home Assistant integration in the settings page is one-directional: HA
tells the display about your TV. To drive it the other way — an automation
that silences the panel at bedtime, or a dashboard button — call the
device's own API. `state` is `on`, `off`, or `auto` (back to the schedule),
and `minutes` is optional: leave it out, or set it to 0, for "until
something changes it".

```yaml
# configuration.yaml
rest_command:
  marquee_off:
    url: "http://marquee.local/api/panel"
    method: POST
    content_type: "application/json"
    payload: '{"state": "off"}'
  marquee_on:
    url: "http://marquee.local/api/panel"
    method: POST
    content_type: "application/json"
    payload: '{"state": "auto"}'

# Optional: a sensor for whether the panel is lit, and why.
sensor:
  - platform: rest
    name: Marquee
    resource: "http://marquee.local/api/panel"
    value_template: "{{ 'on' if value_json.on else 'off' }}"
    json_attributes: [override, override_until, in_schedule, ha_blank]
```

Any setting on the settings page can be changed the same way by POSTing
`{"display.brightness_normal": 40}` to `/api/settings` — the API takes the
same dotted paths the page uses.

If you have set a settings password, these calls need a session; leave the
password off on a trusted home network if you want HA to drive the panel.

The device refuses state-changing requests that carry another site's `Origin`
header, so a web page you happen to be visiting cannot reach in and change
things. Home Assistant and `curl` send no `Origin` and are unaffected; if you
are driving the API from something browser-based and getting a 403, that is
why. During first-time setup — while the display is running its own open
`Marquee-Setup` network — only the wizard's own endpoints answer at all.

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

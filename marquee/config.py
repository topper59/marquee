"""Configuration store.

All user-tunable settings live in a JSON document (default
/var/lib/marquee/config.json) owned by the Config class. Threads take a
snapshot with get() each loop iteration; update() validates, merges, bumps
`generation`, and persists atomically, so changes from the web UI apply
without a restart for everything except the matrix hardware options.
"""

import os
import json
import copy
import uuid
import logging
import tempfile
import threading
from datetime import time as dtime

from marquee.plex import filters

log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("MARQUEE_CONFIG", "/var/lib/marquee/config.json")

# ─────────────────────────────────────────────────────────────────────────────
# Fixed visual tuning — deliberately not user configuration
# ─────────────────────────────────────────────────────────────────────────────
# Scroll speed in **pixels per second**, keyed by `display.scroll_speed`.
# Per-second rather than per-frame so the frame rate below can change without
# silently retuning every speed; the render loop converts once per settings
# change, not once per frame.
SCROLL_SPEEDS       = {"slow": 10.0, "normal": 20.0, "fast": 40.0}
# The panel refreshes at ~120Hz (measured on the Pi: SwapOnVSync blocks a
# steady 8.33ms), so feeding it 20fps threw away five sixths of the positions
# a moving title could have occupied. 60fps lands on every second panel
# refresh and costs about 1.5% of a core — drawing a frame is 0.26ms. The
# float accumulator in the render loop is what lets the resulting sub-pixel
# step actually move instead of rounding to zero.
SCROLL_FRAME_MS     = 1000 / 60
# Horizontal sub-pixel resolution for the scrolling title. The panel can only
# light whole pixels, but it can light them *partially*, so a glyph edge
# landing between two columns is drawn across both in proportion. 4 phases is
# where the improvement stops being visible on a 7px-wide font; each one costs
# a cached image per title, built in about a millisecond.
SCROLL_SUBPIXEL     = 4
SCROLL_PAUSE_MS     = 1500
IDLE_DIM            = (40, 40, 80)
PROGRESS_BG         = (30, 30, 30)
# The progress bar and the time remaining are shades of `display.accent`, so
# there is one colour to change rather than three to keep in step. The time
# remaining sits a little behind the bar.
REMAIN_SCALE        = 0.75
# Paused overlay: poster is dimmed to this fraction and stamped with two bars.
PAUSE_DIM           = 0.35
PAUSE_FG            = (235, 235, 235)
# Posters are fetched at this multiple of their final size and reduced locally.
POSTER_SUPERSAMPLE  = 4
# Text occupies the panel above this row; dots and progress bar sit below it.
TEXT_REGION_BOTTOM  = 50
DOTS_Y              = 53
BAR_Y0, BAR_Y1      = 59, 61

# Font paths (BDF fonts ship with rpi-rgb-led-matrix)
FONT_DIR  = os.environ.get("FONT_DIR", "/opt/rpi-rgb-led-matrix/fonts")
FONT_BIG  = f"{FONT_DIR}/7x13B.bdf"
FONT_SM   = f"{FONT_DIR}/5x7.bdf"
FONT_SUB  = f"{FONT_DIR}/7x13B.bdf"
FONT_CLK  = f"{FONT_DIR}/7x13B.bdf"


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────
def defaults() -> dict:
    return {
        "version": 1,
        "provisioned": False,
        "device": {
            "name": "Marquee",
            "client_id": "",   # uuid4, generated on first load
        },
        "plex": {
            "url": "",
            "token": "",
            "verify_ssl": False,
            "server_name": "",
            "machine_id": "",
            "poll_seconds": 5,
            "stale_after_failures": 6,
            "http_timeout": 4,
            # Which sessions reach the panel. All empty = show everything,
            # which is what an out-of-box device must do. See plex/filters.py.
            "filter": {
                "users": [],            # allow-list; empty means everyone
                "ignore_users": [],     # deny-list, beats the allow-list
                "players": [],          # allow-list; empty means anything
                "ignore_players": [],
                "media_types": [],      # subset of movie/episode/track/other
                "hide_paused": False,
            },
        },
        "display": {
            "cycle_seconds": 10,
            "brightness_normal": 60,
            "brightness_dim": 20,
            "schedule_start": "00:00",
            "schedule_stop": "00:00",
            # Dim by the clock, with no Home Assistant involved. Equal
            # endpoints mean "never" — the HA flag is then the only source of
            # dimming, which is how every device behaved before this existed.
            "dim_start": "00:00",
            "dim_stop": "00:00",
            # Panel accent: progress bar, time remaining, and the headings on
            # the setup/link screens. The web UI keeps its own amber.
            "accent": "#e5a00d",
            # What the panel does with itself when nothing is playing:
            # "clock", "blank" (dark), or "poster" (hold the last artwork).
            "idle_mode": "clock",
            "clock_24h": False,
            # Which half of the panel holds the poster: "left" (the default)
            # or "right". The text block takes the other half.
            "poster_side": "left",
            # How fast a too-wide title slides: see SCROLL_SPEEDS.
            "scroll_speed": "normal",
            # Whose stream it is. Off is a reasonable default for a
            # one-person server, where the row is a wasted eighth of the
            # panel — the other rows spread out to fill it.
            "show_user": True,
        },
        "ha": {
            "enabled": False,
            "url": "",
            "token": "",
            "tv_entity": "",
            # False acts whenever the TV is on; True (the original behaviour)
            # additionally requires the sun to be below the horizon.
            "require_sunset": True,
            # What the TV being on does to the panel: "dim" (the original
            # behaviour) or "off" — a theater room wants no light at all.
            "tv_action": "dim",
            "poll_seconds": 60,
        },
        "network": {
            # On by default: an out-of-box or factory-reset device with no
            # WiFi of its own must raise its setup AP unprompted. Devices
            # migrated from the env-file era get False (see migration).
            "manage": True,
            "ap_ssid_prefix": "Marquee-Setup",
            "join_timeout_s": 45,
            "boot_connect_timeout_s": 90,
            # Station-mode addressing. "auto" is DHCP; "manual" pushes the
            # four fields below onto the saved WiFi profile. NetManager
            # applies a change and rolls back to DHCP if the device does not
            # come back — wlan0 is the only link, so a typo must not strand it.
            "ipv4_method": "auto",
            "ipv4_address": "",
            "ipv4_prefix": 24,
            "ipv4_gateway": "",
            "ipv4_dns": "",
        },
        "updates": {
            # Ask GitHub about new releases once a day. The check itself is
            # the only phone-home this device has; off means updates arrive
            # only by file upload.
            "auto_check": True,
            # Opt-in: install a found update overnight (03:00–05:00) instead
            # of waiting for someone to press the button.
            "auto_install": False,
        },
        "web": {
            "enabled": True,
            "port": 80,
            "password": None,
            # Appearance of the web UI: "auto" follows the phone or laptop's
            # own light/dark setting. Stored on the device, not per-browser,
            # so the panel's pages look the same from anything you open them
            # on.
            "theme": "auto",
        },
        "matrix": {
            "rows": 64,
            "cols": 128,
            "hardware_mapping": "adafruit-hat-pwm",
            "gpio_slowdown": 4,
            "pwm_bits": 11,
            "pwm_lsb_nanoseconds": 130,
            "limit_refresh_rate_hz": 120,
        },
        "log_level": "INFO",
    }


def hex_to_rgb(value: str) -> tuple:
    """'#e5a00d' → (229, 160, 13). Accepts 3- or 6-digit hex, with or
    without the leading '#', because people paste these from anywhere."""
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"not a colour: {value!r}")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ValueError(f"not a colour: {value!r}") from None


def scale_rgb(rgb: tuple, factor: float) -> tuple:
    """A dimmer shade of the same colour, clamped to the byte range."""
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def parse_hhmm(s: str) -> dtime:
    """'HH:MM' → datetime.time, raising ValueError on anything else."""
    parts = str(s).split(":")
    if len(parts) != 2:
        raise ValueError(f"not HH:MM: {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"out of range: {s!r}")
    return dtime(h, m)


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _int_range(lo, hi):
    def convert(v):
        i = int(v)
        if not (lo <= i <= hi):
            raise ValueError(f"{i} outside [{lo}, {hi}]")
        return i
    return convert


def _url(v):
    s = str(v).strip().rstrip("/")
    if s and not (s.startswith("http://") or s.startswith("https://")):
        raise ValueError(f"not an http(s) URL: {s!r}")
    return s


def _hhmm(v):
    parse_hhmm(v)
    return str(v)


def _log_level(v):
    s = str(v).upper()
    if s not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError(f"bad log level: {v!r}")
    return s


def _ipv4(v):
    """Dotted-quad or empty. Deliberately strict: a bad static address on
    this device's only link is a service call, not a typo."""
    s = str(v).strip()
    if not s:
        return ""
    parts = s.split(".")
    if len(parts) != 4:
        raise ValueError(f"not an IPv4 address: {v!r}")
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255) or (len(p) > 1 and p[0] == "0"):
            raise ValueError(f"not an IPv4 address: {v!r}")
    return s


def _ipv4_list(v):
    """Comma- or space-separated IPv4 addresses, normalized to 'a, b'."""
    raw = str(v).replace(",", " ").split()
    return ", ".join(_ipv4(a) for a in raw)


def _ipv4_method(v):
    s = str(v).strip().lower()
    if s not in ("auto", "manual"):
        raise ValueError(f"unknown IPv4 method: {v!r}")
    return s


def _str_list(v):
    """A rule list. Accepts a JSON list or the settings page's comma-separated
    string, and always stores a list so config.json stays self-describing."""
    return filters.as_list(v)


def _media_types(v):
    out = filters.as_list(v)
    for t in out:
        if t.casefold() not in filters.MEDIA_TYPES:
            raise ValueError(
                f"unknown media type: {t!r} (expected one of "
                f"{', '.join(filters.MEDIA_TYPES)})")
    return [t.casefold() for t in out]


def _hex_color(v):
    """Stored normalized, so the settings page's colour input round-trips."""
    return "#%02x%02x%02x" % hex_to_rgb(v)


def _idle_mode(v):
    s = str(v).strip().lower()
    if s not in ("clock", "blank", "poster"):
        raise ValueError(f"unknown idle mode: {v!r}")
    return s


def _poster_side(v):
    s = str(v).strip().lower()
    if s not in ("left", "right"):
        raise ValueError(f"unknown poster side: {v!r}")
    return s


def _scroll_speed(v):
    s = str(v).strip().lower()
    if s not in SCROLL_SPEEDS:
        raise ValueError(f"unknown scroll speed: {v!r} (expected one of "
                         f"{', '.join(SCROLL_SPEEDS)})")
    return s


def _tv_action(v):
    s = str(v).strip().lower()
    if s not in ("dim", "off"):
        raise ValueError(f"unknown TV action: {v!r}")
    return s


def _theme(v):
    s = str(v).strip().lower()
    if s not in ("auto", "light", "dark"):
        raise ValueError(f"unknown theme: {v!r}")
    return s


def _hw_mapping(v):
    s = str(v)
    if s not in ("adafruit-hat-pwm", "adafruit-hat", "regular"):
        raise ValueError(f"unknown hardware mapping: {v!r}")
    return s


# Dotted path → normalizer. update() rejects any path not listed here.
_VALIDATORS = {
    "provisioned":                _as_bool,
    "device.name":                lambda v: str(v).strip()[:32] or "Marquee",
    "device.client_id":           str,
    "plex.url":                   _url,
    "plex.token":                 str,
    "plex.verify_ssl":            _as_bool,
    "plex.server_name":           str,
    "plex.machine_id":            str,
    "plex.poll_seconds":          _int_range(1, 3600),
    "plex.stale_after_failures":  _int_range(1, 100),
    "plex.http_timeout":          _int_range(1, 60),
    "plex.filter.users":          _str_list,
    "plex.filter.ignore_users":   _str_list,
    "plex.filter.players":        _str_list,
    "plex.filter.ignore_players": _str_list,
    "plex.filter.media_types":    _media_types,
    "plex.filter.hide_paused":    _as_bool,
    "display.cycle_seconds":      _int_range(2, 3600),
    "display.brightness_normal":  _int_range(1, 100),
    "display.brightness_dim":     _int_range(1, 100),
    "display.schedule_start":     _hhmm,
    "display.schedule_stop":      _hhmm,
    "display.dim_start":          _hhmm,
    "display.dim_stop":           _hhmm,
    "display.accent":             _hex_color,
    "display.idle_mode":          _idle_mode,
    "display.clock_24h":          _as_bool,
    "display.poster_side":        _poster_side,
    "display.scroll_speed":       _scroll_speed,
    "display.show_user":          _as_bool,
    "ha.enabled":                 _as_bool,
    "ha.url":                     _url,
    "ha.token":                   str,
    "ha.tv_entity":               str,
    "ha.require_sunset":          _as_bool,
    "ha.tv_action":               _tv_action,
    "ha.poll_seconds":            _int_range(5, 3600),
    "network.manage":             _as_bool,
    "network.ap_ssid_prefix":     lambda v: str(v).strip()[:24] or "Marquee-Setup",
    "network.join_timeout_s":     _int_range(10, 300),
    "network.boot_connect_timeout_s": _int_range(10, 600),
    "network.ipv4_method":        _ipv4_method,
    "network.ipv4_address":       _ipv4,
    "network.ipv4_prefix":        _int_range(1, 32),
    "network.ipv4_gateway":       _ipv4,
    "network.ipv4_dns":           _ipv4_list,
    "updates.auto_check":         _as_bool,
    "updates.auto_install":       _as_bool,
    "web.enabled":                _as_bool,
    "web.port":                   _int_range(1, 65535),
    "web.password":               lambda v: None if v in (None, "") else str(v),
    "web.theme":                  _theme,
    "matrix.rows":                _int_range(8, 256),
    "matrix.cols":                _int_range(8, 512),
    "matrix.hardware_mapping":    _hw_mapping,
    "matrix.gpio_slowdown":       _int_range(0, 10),
    "matrix.pwm_bits":            _int_range(1, 11),
    "matrix.pwm_lsb_nanoseconds": _int_range(50, 3000),
    "matrix.limit_refresh_rate_hz": _int_range(0, 1000),
    "log_level":                  _log_level,
}

def _ipv4_int(addr: str) -> int:
    a, b, c, d = (int(p) for p in addr.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def _same_subnet(addr: str, gateway: str, prefix: int) -> bool:
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (_ipv4_int(addr) & mask) == (_ipv4_int(gateway) & mask)


def _validate_combined(data: dict, paths: set):
    """Cross-field rules, checked on the merged document before it is saved.

    Per-path validators cannot see one another, but a static address is only
    meaningful as a complete set — NetworkManager accepts a half-filled one
    and only then fails to bring the link up, which on this hardware means
    the device drops off the network before anyone can be told why.

    Only rules whose inputs the patch actually touches are enforced, so a
    hand-edited config file cannot make unrelated settings unsavable.
    """
    if any(p.startswith("network.ipv4") for p in paths):
        net = data["network"]
        if net["ipv4_method"] == "manual":
            if not net["ipv4_address"]:
                raise ValueError("a static IP address needs an IP address")
            if not net["ipv4_gateway"]:
                raise ValueError("a static IP address needs a router (gateway) address")
            if not _same_subnet(net["ipv4_address"], net["ipv4_gateway"],
                                net["ipv4_prefix"]):
                raise ValueError(
                    f"the router {net['ipv4_gateway']} is not reachable from "
                    f"{net['ipv4_address']}/{net['ipv4_prefix']} — check the "
                    f"address and prefix")


# Saving one of these requires a service restart to take effect.
RESTART_REQUIRED = {"web.enabled", "web.port"} | {
    k for k in _VALIDATORS if k.startswith("matrix.")
}


# ─────────────────────────────────────────────────────────────────────────────
# Dotted-path helpers
# ─────────────────────────────────────────────────────────────────────────────
def _get_path(d: dict, path: str):
    for part in path.split("."):
        d = d[part]
    return d


def _set_path(d: dict, path: str, value):
    parts = path.split(".")
    for part in parts[:-1]:
        d = d[part]
    d[parts[-1]] = value


def _flatten(d: dict, prefix="") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{p}."))
        else:
            out[p] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.generation = 0
        self._data = defaults()

        self._load()
        if not self._data["device"]["client_id"]:
            self._data["device"]["client_id"] = str(uuid.uuid4())
        self._save_locked()

    def _load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError) as e:
            # Never boot-loop over a corrupt file: keep it for forensics and
            # fall back to defaults.
            log.error("Config file unreadable (%s) — starting from defaults", e)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return False
        # Merge over defaults so new keys appear with sane values on upgrade.
        for path, value in _flatten(stored).items():
            try:
                _get_path(self._data, path)          # known key?
            except KeyError:
                continue
            _set_path(self._data, path, value)
        return True

    def _save_locked(self):
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self) -> dict:
        """Deep snapshot; safe to read without holding any lock."""
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> set:
        """Validate and apply {dotted.path: value}. Returns changed paths.

        Rejects the whole patch on the first invalid entry — settings are
        saved all-or-nothing so the UI can show one clear error. Fields are
        checked individually and then as a merged whole, so a combination
        that is only wrong together (see _validate_combined) never lands.
        """
        normalized = {}
        for path, value in patch.items():
            validator = _VALIDATORS.get(path)
            if validator is None:
                raise ValueError(f"unknown setting: {path}")
            normalized[path] = validator(value)

        changed = set()
        with self._lock:
            # Merge into a candidate first: cross-field rules need to see the
            # patch and the stored settings together, and a rejection must
            # leave nothing behind.
            candidate = copy.deepcopy(self._data)
            for path, value in normalized.items():
                _set_path(candidate, path, value)
            _validate_combined(candidate, set(normalized))

            for path, value in normalized.items():
                if _get_path(self._data, path) != value:
                    _set_path(self._data, path, value)
                    changed.add(path)
            if changed:
                self.generation += 1
                self._save_locked()
        if "log_level" in changed:
            logging.getLogger().setLevel(normalized["log_level"])
        if changed:
            log.info("Config updated: %s", ", ".join(sorted(changed)))
        return changed

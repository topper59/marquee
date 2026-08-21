"""Configuration store.

All user-tunable settings live in a JSON document (default
/var/lib/nowplaying/config.json) owned by the Config class. Threads take a
snapshot with get() each loop iteration; update() validates, merges, bumps
`generation`, and persists atomically, so changes from the web UI apply
without a restart for everything except the matrix hardware options.

On first start with no config file, settings migrate from the legacy
/etc/plex-matrix.env so an already-provisioned device keeps working. A
factory reset suppresses that migration via a marker file.
"""

import os
import json
import copy
import uuid
import logging
import tempfile
import threading
from datetime import time as dtime

log = logging.getLogger("plex-matrix")

CONFIG_PATH = os.environ.get("NOWPLAYING_CONFIG", "/var/lib/nowplaying/config.json")
LEGACY_ENV_PATH = "/etc/plex-matrix.env"
# Written by a factory reset: its presence stops the legacy env file from
# resurrecting the old configuration on the next boot.
NO_MIGRATE_MARKER = "/var/lib/nowplaying/.factory-reset"

# ─────────────────────────────────────────────────────────────────────────────
# Fixed visual tuning — deliberately not user configuration
# ─────────────────────────────────────────────────────────────────────────────
SCROLL_PX_PER_FRAME = 1
SCROLL_FRAME_MS     = 50
SCROLL_PAUSE_MS     = 1500
IDLE_DIM            = (40, 40, 80)
PROGRESS_FG         = (229, 160, 13)
PROGRESS_BG         = (30, 30, 30)
REMAIN_FG           = (170, 120, 20)
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
            "name": "NowPlaying",
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
        },
        "display": {
            "cycle_seconds": 10,
            "brightness_normal": 60,
            "brightness_dim": 20,
            "schedule_start": "00:00",
            "schedule_stop": "00:00",
        },
        "ha": {
            "enabled": False,
            "url": "",
            "token": "",
            "tv_entity": "",
            "poll_seconds": 60,
        },
        "network": {
            # On by default: an out-of-box or factory-reset device with no
            # WiFi of its own must raise its setup AP unprompted. Devices
            # migrated from the env-file era get False (see migration).
            "manage": True,
            "ap_ssid_prefix": "NowPlaying-Setup",
            "join_timeout_s": 45,
            "boot_connect_timeout_s": 90,
        },
        "web": {
            "enabled": True,
            "port": 80,
            "password": None,
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


def _hw_mapping(v):
    s = str(v)
    if s not in ("adafruit-hat-pwm", "adafruit-hat", "regular"):
        raise ValueError(f"unknown hardware mapping: {v!r}")
    return s


# Dotted path → normalizer. update() rejects any path not listed here.
_VALIDATORS = {
    "provisioned":                _as_bool,
    "device.name":                lambda v: str(v).strip()[:32] or "NowPlaying",
    "device.client_id":           str,
    "plex.url":                   _url,
    "plex.token":                 str,
    "plex.verify_ssl":            _as_bool,
    "plex.server_name":           str,
    "plex.machine_id":            str,
    "plex.poll_seconds":          _int_range(1, 3600),
    "plex.stale_after_failures":  _int_range(1, 100),
    "plex.http_timeout":          _int_range(1, 60),
    "display.cycle_seconds":      _int_range(2, 3600),
    "display.brightness_normal":  _int_range(1, 100),
    "display.brightness_dim":     _int_range(1, 100),
    "display.schedule_start":     _hhmm,
    "display.schedule_stop":      _hhmm,
    "ha.enabled":                 _as_bool,
    "ha.url":                     _url,
    "ha.token":                   str,
    "ha.tv_entity":               str,
    "ha.poll_seconds":            _int_range(5, 3600),
    "network.manage":             _as_bool,
    "network.ap_ssid_prefix":     lambda v: str(v).strip()[:24] or "NowPlaying-Setup",
    "network.join_timeout_s":     _int_range(10, 300),
    "network.boot_connect_timeout_s": _int_range(10, 600),
    "web.enabled":                _as_bool,
    "web.port":                   _int_range(1, 65535),
    "web.password":               lambda v: None if v in (None, "") else str(v),
    "matrix.rows":                _int_range(8, 256),
    "matrix.cols":                _int_range(8, 512),
    "matrix.hardware_mapping":    _hw_mapping,
    "matrix.gpio_slowdown":       _int_range(0, 10),
    "matrix.pwm_bits":            _int_range(1, 11),
    "matrix.pwm_lsb_nanoseconds": _int_range(50, 3000),
    "matrix.limit_refresh_rate_hz": _int_range(0, 1000),
    "log_level":                  _log_level,
}

# Saving one of these requires a service restart to take effect.
RESTART_REQUIRED = {"web.enabled", "web.port"} | {
    k for k in _VALIDATORS if k.startswith("matrix.")
}


# ─────────────────────────────────────────────────────────────────────────────
# Legacy env-file migration
# ─────────────────────────────────────────────────────────────────────────────
_ENV_MAP = {
    "PLEX_URL":             ("plex.url", _url),
    "PLEX_TOKEN":           ("plex.token", str),
    "PLEX_VERIFY_SSL":      ("plex.verify_ssl", _as_bool),
    "POLL_SECONDS":         ("plex.poll_seconds", int),
    "STALE_AFTER_FAILURES": ("plex.stale_after_failures", int),
    "HTTP_TIMEOUT":         ("plex.http_timeout", int),
    "CYCLE_SECONDS":        ("display.cycle_seconds", int),
    "BRIGHTNESS_NORMAL":    ("display.brightness_normal", int),
    "BRIGHTNESS_DIM":       ("display.brightness_dim", int),
    "SCHEDULE_START":       ("display.schedule_start", _hhmm),
    "SCHEDULE_STOP":        ("display.schedule_stop", _hhmm),
    "HA_URL":               ("ha.url", _url),
    "HA_TOKEN":             ("ha.token", str),
    "HA_TV_ENTITY":         ("ha.tv_entity", str),
    "HA_POLL_SECONDS":      ("ha.poll_seconds", int),
    "LOG_LEVEL":            ("log_level", _log_level),
}

# The old code baked these into its env-var defaults; a migrated device that
# never overrode them must keep the same effective values.
_LEGACY_DEFAULTS = {
    "plex.url": "https://192.168.1.3:32400",
    "ha.url": "https://ha.example.com",
    "ha.tv_entity": "media_player.living_room_tv",
    "display.schedule_start": "07:00",
    "display.schedule_stop": "00:00",
}


def _migrate_legacy_env(env_path: str, data: dict) -> bool:
    """Fold /etc/plex-matrix.env into `data`. True if the file was read."""
    try:
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    for path, value in _LEGACY_DEFAULTS.items():
        _set_path(data, path, value)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key, raw = key.strip(), raw.strip().strip('"').strip("'")
        target = _ENV_MAP.get(key)
        if target is None:
            continue
        path, convert = target
        try:
            _set_path(data, path, convert(raw))
        except (ValueError, TypeError) as e:
            log.warning("Migration: ignoring %s=%r (%s)", key, raw, e)

    if _get_path(data, "ha.token"):
        _set_path(data, "ha.enabled", True)
    data["provisioned"] = True
    # An upgraded device already has working WiFi set up by other means;
    # never take its network over on the strength of a version bump.
    _set_path(data, "network.manage", False)
    return True


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

        loaded = self._load()
        if not loaded and not os.path.exists(NO_MIGRATE_MARKER):
            if _migrate_legacy_env(LEGACY_ENV_PATH, self._data):
                log.info("Migrated config from %s", LEGACY_ENV_PATH)
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
            # fall back to defaults + migration.
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
        saved all-or-nothing so the UI can show one clear error.
        """
        normalized = {}
        for path, value in patch.items():
            validator = _VALIDATORS.get(path)
            if validator is None:
                raise ValueError(f"unknown setting: {path}")
            normalized[path] = validator(value)

        changed = set()
        with self._lock:
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

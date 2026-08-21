"""Configuration.

Transitional shim: still env-vars-at-import, exactly as the single-file app
read them. The JSON config store replaces the internals of this module next;
callers already import everything from here so only this file changes.
"""

import os
import logging
from datetime import time as dtime

# ─────────────────────────────────────────────────────────────────────────────
# Config (override via env vars)
# ─────────────────────────────────────────────────────────────────────────────
# Plex Media Server. Plex serves HTTPS with a *.plex.direct cert that will not
# match a bare LAN IP, so certificate verification is off by default. Set
# PLEX_VERIFY_SSL=true only if PLEX_URL is the plex.direct hostname (which needs
# working public DNS to resolve).
PLEX_URL        = os.environ.get("PLEX_URL", "https://192.168.1.3:32400").rstrip("/")
PLEX_VERIFY_SSL = os.environ.get("PLEX_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
# Optional. Unset works while this host sits in the PMS "allowed without auth"
# network list; set it to survive that setting changing.
PLEX_TOKEN      = os.environ.get("PLEX_TOKEN", "")

POLL_SECONDS    = int(os.environ.get("POLL_SECONDS", "5"))
# Consecutive failed polls before the panel drops to the idle clock, rather
# than leaving a finished show frozen on screen.
STALE_AFTER     = int(os.environ.get("STALE_AFTER_FAILURES", "6"))
CYCLE_SECONDS   = int(os.environ.get("CYCLE_SECONDS", "10"))
HTTP_TIMEOUT    = int(os.environ.get("HTTP_TIMEOUT", "4"))

# Home Assistant
HA_URL          = os.environ.get("HA_URL", "https://ha.example.com").rstrip("/")
HA_TOKEN        = os.environ["HA_TOKEN"]  # required — long-lived access token
HA_TV_ENTITY    = os.environ.get("HA_TV_ENTITY", "media_player.living_room_tv")
HA_POLL_SECONDS = int(os.environ.get("HA_POLL_SECONDS", "60"))
BRIGHTNESS_NORMAL = int(os.environ.get("BRIGHTNESS_NORMAL", "60"))
BRIGHTNESS_DIM    = int(os.environ.get("BRIGHTNESS_DIM",    "20"))

# Schedule — display is active between START_TIME and STOP_TIME (Pi local time).
# Supports overnight spans (e.g. START=22:00 STOP=06:00) automatically.
# Set both to 00:00 to disable scheduling (always on).
_parse_t = lambda s: dtime(*map(int, s.split(":")))
SCHEDULE_START  = _parse_t(os.environ.get("SCHEDULE_START", "07:00"))
SCHEDULE_STOP   = _parse_t(os.environ.get("SCHEDULE_STOP",  "00:00"))
SCHEDULE_ENABLE = not (SCHEDULE_START == SCHEDULE_STOP == dtime(0, 0))

# Visual tuning
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

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

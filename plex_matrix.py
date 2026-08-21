#!/usr/bin/env python3
"""
Plex now-playing display for 128x64 HUB75 panel on Raspberry Pi + Adafruit RGB Matrix Bonnet.

Layout (mirrors the Matrix Portal S3 build):
  - Left:  64x64 poster art (center-cropped square)
  - Right: 64x64 area for title (scrolling if needed) + subtitle + user + progress bar
  - Idle:  centered clock + "Nothing playing"
  - Multi-session: cycles between active sessions every CYCLE_SECONDS

Data source: Plex Media Server /status/sessions
Image source: Plex /photo/:/transcode (server-side resize, cropped locally)
"""

import io
import os
import time
import signal
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

import requests
import urllib3
from PIL import Image, ImageDraw
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

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
#FONT_BIG  = f"{FONT_DIR}/helvR12.bdf"
FONT_BIG = f"{FONT_DIR}/7x13B.bdf"
FONT_SM   = f"{FONT_DIR}/5x7.bdf"
FONT_SUB  = f"{FONT_DIR}/7x13B.bdf"
FONT_CLK  = f"{FONT_DIR}/7x13B.bdf"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("plex-matrix")


# ─────────────────────────────────────────────────────────────────────────────
# Schedule helper
# ─────────────────────────────────────────────────────────────────────────────
def is_within_schedule() -> bool:
    """Return True if the current local time falls within the active window."""
    if not SCHEDULE_ENABLE:
        return True
    now = datetime.now().time().replace(second=0, microsecond=0)
    start, stop = SCHEDULE_START, SCHEDULE_STOP
    if start <= stop:
        # Normal window: e.g. 07:00 – 23:00 (stop=00:00 treated as end-of-day)
        # Special case: stop at midnight means active until end of day
        if stop == dtime(0, 0):
            return now >= start
        return start <= now < stop
    else:
        # Overnight window: e.g. 22:00 – 06:00
        return now >= start or now < stop


# ─────────────────────────────────────────────────────────────────────────────
# Session model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Session:
    session_key: str
    title: str
    subtitle: str
    user: str
    progress: float
    thumb_path: str
    duration_ms: float = 0.0
    view_offset_ms: float = 0.0
    state: str = "playing"
    poster: Optional[Image.Image] = field(default=None, repr=False)
    # Lazily built from `poster` the first time this session is seen paused.
    poster_paused: Optional[Image.Image] = field(default=None, repr=False)


# ─────────────────────────────────────────────────────────────────────────────
# Plex client
# ─────────────────────────────────────────────────────────────────────────────
class Plex:
    def __init__(self, base_url: str, token: str = "", verify_ssl: bool = False):
        self.base_url = base_url
        self.s = requests.Session()
        self.s.verify = verify_ssl
        if not verify_ssl:
            # Expected: the *.plex.direct cert cannot match a bare LAN IP.
            # Without this urllib3 warns on every poll.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.s.headers.update({"Accept": "application/json"})
        if token:
            self.s.headers["X-Plex-Token"] = token

    def get_activity(self) -> list[Session]:
        r = self.s.get(f"{self.base_url}/status/sessions", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        container = r.json().get("MediaContainer", {})
        sessions = []
        for m in container.get("Metadata", []):
            media_type = m.get("type", "")
            if media_type == "episode":
                title    = m.get("grandparentTitle", "") or m.get("title", "")
                subtitle = f"S{int(m.get('parentIndex', 0) or 0):02d}E{int(m.get('index', 0) or 0):02d}"
                thumb    = m.get("grandparentThumb") or m.get("thumb", "")
            elif media_type == "track":
                title    = m.get("title", "")
                subtitle = m.get("grandparentTitle", "") or m.get("parentTitle", "")
                thumb    = m.get("thumb") or m.get("parentThumb", "")
            else:
                title    = m.get("title", "")
                subtitle = str(m.get("year", "") or "")
                thumb    = m.get("thumb", "")

            duration    = float(m.get("duration", 0) or 0)
            view_offset = float(m.get("viewOffset", 0) or 0)
            progress    = (view_offset / duration) if duration > 0 else 0.0

            sessions.append(Session(
                session_key=str(m.get("sessionKey", "")),
                title=str(title),
                subtitle=str(subtitle),
                user=str((m.get("User") or {}).get("title", "")),
                progress=max(0.0, min(1.0, progress)),
                thumb_path=str(thumb),
                duration_ms=duration,
                view_offset_ms=view_offset,
                state=str((m.get("Player") or {}).get("state", "playing")),
            ))
        return sessions

    def fetch_poster(self, thumb_path: str, size: int = 64) -> Optional[Image.Image]:
        if not thumb_path:
            return None
        try:
            # Fetch oversized and let LANCZOS below do the final reduction.
            # Asking Plex to scale straight to 64px is visibly softer: its own
            # scaler plus a ~2KB JPEG loses detail that survives a local
            # downsample. minSize=1 puts the *short* edge on the requested
            # size, leaving the long edge to be center-cropped below.
            src = size * POSTER_SUPERSAMPLE
            r = self.s.get(
                f"{self.base_url}/photo/:/transcode",
                params={
                    "url": thumb_path,
                    "width": src,
                    "height": src,
                    "minSize": 1,
                    "upscale": 1,
                },
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            w, h = img.size
            if w != h:
                side = min(w, h)
                left = (w - side) // 2
                top  = (h - side) // 2
                img  = img.crop((left, top, left + side, top + side))
            if img.size != (size, size):
                img = img.resize((size, size), Image.LANCZOS)
            return img
        except Exception as e:
            log.warning("Poster fetch failed for %s: %s", thumb_path, e)
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Home Assistant client
# ─────────────────────────────────────────────────────────────────────────────
class HomeAssistant:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def get_state(self, entity_id: str) -> Optional[str]:
        try:
            r = self.s.get(
                f"{self.base_url}/api/states/{entity_id}",
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("state")
        except Exception as e:
            log.warning("HA state fetch failed for %s: %s", entity_id, e)
            return None

    def should_dim(self, tv_entity: str) -> bool:
        tv_state  = self.get_state(tv_entity)
        sun_state = self.get_state("sun.sun")
        tv_on        = tv_state not in (None, "off", "unavailable", "unknown", "standby")
        after_sunset = sun_state == "below_horizon"
        log.debug("HA dim check — tv=%s (%s) sun=%s (%s)",
                  tv_entity, tv_state, "sun.sun", sun_state)
        return tv_on and after_sunset


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions: list[Session] = []
        # The session on screen is tracked by key. Tracking it by list position
        # meant that when any session ended, the index silently landed on a
        # different show mid-dwell.
        self.current_key: Optional[str] = None
        self.last_cycle = time.monotonic()
        self.dim = False

    def _index_of(self, key: Optional[str]) -> Optional[int]:
        """Position of `key` in the current list. Caller must hold the lock."""
        for i, s in enumerate(self.sessions):
            if s.session_key == key:
                return i
        return None

    def replace(self, new_sessions: list[Session]):
        with self.lock:
            old_by_key = {s.session_key: s for s in self.sessions}
            for ns in new_sessions:
                old = old_by_key.get(ns.session_key)
                # Only reuse the art if it is still art for the same thing — a
                # client can roll onto a new item without changing session key.
                if old is not None and old.thumb_path == ns.thumb_path:
                    ns.poster        = old.poster
                    ns.poster_paused = old.poster_paused
            self.sessions = new_sessions
            if self._index_of(self.current_key) is None:
                self.current_key = new_sessions[0].session_key if new_sessions else None

    def maybe_cycle(self):
        with self.lock:
            if len(self.sessions) <= 1:
                return False
            now = time.monotonic()
            if now - self.last_cycle >= CYCLE_SECONDS:
                i = self._index_of(self.current_key)
                i = 0 if i is None else (i + 1) % len(self.sessions)
                self.current_key = self.sessions[i].session_key
                self.last_cycle = now
                return True
        return False

    def current(self) -> Optional[Session]:
        with self.lock:
            if not self.sessions:
                return None
            i = self._index_of(self.current_key)
            return self.sessions[0 if i is None else i]

    def cycle_position(self) -> tuple[int, int]:
        """(index, count) of the on-screen session, for the cycle dots."""
        with self.lock:
            if not self.sessions:
                return 0, 0
            i = self._index_of(self.current_key)
            return (0 if i is None else i), len(self.sessions)


# ─────────────────────────────────────────────────────────────────────────────
# Fetcher thread
# ─────────────────────────────────────────────────────────────────────────────
def fetcher_loop(plex: Plex, state: State, stop: threading.Event):
    failures = 0
    while not stop.is_set():
        try:
            sessions = plex.get_activity()
            state.replace(sessions)
            failures = 0
            with state.lock:
                to_fetch = [(s.session_key, s.thumb_path) for s in state.sessions if s.poster is None]
            for key, thumb in to_fetch:
                if stop.is_set():
                    break
                img = plex.fetch_poster(thumb, size=64)
                with state.lock:
                    for s in state.sessions:
                        if s.session_key == key:
                            s.poster = img
                            break
        except Exception as e:
            failures += 1
            log.warning("Fetch cycle failed (%d in a row): %s", failures, e)
            if failures == STALE_AFTER:
                log.warning("Plex unreachable for ~%ds — clearing the display",
                            failures * POLL_SECONDS)
                state.replace([])
        stop.wait(POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# HA poller thread
# ─────────────────────────────────────────────────────────────────────────────
def ha_poller_loop(ha: HomeAssistant, state: State, stop: threading.Event):
    while not stop.is_set():
        try:
            dim = ha.should_dim(HA_TV_ENTITY)
            with state.lock:
                state.dim = dim
            log.debug("Dim state updated: %s", dim)
        except Exception as e:
            log.warning("HA poll cycle failed: %s", e)
        stop.wait(HA_POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
def make_paused_poster(img: Image.Image) -> Image.Image:
    """Dim the poster and stamp a centered pause glyph over it.

    Built once per session and cached — this is far too slow to run per frame.
    """
    out = img.point(lambda v: int(v * PAUSE_DIM))
    w, h = out.size
    bar_w = max(3, w // 9)
    bar_h = max(8, h // 3)
    gap   = max(2, w // 10)
    top   = (h - bar_h) // 2
    left  = (w - (2 * bar_w + gap)) // 2
    draw  = ImageDraw.Draw(out)
    for i in range(2):
        x0 = left + i * (bar_w + gap)
        draw.rectangle([x0, top, x0 + bar_w - 1, top + bar_h - 1], fill=PAUSE_FG)
    return out


def build_matrix() -> RGBMatrix:
    opts = RGBMatrixOptions()
    opts.rows = 64
    opts.cols = 128
    opts.chain_length = 1
    opts.parallel = 1
    opts.hardware_mapping = "adafruit-hat-pwm"
    opts.gpio_slowdown = 4
    opts.brightness = BRIGHTNESS_NORMAL
    opts.pwm_bits = 11
    opts.pwm_lsb_nanoseconds = 130
    opts.limit_refresh_rate_hz = 120
    opts.drop_privileges = False
    opts.show_refresh_rate = False
    opts.scan_mode = 0
    return RGBMatrix(options=opts)


def load_fonts():
    title   = graphics.Font(); title.LoadFont(FONT_BIG)
    sub     = graphics.Font(); sub.LoadFont(FONT_SM)
    sub_big = graphics.Font(); sub_big.LoadFont(FONT_SUB)
    clk     = graphics.Font(); clk.LoadFont(FONT_CLK)
    return title, sub, sub_big, clk


def wrap_two_lines(text: str, max_chars: int) -> tuple[str, str]:
    """Greedy-wrap `text` into at most two lines of `max_chars`.

    Words are placed in order and never revisited: once a word overflows line
    one, everything after it belongs to line two. Revisiting is what used to
    reorder subtitles — a short word following a long one would jump back up to
    fill the gap ("S04E03 Got" / "Richmond's T"). Anything that will not fit in
    two lines is cut short with an ellipsis.
    """
    lines = ["", ""]
    idx = 0
    for word in text.split():
        candidate = f"{lines[idx]} {word}".strip()
        if len(candidate) <= max_chars:
            lines[idx] = candidate
        elif idx == 0:
            idx = 1
            lines[idx] = word[:max_chars]
        else:
            lines[1] = lines[1][:max_chars - 1].rstrip() + "\u2026"
            break
    return lines[0], lines[1]


def format_remaining(duration_ms: float, view_offset_ms: float) -> str:
    """Time left as '1h04m left' / '42m left' / '38s left'.

    Empty when the server reports no duration, which is normal for live TV.
    """
    if duration_ms <= 0:
        return ""
    secs = max(0, int((duration_ms - view_offset_ms) / 1000))
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours:
        return f"{hours}h{mins:02d}m left"
    if mins:
        return f"{mins}m left"
    return f"{s}s left"


def text_width(font, text: str) -> int:
    """Pixel width of `text` in `font`.

    Sums per-glyph advances rather than assuming a fixed cell, so proportional
    faces (helvR12) measure correctly and monospace ones still come out exact.
    """
    return sum(max(0, font.CharacterWidth(ord(ch))) for ch in text)


def compute_text_layout(fonts, bottom: int = TEXT_REGION_BOTTOM) -> list[int]:
    """Spread each font's text block evenly down the text region.

    Returns one baseline per font. Driven by real font metrics so that swapping
    a font rebalances the panel on its own, rather than needing the baselines
    to be re-tuned by hand.
    """
    total = sum(f.height for f in fonts)
    slots = len(fonts) + 1
    gap, extra = divmod(max(0, bottom - total), slots)
    baselines, y = [], 0
    for i, f in enumerate(fonts):
        # Hand the leftover pixels to the topmost gaps rather than dropping
        # them, so the block stays centred in the region.
        y += gap + (1 if i < extra else 0)
        baselines.append(y + f.baseline)
        y += f.height
    return baselines


def render_loop(matrix: RGBMatrix, state: State, stop: threading.Event):
    _font_big, _font_sm, _font_sub, _font_clk = load_fonts()
    title_y, sub_y, user_y, rem_y = compute_text_layout(
        [_font_big, _font_sub, _font_sm, _font_sm])
    log.info("Layout: title y=%d, subtitle y=%d, user y=%d, remaining y=%d",
             title_y, sub_y, user_y, rem_y)

    canvas = matrix.CreateFrameCanvas()
    white  = graphics.Color(220, 220, 220)
    clk_c  = graphics.Color(*IDLE_DIM)
    sub_c  = graphics.Color(160, 160, 160)
    user_c = graphics.Color(120, 120, 200)

    scroll_offset     = 0
    scroll_dir        = 1
    scroll_pause_until = 0.0
    last_session_key  = None
    last_frame        = time.monotonic()
    last_brightness   = BRIGHTNESS_NORMAL
    was_active        = True   # track transitions for log clarity

    while not stop.is_set():
        now = time.monotonic()
        dt = now - last_frame
        if dt < SCROLL_FRAME_MS / 1000:
            time.sleep((SCROLL_FRAME_MS / 1000) - dt)
        last_frame = time.monotonic()

        # ── Schedule gate ──────────────────────────────────────────────────
        active = is_within_schedule()
        if not active:
            if was_active:
                matrix.Clear()
                log.info("Schedule: outside window, display off (%s–%s)",
                         SCHEDULE_START.strftime("%H:%M"),
                         SCHEDULE_STOP.strftime("%H:%M"))
            was_active = False
            # Sleep longer while blanked — no need to spin at 20fps
            stop.wait(30)
            continue

        if not was_active:
            log.info("Schedule: inside window, display on")
        was_active = True

        # ── Brightness ─────────────────────────────────────────────────────
        state.maybe_cycle()
        with state.lock:
            should_dim = state.dim
        target_brightness = BRIGHTNESS_DIM if should_dim else BRIGHTNESS_NORMAL
        if target_brightness != last_brightness:
            matrix.brightness = target_brightness
            last_brightness = target_brightness
            log.info("Brightness → %d", target_brightness)

        # ── Draw ───────────────────────────────────────────────────────────
        canvas.Clear()
        current = state.current()

        if current is None:
            t  = time.strftime("%I:%M %p")
            x  = (128 - text_width(_font_clk, t)) // 2
            graphics.DrawText(canvas, _font_clk, x, 13, clk_c, t)
            msg = "Nothing playing"
            x2  = (128 - text_width(_font_sm, msg)) // 2
            graphics.DrawText(canvas, _font_sm, x2, 46, graphics.Color(60, 60, 60), msg)
        else:
            RX = 64
            sub_max_chars = 64 // 5

            title = current.title or "—"
            tw    = text_width(_font_big, title)
            if tw <= 64:
                graphics.DrawText(canvas, _font_big,
                                  RX + max(0, (64 - tw) // 2), title_y, white, title)
            else:
                if current.session_key != last_session_key:
                    scroll_offset      = 0
                    scroll_dir         = 1
                    scroll_pause_until = now + (SCROLL_PAUSE_MS / 1000)
                if now >= scroll_pause_until:
                    scroll_offset += SCROLL_PX_PER_FRAME * scroll_dir
                    if scroll_offset >= (tw - 64):
                        scroll_offset      = tw - 64
                        scroll_dir         = -1
                        scroll_pause_until = now + (SCROLL_PAUSE_MS / 1000)
                    elif scroll_offset <= 0:
                        scroll_offset      = 0
                        scroll_dir         = 1
                        scroll_pause_until = now + (SCROLL_PAUSE_MS / 1000)
                graphics.DrawText(canvas, _font_big, RX - scroll_offset, title_y, white, title)

            last_session_key = current.session_key

            if current.poster is not None:
                poster = current.poster
                if current.state == "paused":
                    if current.poster_paused is None:
                        current.poster_paused = make_paused_poster(poster)
                    poster = current.poster_paused
                canvas.SetImage(poster, 0, 0)
            else:
                for py in range(64):
                    for px in range(64):
                        if (px + py) % 8 == 0:
                            canvas.SetPixel(px, py, 30, 30, 30)

            sub_text = current.subtitle or ""
            sw = text_width(_font_sub, sub_text)
            if sw <= 64:
                graphics.DrawText(canvas, _font_sub,
                                  RX + max(0, (64 - sw) // 2), sub_y, sub_c, sub_text)
            else:
                # Two small lines fill exactly the block the single large line
                # would have occupied, so nothing collides with the row below.
                block_top = sub_y - _font_sub.baseline
                line1_y   = block_top + _font_sm.baseline
                line1, line2 = wrap_two_lines(sub_text, sub_max_chars)
                graphics.DrawText(canvas, _font_sm, RX + 1, line1_y, sub_c, line1)
                if line2:
                    graphics.DrawText(canvas, _font_sm, RX + 1,
                                      line1_y + _font_sm.height, sub_c, line2)

            user = (current.user or "")[:sub_max_chars]
            graphics.DrawText(canvas, _font_sm, RX + 1, user_y, user_c, user)

            remaining = format_remaining(current.duration_ms, current.view_offset_ms)
            if remaining:
                graphics.DrawText(canvas, _font_sm, RX + 1, rem_y,
                                  graphics.Color(*REMAIN_FG), remaining)

            bar_x0, bar_x1 = 65, 126
            bar_y0, bar_y1 = BAR_Y0, BAR_Y1
            for bx in range(bar_x0, bar_x1 + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *PROGRESS_BG)
            fill = int((bar_x1 - bar_x0) * current.progress)
            for bx in range(bar_x0, bar_x0 + fill + 1):
                for by in range(bar_y0, bar_y1 + 1):
                    canvas.SetPixel(bx, by, *PROGRESS_FG)

            idx, n = state.cycle_position()
            if n > 1:
                for i in range(n):
                    dx    = 127 - (n - 1 - i) * 3
                    color = (200, 200, 200) if i == idx else (60, 60, 60)
                    canvas.SetPixel(dx, DOTS_Y, *color)
                    canvas.SetPixel(dx, DOTS_Y + 1, *color)

        canvas = matrix.SwapOnVSync(canvas)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    log.info("Starting plex-matrix (schedule: %s, %s→%s)",
             "enabled" if SCHEDULE_ENABLE else "disabled",
             SCHEDULE_START.strftime("%H:%M"),
             SCHEDULE_STOP.strftime("%H:%M"))

    matrix   = build_matrix()
    state    = State()
    plex     = Plex(PLEX_URL, PLEX_TOKEN, PLEX_VERIFY_SSL)
    ha       = HomeAssistant(HA_URL, HA_TOKEN)
    stop     = threading.Event()

    def handle_signal(signum, frame):
        log.info("Signal %d received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    fetcher   = threading.Thread(target=fetcher_loop,   args=(plex, state, stop),     daemon=True)
    ha_poller = threading.Thread(target=ha_poller_loop, args=(ha, state, stop),       daemon=True)
    fetcher.start()
    ha_poller.start()

    try:
        render_loop(matrix, state, stop)
    finally:
        stop.set()
        fetcher.join(timeout=2)
        ha_poller.join(timeout=2)
        matrix.Clear()
        log.info("Stopped")


if __name__ == "__main__":
    main()

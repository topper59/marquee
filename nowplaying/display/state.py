"""Shared session model and lock-guarded display state."""

import time
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image


class DisplayMode(Enum):
    """What the panel is showing. NORMAL is the playing/idle display; the
    rest are full-screen status pages owned by provisioning/auth flows."""
    NORMAL     = "normal"
    SETUP      = "setup"        # AP up: shows SSID + portal URL
    NEEDS_SETUP = "needs_setup"  # online but no Plex server yet: shows address
    CONNECTING = "connecting"   # joining WiFi
    LINK_CODE  = "link_code"    # plex.tv/link code entry
    ERROR      = "error"        # short failure text
    INFO       = "info"         # button-press info page (IP, hostname)
    RESETTING  = "resetting"    # factory-reset hold countdown


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
    # Who/what is playing it, kept for the session filter (see plex/filter.py).
    player: str = ""
    media_type: str = ""
    poster: Optional[Image.Image] = field(default=None, repr=False)
    # Lazily built from `poster` the first time this session is seen paused.
    poster_paused: Optional[Image.Image] = field(default=None, repr=False)
    # Lazily built the first time a too-wide title has to scroll: the same
    # string pre-rendered at each horizontal sub-pixel phase. See
    # render.make_title_phases.
    title_phases: Optional[list] = field(default=None, repr=False)


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
        # True once Plex has failed enough polls in a row to be considered
        # gone. The idle clock alone would claim nothing is playing, which is
        # a different — and wrong — thing to tell someone.
        self.plex_offline = False
        # Artwork from the last thing that played, kept so the "poster" idle
        # mode has something to show. One image, replaced not accumulated.
        self.last_poster: Optional[Image.Image] = None
        self.mode = DisplayMode.NORMAL
        self.mode_payload: dict = {}

    def set_mode(self, mode: DisplayMode, **payload):
        with self.lock:
            if self.mode != mode or self.mode_payload != payload:
                self.mode = mode
                self.mode_payload = payload

    def get_mode(self) -> tuple[DisplayMode, dict]:
        with self.lock:
            return self.mode, dict(self.mode_payload)

    def clear_mode(self, *expected: DisplayMode):
        """Back to NORMAL, but only from `expected` modes — so e.g. the info
        page timing out cannot stomp a setup screen another thread raised."""
        with self.lock:
            if self.mode in expected:
                self.mode = DisplayMode.NORMAL
                self.mode_payload = {}

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
                # The title strip is keyed on the title, not the artwork —
                # they change independently, and rebuilding it every poll
                # would throw away the cache the render loop depends on.
                if old is not None and old.title == ns.title:
                    ns.title_phases = old.title_phases
            # Going empty is the moment to remember what was on — after this
            # the session it came from is gone.
            if not new_sessions and self.sessions:
                prev = self.sessions[self._index_of(self.current_key) or 0]
                if prev.poster is not None:
                    self.last_poster = prev.poster
            self.sessions = new_sessions
            if self._index_of(self.current_key) is None:
                self.current_key = new_sessions[0].session_key if new_sessions else None

    def maybe_cycle(self, cycle_seconds: float):
        with self.lock:
            if len(self.sessions) <= 1:
                return False
            now = time.monotonic()
            if now - self.last_cycle >= cycle_seconds:
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

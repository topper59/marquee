"""Shared session model and lock-guarded display state."""

import time
import threading
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from nowplaying import config


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
            if now - self.last_cycle >= config.CYCLE_SECONDS:
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

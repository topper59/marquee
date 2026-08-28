"""Artwork galleries for the two cycling idle modes.

Two lists of {title, subtitle, thumb_path} plus the 64x64 art to go with them:

  recently played   what this display has actually shown, newest first. Written
                    by the fetcher as sessions appear, so it is a record of the
                    room rather than of the server.
  recently added    what the Plex server has just gained, newest first. Pulled
                    from Plex on a slow timer.

Only the played list is persisted. It is the one that cannot be rebuilt — a
restart would otherwise throw away the history of the room — while recently
added is a copy of something the server can always be asked for again.

Art is cached as PNG under STATE_DIR/art/<slug>/, keyed by a hash of the Plex
thumb path. A 64x64 poster is about 4KB, so the whole cache is a few hundred
KB, and holding it on disk rather than in RAM is what lets the gallery come
straight back after a restart with Plex still waking up. Each gallery gets its
own subdirectory: prune_art() removes whatever the gallery's own items no
longer reference, so two galleries sharing a directory would delete each
other's posters on every poll.
"""

import hashlib
import json
import logging
import os
import threading
import time

from PIL import Image

log = logging.getLogger(__name__)

STATE_DIR = os.environ.get("MARQUEE_STATE_DIR", "/var/lib/marquee")
ART_DIR = os.path.join(STATE_DIR, "art")

# How many items each gallery holds. Twelve is well past what anyone watches
# in a sitting, and at ~4KB of art each the whole cache stays trivial.
MAX_ITEMS = 12


def _art_key(thumb_path: str) -> str:
    """A filename for this artwork. Plex thumb paths carry slashes and query
    strings, and change when the art does — which is exactly the invalidation
    behaviour wanted, so hash the whole thing."""
    return hashlib.sha256(thumb_path.encode("utf-8", "replace")).hexdigest()[:16]


class Gallery:
    """One list of items and the art cache behind it.

    Every public method is safe to call from any thread. The render loop only
    ever calls `items()`, which hands back a snapshot — it must never block on
    a disk read while a frame is due.
    """

    def __init__(self, name: str, slug: str, persist: bool = False,
                 max_items: int = MAX_ITEMS):
        self.name = name
        self.persist = persist
        self.max_items = max_items
        # Each gallery owns its own art directory. Sharing one was a bug with
        # teeth: prune_art() deletes whatever the gallery's own items do not
        # reference, so two galleries in one directory delete each other's
        # posters on every poll and neither ever accumulates any.
        self.art_dir = os.path.join(ART_DIR, slug)
        self.history_path = os.path.join(STATE_DIR, f"history-{slug}.json")
        self._lock = threading.Lock()
        self._items: list[dict] = []
        # thumb_path → Image, populated lazily from the disk cache. Bounded by
        # max_items because pruning drops anything no item refers to.
        self._art: dict[str, Image.Image] = {}
        if persist:
            self._load()

    # ── contents ─────────────────────────────────────────────────────────
    def items(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, title: str, subtitle: str, thumb_path: str) -> bool:
        """Record an item at the front. Returns True if the list changed.

        A repeat of whatever is already newest is ignored: the fetcher calls
        this on every poll, and an hour-long film would otherwise fill the
        whole gallery with itself.
        """
        title = str(title or "").strip()
        if not title:
            return False
        with self._lock:
            if self._items and self._items[0]["title"] == title:
                return False
            self._items = [
                i for i in self._items if i["title"] != title
            ]
            self._items.insert(0, {"title": title,
                                   "subtitle": str(subtitle or ""),
                                   "thumb_path": str(thumb_path or ""),
                                   "at": time.time()})
            del self._items[self.max_items:]
        if self.persist:
            self._save()
        return True

    def replace(self, entries: list[dict]) -> None:
        """Set the whole list (recently-added, refreshed from the server)."""
        clean = [{"title": str(e.get("title") or ""),
                  "subtitle": str(e.get("subtitle") or ""),
                  "thumb_path": str(e.get("thumb_path") or ""),
                  "at": e.get("at") or time.time()}
                 for e in entries if (e.get("title") or "").strip()]
        with self._lock:
            self._items = clean[:self.max_items]
        if self.persist:
            self._save()

    def missing_art(self) -> list[str]:
        """Thumb paths with no artwork yet — what the fetcher should go get."""
        with self._lock:
            return [i["thumb_path"] for i in self._items
                    if i["thumb_path"] and i["thumb_path"] not in self._art]

    # ── artwork ──────────────────────────────────────────────────────────
    def art(self, thumb_path: str):
        """Cached artwork for `thumb_path`, or None. Never touches the disk —
        the render loop calls this."""
        with self._lock:
            return self._art.get(thumb_path)

    def put_art(self, thumb_path: str, img) -> None:
        if img is None or not thumb_path:
            return
        with self._lock:
            self._art[thumb_path] = img
        self._write_art(thumb_path, img)

    def _art_path(self, thumb_path: str) -> str:
        return os.path.join(self.art_dir, _art_key(thumb_path) + ".png")

    def _write_art(self, thumb_path: str, img) -> None:
        try:
            os.makedirs(self.art_dir, exist_ok=True)
            tmp = self._art_path(thumb_path) + ".tmp"
            img.save(tmp, "PNG")
            os.replace(tmp, self._art_path(thumb_path))
        except OSError as e:
            # A full or read-only disk must not stop the panel drawing.
            log.warning("Could not cache artwork: %s", e)

    def load_cached_art(self) -> int:
        """Pull any already-cached art off the disk into memory. Called once
        at startup so a restart does not blank the gallery until Plex answers."""
        loaded = 0
        for item in self.items():
            thumb = item["thumb_path"]
            if not thumb or self.art(thumb) is not None:
                continue
            try:
                with Image.open(self._art_path(thumb)) as f:
                    img = f.convert("RGB")
            except (OSError, ValueError):
                continue
            with self._lock:
                self._art[thumb] = img
            loaded += 1
        return loaded

    def prune_art(self) -> None:
        """Drop cached art, in memory and on disk, that no item refers to."""
        keep = {i["thumb_path"] for i in self.items() if i["thumb_path"]}
        with self._lock:
            for thumb in [t for t in self._art if t not in keep]:
                del self._art[thumb]
        wanted = {_art_key(t) + ".png" for t in keep}
        try:
            for name in os.listdir(self.art_dir):
                if name.endswith(".png") and name not in wanted:
                    os.unlink(os.path.join(self.art_dir, name))
        except OSError:
            pass

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.history_path, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(stored, list):
            with self._lock:
                self._items = [i for i in stored
                               if isinstance(i, dict) and i.get("title")
                               ][:self.max_items]

    def _save(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items(), f)
            os.replace(tmp, self.history_path)
        except OSError as e:
            log.warning("Could not save play history: %s", e)

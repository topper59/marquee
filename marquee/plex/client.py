"""Plex Media Server client and the session-fetcher thread."""

import io
import logging
import threading
import time
from typing import Optional

import requests
import urllib3
from PIL import Image

from marquee import config
from marquee.plex.filters import apply_filter
from marquee.display.state import Session, State

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Plex client
# ─────────────────────────────────────────────────────────────────────────────
class Plex:
    def __init__(self, base_url: str, token: str = "", verify_ssl: bool = False,
                 timeout: float = 4):
        self.base_url = base_url
        self.timeout = timeout
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
        r = self.s.get(f"{self.base_url}/status/sessions", timeout=self.timeout)
        r.raise_for_status()
        container = r.json().get("MediaContainer", {})
        sessions = []
        for m in container.get("Metadata", []):
            media_type = m.get("type", "")
            album = ""
            if media_type == "episode":
                title    = m.get("grandparentTitle", "") or m.get("title", "")
                subtitle = f"S{int(m.get('parentIndex', 0) or 0):02d}E{int(m.get('index', 0) or 0):02d}"
                thumb    = m.get("grandparentThumb") or m.get("thumb", "")
            elif media_type == "track":
                # grandparentTitle is the artist, parentTitle the album. Both
                # earn a row of their own on the music layout, so unlike the
                # video cases the album does not have to squeeze into subtitle.
                title    = m.get("title", "")
                subtitle = m.get("grandparentTitle", "") or m.get("parentTitle", "")
                album    = m.get("parentTitle", "")
                thumb    = m.get("thumb") or m.get("parentThumb", "")
            else:
                title    = m.get("title", "")
                subtitle = str(m.get("year", "") or "")
                thumb    = m.get("thumb", "")

            duration    = float(m.get("duration", 0) or 0)
            view_offset = float(m.get("viewOffset", 0) or 0)
            progress    = (view_offset / duration) if duration > 0 else 0.0

            player = m.get("Player") or {}
            sessions.append(Session(
                session_key=str(m.get("sessionKey", "")),
                title=str(title),
                subtitle=str(subtitle),
                user=str((m.get("User") or {}).get("title", "")),
                progress=max(0.0, min(1.0, progress)),
                thumb_path=str(thumb),
                duration_ms=duration,
                view_offset_ms=view_offset,
                state=str(player.get("state", "playing")),
                player=str(player.get("title", "") or player.get("product", "")),
                media_type=media_type,
                album=str(album),
            ))
        return sessions

    def recently_added(self, limit: int = 12) -> list[dict]:
        """Newest movies and TV on the server, as gallery entries.

        Plex answers /library/recentlyAdded with a mix of movies, seasons and
        albums. Seasons are what a TV library actually reports, so the show
        name comes off parentTitle/grandparentTitle and the season becomes the
        subtitle — "Andor / Season 2" reads better on a 64px panel than the
        raw title Plex hands back, which is just "Season 2".
        """
        r = self.s.get(f"{self.base_url}/library/recentlyAdded",
                       params={"X-Plex-Container-Start": 0,
                               "X-Plex-Container-Size": limit * 3},
                       timeout=self.timeout)
        r.raise_for_status()
        out = []
        for m in r.json().get("MediaContainer", {}).get("Metadata", []):
            kind = m.get("type", "")
            if kind == "movie":
                title    = m.get("title", "")
                subtitle = str(m.get("year", "") or "")
                thumb    = m.get("thumb", "")
            elif kind in ("season", "episode", "show"):
                title    = (m.get("grandparentTitle") or m.get("parentTitle")
                            or m.get("title", ""))
                subtitle = m.get("title", "") if kind != "show" else ""
                thumb    = (m.get("grandparentThumb") or m.get("parentThumb")
                            or m.get("thumb", ""))
            else:
                continue   # albums, clips, photos — not what this shows
            if title:
                out.append({"title": str(title), "subtitle": str(subtitle),
                            "thumb_path": str(thumb)})
            if len(out) >= limit:
                break
        return out

    def fetch_poster(self, thumb_path: str, size: int = 64) -> Optional[Image.Image]:
        if not thumb_path:
            return None
        try:
            # Fetch oversized and let LANCZOS below do the final reduction.
            # Asking Plex to scale straight to 64px is visibly softer: its own
            # scaler plus a ~2KB JPEG loses detail that survives a local
            # downsample. minSize=1 puts the *short* edge on the requested
            # size, leaving the long edge to be center-cropped below.
            src = size * config.POSTER_SUPERSAMPLE
            r = self.s.get(
                f"{self.base_url}/photo/:/transcode",
                params={
                    "url": thumb_path,
                    "width": src,
                    "height": src,
                    "minSize": 1,
                    "upscale": 1,
                },
                timeout=self.timeout,
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
# Fetcher thread
# ─────────────────────────────────────────────────────────────────────────────
# How often the recently-added gallery is refreshed. A library gains items
# a few times a week at most, so this is deliberately slow — it is a decoration
# on an idle screen, not something anyone is waiting on.
RECENT_ADDED_REFRESH_S = 15 * 60


def _gallery_upkeep(plex: "Plex", cfg: dict, state: State,
                    stop: threading.Event, last_added_refresh: float) -> float:
    """Keep whichever gallery the idle screen is set to fed with artwork.

    Both galleries are inert until selected: a device showing the clock has no
    reason to pull posters for things nobody is looking at. Returns the new
    recently-added refresh stamp.
    """
    from marquee import config as cfgmod

    idle_mode = cfg["display"]["idle_mode"]
    if idle_mode not in cfgmod.GALLERY_MODES:
        return last_added_refresh

    gallery = (state.recent_added if idle_mode == "recent_added"
               else state.recent_played)

    if idle_mode == "recent_added":
        now = time.monotonic()
        if now - last_added_refresh >= RECENT_ADDED_REFRESH_S or not len(gallery):
            last_added_refresh = now
            try:
                gallery.replace(plex.recently_added(gallery.max_items))
                gallery.prune_art()
            except Exception as e:
                # An empty gallery falls back to the clock; it is not an outage.
                log.warning("Recently-added refresh failed: %s", e)

    for thumb in gallery.missing_art():
        if stop.is_set():
            break
        gallery.put_art(thumb, plex.fetch_poster(thumb, size=64))
    return last_added_refresh


def fetcher_loop(config, state: State, stop: threading.Event):
    plex = None
    plex_sig = None
    failures = 0
    offline = False
    last_added_refresh = 0.0
    # Art cached on disk from before the restart, so a gallery is not blank
    # while Plex wakes up.
    state.recent_played.load_cached_art()
    while not stop.is_set():
        cfg = config.get()
        pc = cfg["plex"]
        if not pc["url"]:
            # Unprovisioned: nothing to poll yet, and nothing to be offline
            # from — the panel shows the setup prompt in this state.
            state.replace([])
            offline = False
            with state.lock:
                state.plex_offline = False
            stop.wait(pc["poll_seconds"])
            continue
        sig = (pc["url"], pc["token"], pc["verify_ssl"], pc["http_timeout"])
        if sig != plex_sig:
            plex = Plex(pc["url"], pc["token"], pc["verify_ssl"], pc["http_timeout"])
            plex_sig = sig
            failures = 0
            offline = False
            # A different server gets a clean slate: the old one's failures
            # say nothing about this one.
            with state.lock:
                state.plex_offline = False
        try:
            sessions = apply_filter(plex.get_activity(), pc.get("filter") or {})
            state.replace(sessions)
            # What the panel showed is the history worth keeping — the filter
            # has already had its say, so someone else's stream in another
            # room never lands in this room's gallery.
            for sess in sessions:
                if state.recent_played.add(sess.title, sess.subtitle,
                                           sess.thumb_path):
                    state.recent_played.prune_art()
            failures = 0
            offline = False
            with state.lock:
                state.plex_offline = False
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
                # The gallery wants the same 64x64 art, and this is the one
                # moment it is already in hand.
                state.recent_played.put_art(thumb, img)
            last_added_refresh = _gallery_upkeep(plex, cfg, state, stop,
                                                 last_added_refresh)
        except Exception as e:
            failures += 1
            log.warning("Fetch cycle failed (%d in a row): %s", failures, e)
            # >= rather than ==, and latched: an exact match silently never
            # fired if the threshold was lowered while failures were already
            # past it, leaving the panel claiming "Nothing playing" forever.
            if failures >= pc["stale_after_failures"] and not offline:
                offline = True
                log.warning("Plex unreachable for ~%ds — saying so on the panel",
                            failures * pc["poll_seconds"])
                state.replace([])
                with state.lock:
                    state.plex_offline = True
        stop.wait(pc["poll_seconds"])

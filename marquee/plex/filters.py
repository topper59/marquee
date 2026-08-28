"""Which Plex sessions the panel is allowed to show.

A Plex server reports *every* active session, so without this the display
happily cycles through someone else's phone in another room. The rules are
deliberately simple and all optional — an out-of-box device shows everything,
which is what someone who has never opened this page expects.

Each rule is a list of names:

  users / players    allow-lists. Empty means "everyone" / "anything";
                     non-empty means only these.
  ignore_users /
  ignore_players     deny-lists, applied after the allow-lists and winning
                     over them, so "only these two people, but never the
                     kitchen tablet" is expressible.
  media_types        allow-list over movie/episode/track/other.
  hide_paused        drop sessions that are not actively playing.

Matching is case-insensitive and ignores surrounding whitespace, because
these are typed by hand into a phone.
"""

# Plex reports many item types; the panel only distinguishes the three it
# renders differently, and everything else (clip, photo, trailer…) is "other".
KNOWN_TYPES = ("movie", "episode", "track")
MEDIA_TYPES = KNOWN_TYPES + ("other",)


def as_list(value) -> list[str]:
    """Normalize a rule to a list of non-empty strings.

    Accepts a real list (from config.json) or the comma-separated string the
    settings page sends, so both round-trip through the same validator.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return [s for s in (str(p).strip() for p in parts) if s]


def _folded(value) -> set:
    return {s.casefold() for s in as_list(value)}


def bucket(media_type: str) -> str:
    """Collapse a Plex item type onto one of MEDIA_TYPES."""
    t = str(media_type or "").strip().casefold()
    return t if t in KNOWN_TYPES else "other"


def compile_filter(filt: dict) -> dict:
    """The rule lists as folded sets, ready to test sessions against.

    Built once per poll rather than once per rule per session: allowed() used
    to re-fold all five lists for every session it looked at.
    """
    return {
        "hide_paused": bool(filt.get("hide_paused")),
        "ignore_users": _folded(filt.get("ignore_users")),
        "ignore_players": _folded(filt.get("ignore_players")),
        "users": _folded(filt.get("users")),
        "players": _folded(filt.get("players")),
        "media_types": _folded(filt.get("media_types")),
    }


def allowed(session, filt: dict) -> bool:
    """True if `session` passes every rule in `filt` (a raw config filter)."""
    return _allowed(session, compile_filter(filt))


def _allowed(session, rules: dict) -> bool:
    if rules["hide_paused"] and session.state != "playing":
        return False

    user = str(session.user or "").casefold()
    player = str(session.player or "").casefold()

    # Deny beats allow, so the deny-lists are checked first and unconditionally.
    if user and user in rules["ignore_users"]:
        return False
    if player and player in rules["ignore_players"]:
        return False

    if rules["users"] and user not in rules["users"]:
        return False
    if rules["players"] and player not in rules["players"]:
        return False

    if rules["media_types"] and bucket(session.media_type) not in rules["media_types"]:
        return False

    return True


def apply_filter(sessions: list, filt: dict) -> list:
    """The subset of `sessions` the panel should cycle through.

    An empty/absent filter is the identity, so this is always safe to call.
    """
    if not filt:
        return list(sessions)
    rules = compile_filter(filt)
    return [s for s in sessions if _allowed(s, rules)]

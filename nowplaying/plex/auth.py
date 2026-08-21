"""plex.tv PIN ("link") authentication and account resources lookup.

The device can't do a browser OAuth redirect — during setup the user's phone
is trapped in a captive portal — so it uses the link flow: create a PIN, show
the 4-character code on the portal page and the LED panel, and poll until the
user enters it at https://plex.tv/link from any signed-in device.

Everything here talks to plex.tv over verified TLS — unlike the LAN client,
where verification is off because of the *.plex.direct certificate.
"""

import logging

import requests

import nowplaying

log = logging.getLogger("plex-matrix")

PLEX_TV = "https://plex.tv"
TIMEOUT = 10
PRODUCT = "NowPlaying Display"


def _headers(client_id: str, token: str = "") -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": nowplaying.__version__,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Device-Name": PRODUCT,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def pin_create(client_id: str) -> dict:
    """Create a link PIN. Returns {"id": int, "code": "ABCD"}."""
    r = requests.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "false"},
                      headers=_headers(client_id), timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    log.info("Plex link PIN created (id=%s)", d.get("id"))
    return {"id": d["id"], "code": d["code"]}


def pin_poll(client_id: str, pin_id: int) -> dict:
    """Check a PIN. Returns {"token": str_or_empty, "expired": bool}."""
    r = requests.get(f"{PLEX_TV}/api/v2/pins/{pin_id}",
                     headers=_headers(client_id), timeout=TIMEOUT)
    if r.status_code == 404:
        return {"token": "", "expired": True}
    r.raise_for_status()
    d = r.json()
    return {"token": d.get("authToken") or "", "expired": False}


def account_servers(client_id: str, token: str) -> list[dict]:
    """Servers on the signed-in account, for when LAN discovery finds nothing.

    Returns [{name, machine_id, connections: [uri, ...]}] with local
    connections first — relay URIs are excluded (too slow for poster art).
    """
    r = requests.get(f"{PLEX_TV}/api/v2/resources",
                     params={"includeHttps": "1", "includeRelay": "0"},
                     headers=_headers(client_id, token), timeout=TIMEOUT)
    r.raise_for_status()
    servers = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        conns = sorted(res.get("connections") or [],
                       key=lambda c: not c.get("local", False))
        servers.append({
            "name": res.get("name", ""),
            "machine_id": res.get("clientIdentifier", ""),
            "connections": [c["uri"] for c in conns if c.get("uri")],
        })
    return servers

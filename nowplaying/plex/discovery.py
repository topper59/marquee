"""Finding and validating Plex servers.

GDM (Good Day Mate) is Plex's LAN discovery: servers answer a UDP broadcast on
port 32414 with an HTTP-ish header blob. Broadcast only crosses the local
subnet, so a server on another VLAN needs manual entry or the plex.tv
resources lookup after sign-in — the wizard offers all three paths.
"""

import time
import socket
import logging

import requests
import urllib3

log = logging.getLogger("plex-matrix")

GDM_ADDR = ("239.0.0.250", 32414)
GDM_MSG = b"M-SEARCH * HTTP/1.1\r\n\r\n"


def gdm_discover(timeout: float = 2.0) -> list[dict]:
    """Broadcast GDM and collect responding servers.

    Returns [{name, ip, port, machine_id, url}] — url is the https guess;
    probe_server() settles the scheme.
    """
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        for dest in (("<broadcast>", GDM_ADDR[1]), GDM_ADDR):
            try:
                sock.sendto(GDM_MSG, dest)
            except OSError as e:
                log.debug("GDM send to %s failed: %s", dest, e)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            headers = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                k, _, v = line.partition(":")
                if v:
                    headers[k.strip().lower()] = v.strip()
            if headers.get("content-type") != "plex/media-server":
                continue
            port = int(headers.get("port", "32400") or 32400)
            found[ip] = {
                "name": headers.get("name", ip),
                "ip": ip,
                "port": port,
                "machine_id": headers.get("resource-identifier", ""),
                "url": f"https://{ip}:{port}",
            }
    finally:
        sock.close()
    log.info("GDM discovery found %d server(s)", len(found))
    return list(found.values())


def probe_server(url: str, token: str = "", timeout: float = 4) -> dict:
    """Validate a Plex server URL and learn whether it needs auth.

    Tries the URL as given; on TLS/connection failure of an https URL, retries
    the http form once. Returns {ok, url, name?, machine_id, version,
    auth_required, error?} — auth_required reflects /status/sessions, which is
    what the app actually polls (200 unauthenticated means no token needed).
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}"
    if ":" not in url.split("://", 1)[1]:
        url = f"{url}:32400"

    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://"):])

    headers = {"Accept": "application/json"}
    if token:
        headers["X-Plex-Token"] = token
    last_err = None
    for candidate in candidates:
        try:
            r = requests.get(f"{candidate}/identity", headers=headers,
                             timeout=timeout, verify=False)
            r.raise_for_status()
            ident = r.json().get("MediaContainer", {})
        except Exception as e:
            last_err = e
            continue
        result = {
            "ok": True,
            "url": candidate,
            "machine_id": ident.get("machineIdentifier", ""),
            "version": ident.get("version", ""),
            "auth_required": False,
        }
        try:
            s = requests.get(f"{candidate}/status/sessions", headers=headers,
                             timeout=timeout, verify=False)
            if s.status_code == 401:
                result["auth_required"] = True
            elif token and s.ok:
                result["token_ok"] = True
        except Exception as e:
            log.debug("Sessions probe on %s failed: %s", candidate, e)
        return result
    return {"ok": False, "url": url, "error": str(last_err)}

"""Web UI: post-setup settings page (and, in setup mode, the wizard/portal).

Runs as a daemon thread inside the display process via werkzeug's make_server
(no reloader, clean shutdown). A failure here must never take down rendering —
app.py wraps startup, and all handlers only touch the Config/State objects.
"""

import hashlib
import logging
import secrets
import subprocess
import threading
import time

from flask import Flask, jsonify, request, render_template, session

from flask import redirect

import nowplaying
from nowplaying.config import RESTART_REQUIRED
from nowplaying.display.state import DisplayMode
from nowplaying.netmgr import AP_IP
from nowplaying.plex import auth as plex_auth
from nowplaying.plex.discovery import gdm_discover, probe_server

# Paths OSes fetch to detect a captive portal; answering with a redirect to
# the portal is what pops the sign-in sheet.
CAPTIVE_PROBES = ("/generate_204", "/gen_204", "/hotspot-detect.html",
                  "/library/test/success.html", "/connecttest.txt",
                  "/ncsi.txt", "/success.txt", "/canonical.html")

# How long a link code stays on the panel without being claimed. Plex's own
# PIN lifetime is ~15 minutes; expire slightly after so the panel never
# outlives a code that could still work.
PIN_PANEL_TTL = 16 * 60

log = logging.getLogger("plex-matrix")

# Secrets are never sent to the browser; this placeholder round-trips instead.
SENTINEL = "•••"
SECRET_PATHS = ("plex.token", "ha.token", "web.password")


def _get_path(d, path):
    for part in path.split("."):
        d = d[part]
    return d


def _set_path(d, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        d = d[part]
    d[parts[-1]] = value


def create_app(config, state, netmgr=None) -> Flask:
    app = Flask(__name__)
    # Sessions only carry the "authed" flag; a fresh key per process simply
    # re-asks for the password after a restart.
    app.secret_key = secrets.token_hex(32)

    def in_ap_mode() -> bool:
        return netmgr is not None and netmgr.in_ap_mode()

    # ── Optional settings password ────────────────────────────────────────
    @app.before_request
    def require_password():
        stored = config.get()["web"]["password"]
        if (not stored or in_ap_mode() or session.get("authed")
                or request.path == "/login"
                or request.path.startswith("/static/")):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "password required"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            given = hashlib.sha256(
                request.form.get("password", "").encode()).hexdigest()
            stored = config.get()["web"]["password"] or ""
            if secrets.compare_digest(given, stored):
                session["authed"] = True
                return redirect("/")
            error = "Wrong password"
        cfg = config.get()
        return render_template("login.html", error=error,
                               device_name=cfg["device"]["name"],
                               version=nowplaying.__version__)

    @app.post("/api/password")
    def set_password():
        body = request.get_json(silent=True) or {}
        pw = str(body.get("password") or "")
        config.update({"web.password":
                       hashlib.sha256(pw.encode()).hexdigest() if pw else None})
        session["authed"] = True
        return jsonify({"ok": True, "enabled": bool(pw)})

    @app.before_request
    def captive_redirect():
        """In AP mode the wildcard DNS points every hostname here; bounce
        anything that isn't for the portal itself onto it."""
        if not in_ap_mode():
            return None
        host = (request.host or "").split(":")[0]
        if request.path in CAPTIVE_PROBES or host not in (AP_IP, "nowplaying.local"):
            return redirect(f"http://{AP_IP}/", code=302)
        return None

    @app.get("/")
    def index():
        cfg = config.get()
        template = "settings.html" if cfg["provisioned"] else "setup.html"
        return render_template(template,
                               device_name=cfg["device"]["name"],
                               version=nowplaying.__version__)

    # ── WiFi (provisioning) ───────────────────────────────────────────────
    @app.get("/api/wifi/scan")
    def wifi_scan():
        if netmgr is None:
            return jsonify({"networks": [], "error": "network management off"})
        # Live scans usually fail while the AP is up — serve the cache then.
        nets = netmgr.scan_cache if in_ap_mode() else netmgr.wifi_scan()
        return jsonify({"networks": nets})

    @app.post("/api/wifi/join")
    def wifi_join():
        if netmgr is None or netmgr.status == "passive":
            return jsonify({"error": "network management off"}), 400
        body = request.get_json(silent=True) or {}
        ssid = str(body.get("ssid", "")).strip()
        if not ssid:
            return jsonify({"error": "ssid required"}), 400
        netmgr.request_join(ssid, str(body.get("psk", "")))
        return jsonify({"ok": True, "reconnect_to": "http://nowplaying.local/"})

    @app.get("/api/status")
    def status():
        cfg = config.get()
        with state.lock:
            sessions = [{"title": s.title, "user": s.user, "state": s.state}
                        for s in state.sessions]
            dim = state.dim
        return jsonify({
            "device": cfg["device"]["name"],
            "version": nowplaying.__version__,
            "provisioned": cfg["provisioned"],
            "plex_url": cfg["plex"]["url"],
            "sessions": sessions,
            "dim": dim,
            "network": {
                "status": netmgr.status if netmgr else "passive",
                "error": netmgr.last_error if netmgr else "",
                "ip": netmgr.ip_address() if netmgr and not netmgr.in_ap_mode() else "",
            },
        })

    @app.get("/api/settings")
    def get_settings():
        cfg = config.get()
        for path in SECRET_PATHS:
            if _get_path(cfg, path):
                _set_path(cfg, path, SENTINEL)
        return jsonify(cfg)

    @app.post("/api/settings")
    def post_settings():
        patch = request.get_json(silent=True)
        if not isinstance(patch, dict):
            return jsonify({"error": "expected a JSON object of {path: value}"}), 400
        # Sentinel means "unchanged secret" — never write it through.
        patch = {k: v for k, v in patch.items() if v != SENTINEL}
        try:
            changed = config.update(patch)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "changed": sorted(changed),
            "restart_required": sorted(changed & RESTART_REQUIRED),
        })

    # ── Plex server discovery / auth ──────────────────────────────────────
    # One link flow at a time; the claimed token lives here only until the
    # user picks a server, then it is saved to config and cleared.
    auth = {"lock": threading.Lock(), "pin_id": None, "candidate": None, "token": ""}

    def _end_link_flow():
        """Drop any pending PIN and take the code off the panel.

        Every exit from the link flow routes through here — claimed, expired,
        cancelled, or abandoned because the user configured the server some
        other way. Missing one of those paths is what used to strand the code
        on the display until a restart.
        """
        with auth["lock"]:
            auth["pin_id"] = None
            auth["candidate"] = None
        state.clear_mode(DisplayMode.LINK_CODE)

    def _save_server(probe: dict, token: str, name: str = ""):
        config.update({
            "plex.url": probe["url"],
            "plex.token": token,
            "plex.machine_id": probe.get("machine_id", ""),
            "plex.server_name": name,
        })
        _end_link_flow()

    @app.post("/api/plex/discover")
    def plex_discover():
        servers = gdm_discover()
        for s in servers:
            probe = probe_server(s["url"])
            s.update(ok=probe.get("ok", False),
                     url=probe.get("url", s["url"]),
                     auth_required=probe.get("auth_required", False))
        return jsonify({"servers": servers})

    @app.post("/api/plex/probe")
    def plex_probe():
        body = request.get_json(silent=True) or {}
        url = str(body.get("url", "")).strip()
        if not url:
            return jsonify({"error": "url required"}), 400
        return jsonify(probe_server(url))

    @app.post("/api/plex/auth/start")
    def plex_auth_start():
        body = request.get_json(silent=True) or {}
        client_id = config.get()["device"]["client_id"]
        try:
            pin = plex_auth.pin_create(client_id)
        except Exception as e:
            log.warning("PIN create failed: %s", e)
            return jsonify({"error": f"could not reach plex.tv: {e}"}), 502
        with auth["lock"]:
            auth["pin_id"] = pin["id"]
            auth["candidate"] = body.get("server") or None  # {url, name, machine_id}
            auth["token"] = ""
        # Panel expiry backstops the browser: if the user simply walks away,
        # the code clears itself rather than sitting there indefinitely.
        state.set_mode(DisplayMode.LINK_CODE, code=pin["code"],
                       expires_at=time.monotonic() + PIN_PANEL_TTL)
        return jsonify({"code": pin["code"]})

    @app.post("/api/plex/auth/cancel")
    def plex_auth_cancel():
        _end_link_flow()
        return jsonify({"ok": True})

    @app.post("/api/plex/auth/poll")
    def plex_auth_poll():
        with auth["lock"]:
            pin_id = auth["pin_id"]
            candidate = auth["candidate"]
        if pin_id is None:
            return jsonify({"error": "no link flow in progress"}), 400
        client_id = config.get()["device"]["client_id"]
        try:
            res = plex_auth.pin_poll(client_id, pin_id)
        except Exception as e:
            return jsonify({"error": f"could not reach plex.tv: {e}"}), 502
        if res["expired"]:
            _end_link_flow()
            return jsonify({"claimed": False, "expired": True})
        token = res["token"]
        if not token:
            return jsonify({"claimed": False})

        _end_link_flow()
        with auth["lock"]:
            auth["token"] = token

        if candidate:
            probe = probe_server(candidate["url"], token)
            if probe.get("ok") and (not probe.get("auth_required") or probe.get("token_ok")):
                _save_server(probe, token, candidate.get("name", ""))
                with auth["lock"]:
                    auth["token"] = ""
                return jsonify({"claimed": True, "saved": True,
                                "server": candidate.get("name") or probe["url"]})
            return jsonify({"claimed": True, "saved": False,
                            "error": "signed in, but the token does not work on "
                                     f"{candidate['url']}"}), 502
        # No server chosen yet — offer the account's servers.
        try:
            servers = plex_auth.account_servers(client_id, token)
        except Exception as e:
            servers = []
            log.warning("resources lookup failed: %s", e)
        return jsonify({"claimed": True, "saved": False, "servers": servers})

    @app.post("/api/plex/select")
    def plex_select():
        body = request.get_json(silent=True) or {}
        urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
        if not urls:
            return jsonify({"error": "url required"}), 400
        with auth["lock"]:
            token = auth["token"] or config.get()["plex"]["token"]
        last = None
        for url in urls[:6]:
            probe = probe_server(url, token)
            last = probe
            if probe.get("ok") and (not probe.get("auth_required") or probe.get("token_ok")):
                # Token is kept even for open servers — it is the fallback if
                # the server's no-auth network allowance ever changes.
                _save_server(probe, token, body.get("name", ""))
                with auth["lock"]:
                    auth["token"] = ""
                return jsonify({"saved": True, "url": probe["url"]})
        err = (last or {}).get("error", "server did not respond")
        if last and last.get("ok") and last.get("auth_required"):
            err = "this server requires Plex sign-in"
        return jsonify({"saved": False, "error": err}), 502

    @app.post("/api/restart")
    def restart():
        # systemd-run detaches the restart from this process, so the HTTP
        # response gets out before the service goes down.
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "restart", "plex-matrix.service"])
        return jsonify({"ok": True})

    return app


def start_web(config, state, netmgr=None):
    """Start the web server thread. Returns the server (call .shutdown())."""
    from werkzeug.serving import make_server

    app = create_app(config, state, netmgr)
    port = config.get()["web"]["port"]
    server = make_server("0.0.0.0", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Web UI listening on port %d", port)
    return server

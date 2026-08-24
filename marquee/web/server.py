"""Web UI: post-setup settings page (and, in setup mode, the wizard/portal).

Runs as a daemon thread inside the display process via werkzeug's make_server
(no reloader, clean shutdown). A failure here must never take down rendering —
app.py wraps startup, and all handlers only touch the Config/State objects.
"""

import hashlib
import logging
import os
import secrets
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request, render_template, session

from flask import redirect

import marquee
from marquee import update
from marquee.config import RESTART_REQUIRED
from marquee.display.state import DisplayMode
from marquee.factoryreset import SSHD_DROPIN
from marquee.netmgr import AP_IP, local_ip, wifi_mac
from marquee.plex import auth as plex_auth
from marquee.plex.discovery import gdm_discover, probe_server

# Paths OSes fetch to detect a captive portal; answering with a redirect to
# the portal is what pops the sign-in sheet.
CAPTIVE_PROBES = ("/generate_204", "/gen_204", "/hotspot-detect.html",
                  "/library/test/success.html", "/connecttest.txt",
                  "/ncsi.txt", "/success.txt", "/canonical.html")

# How long a link code stays on the panel without being claimed. Plex's own
# PIN lifetime is ~15 minutes; expire slightly after so the panel never
# outlives a code that could still work.
PIN_PANEL_TTL = 16 * 60

log = logging.getLogger(__name__)

# The package is on the path rather than installed in the venv, so a detached
# `python -m marquee.factoryreset` needs to start from the directory that
# contains it (mirrors the WorkingDirectory= in the unit file).
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

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


def _ssh_active() -> bool:
    out = subprocess.run(["systemctl", "is-active", "ssh"],
                         capture_output=True, text=True)
    return out.stdout.strip() == "active"


def create_app(config, state, netmgr=None, updater=None) -> Flask:
    app = Flask(__name__)
    # Sessions only carry the "authed" flag; a fresh key per process simply
    # re-asks for the password after a restart.
    app.secret_key = secrets.token_hex(32)
    # Update bundles arrive as one multipart upload.
    app.config["MAX_CONTENT_LENGTH"] = update.MAX_BUNDLE_BYTES

    def in_ap_mode() -> bool:
        return netmgr is not None and netmgr.in_ap_mode()

    @app.context_processor
    def inject_theme():
        """Render the theme into <html> so the first paint is already right.

        Setting it from JS after load would flash the wrong palette, which is
        exactly what someone picking dark mode is trying to avoid.
        """
        return {"theme": config.get()["web"]["theme"]}

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
                               version=marquee.__version__)

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
        if request.path in CAPTIVE_PROBES or host not in (AP_IP, "marquee.local"):
            return redirect(f"http://{AP_IP}/", code=302)
        return None

    @app.get("/")
    def index():
        cfg = config.get()
        template = "settings.html" if cfg["provisioned"] else "setup.html"
        return render_template(template,
                               device_name=cfg["device"]["name"],
                               version=marquee.__version__)

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
        return jsonify({"ok": True, "reconnect_to": "http://marquee.local/"})

    @app.get("/api/status")
    def status():
        cfg = config.get()
        with state.lock:
            # `player` and `type` are here so the Filters page can show what
            # the rules would actually be matching against — copying a name
            # off this list beats guessing at Plex's spelling of it.
            sessions = [{"title": s.title, "user": s.user, "state": s.state,
                         "player": s.player, "type": s.media_type}
                        for s in state.sessions]
            dim = state.dim
            plex_offline = state.plex_offline
        ip = ssid = ""
        if not in_ap_mode():
            # local_ip() covers unmanaged devices (network.manage=false),
            # where netmgr never learns an address.
            ip = (netmgr.ip_address() if netmgr else "") or local_ip()
            ssid = netmgr.connected_ssid() if netmgr else ""
        return jsonify({
            "device": cfg["device"]["name"],
            "version": marquee.__version__,
            "provisioned": cfg["provisioned"],
            # Set by apt when a security update (kernel, libc) wants a
            # reboot; the Device page offers the restart, never forces it.
            "reboot_required": os.path.exists("/var/run/reboot-required"),
            "ssh_active": _ssh_active(),
            "plex_url": cfg["plex"]["url"],
            "sessions": sessions,
            "dim": dim,
            "plex_offline": plex_offline,
            "network": {
                "status": netmgr.status if netmgr else "passive",
                "error": netmgr.last_error if netmgr else "",
                "ip": ip,
                "ssid": ssid,
                "mac": netmgr.mac_address() if netmgr else wifi_mac(),
                "ipv4_method": cfg["network"]["ipv4_method"],
                "ipv4_error": netmgr.ipv4_error if netmgr else "",
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
        # A fresh addressing attempt supersedes whatever the last one said.
        if netmgr is not None and any(k.startswith("network.ipv4") for k in patch):
            netmgr.clear_ipv4_error()
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

    @app.post("/api/network/clear-error")
    def clear_network_error():
        if netmgr is not None:
            netmgr.clear_ipv4_error()
        return jsonify({"ok": True})

    @app.post("/api/factory-reset")
    def factory_reset():
        """Wipe the device back to out-of-box state.

        The reply has to reach the browser before anything is deleted: a full
        reset drops the WiFi profile, and on this hardware that is the only
        link. So the wipe runs on a short timer in a detached process, exactly
        like the SSH path, and this handler only schedules it.
        """
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return jsonify({"error": "confirmation required"}), 400
        keep_wifi = bool(body.get("keep_wifi"))

        log.warning("FACTORY RESET requested from the web UI (keep_wifi=%s)",
                    keep_wifi)
        state.set_mode(DisplayMode.INFO,
                       lines=["Factory reset", "", "restarting…"])
        args = [sys.executable, "-m", "marquee.factoryreset", "-y"]
        if keep_wifi:
            args.append("--keep-wifi")
        subprocess.Popen(
            ["systemd-run", "--collect", "--on-active=2",
             f"--unit=np-web-reset-{int(time.time())}",
             "-p", f"WorkingDirectory={PACKAGE_ROOT}"] + args)
        return jsonify({
            "ok": True,
            "keep_wifi": keep_wifi,
            "reconnect_to": "http://marquee.local/" if keep_wifi else "",
        })

    # ── Software updates ──────────────────────────────────────────────────
    # Both delivery paths (GitHub download, file upload) land the same signed
    # bundle at update.PENDING; /api/update/apply is the single install path
    # after that. The signature check in update.verify_bundle is the real
    # gate — an upload endpoint that becomes root-run code must never trust
    # the transport it arrived on.
    def _updater_or_503():
        if updater is None:
            return jsonify({"error": "updates are not available"}), 503
        return None

    @app.get("/api/update/status")
    def update_status():
        return _updater_or_503() or jsonify(updater.status())

    @app.post("/api/update/check")
    def update_check():
        return _updater_or_503() or jsonify(updater.check_now())

    @app.post("/api/update/download")
    def update_download():
        err = _updater_or_503()
        if err:
            return err
        try:
            manifest = updater.download_pending()
        except update.UpdateError as e:
            return jsonify({"error": str(e)}), 502
        except ImportError:
            return jsonify({"error": "update support is missing the "
                                     "cryptography package"}), 500
        return jsonify({"ok": True, "version": manifest["version"]})

    @app.post("/api/update/upload")
    def update_upload():
        f = request.files.get("file")
        if f is None:
            return jsonify({"error": "no file in the upload"}), 400
        os.makedirs(update.STATE_DIR, exist_ok=True)
        tmp = update.PENDING + ".part"
        f.save(tmp)
        try:
            manifest = update.verify_bundle(tmp)
        except update.UpdateError as e:
            os.unlink(tmp)
            return jsonify({"error": str(e)}), 400
        except ImportError:
            os.unlink(tmp)
            return jsonify({"error": "update support is missing the "
                                     "cryptography package"}), 500
        os.replace(tmp, update.PENDING)
        version = manifest["version"]
        return jsonify({
            "ok": True,
            "version": version,
            "current": marquee.__version__,
            "newer": update.is_newer(version, marquee.__version__),
        })

    @app.post("/api/update/apply")
    def update_apply():
        """Install the verified pending bundle. Detached, like restart and
        factory reset: the applier restarts this very service."""
        body = request.get_json(silent=True) or {}
        if body.get("confirm") is not True:
            return jsonify({"error": "confirmation required"}), 400
        try:
            manifest = update.verify_bundle(update.PENDING)
        except (update.UpdateError, ImportError, OSError) as e:
            return jsonify({"error": f"no installable update is waiting ({e})"}), 400
        version = manifest["version"]
        if not update.is_newer(version, marquee.__version__):
            return jsonify({"error": f"{version} is not newer than the "
                                     f"installed {marquee.__version__}"}), 400
        log.info("Installing update %s from the web UI", version)
        update.write_result("installing", marquee.__version__, version)
        update.schedule_apply()
        return jsonify({"ok": True, "version": version})

    @app.post("/api/ssh")
    def set_ssh():
        """Toggle the sshd service. Turning it on requires choosing a root
        password first — shipping a fleet that shares any default credential
        is the one classic IoT mistake this page must make impossible."""
        body = request.get_json(silent=True) or {}
        enable = bool(body.get("enabled"))
        try:
            if enable:
                pw = str(body.get("password") or "")
                if len(pw) < 8:
                    return jsonify({"error": "pick a password of at least 8 "
                                             "characters"}), 400
                # Via systemd-run: the service's own CapabilityBoundingSet
                # is too tight for chpasswd's shadow-file replacement. The
                # password goes through stdin, never argv.
                res = subprocess.run(
                    ["systemd-run", "--quiet", "--wait", "--pipe",
                     "--collect", "chpasswd"],
                    input=f"root:{pw}", text=True, capture_output=True,
                    timeout=30)
                if res.returncode != 0:
                    raise subprocess.CalledProcessError(
                        res.returncode, "chpasswd", stderr=res.stderr)
                os.makedirs(os.path.dirname(SSHD_DROPIN), exist_ok=True)
                with open(SSHD_DROPIN, "w") as f:
                    f.write("# Written by the Marquee settings page "
                            "(SSH toggle).\nPermitRootLogin yes\n")
                subprocess.run(["systemctl", "enable", "--now", "ssh"],
                               check=True, timeout=30)
                log.warning("SSH enabled from the web UI")
            else:
                subprocess.run(["systemctl", "disable", "--now", "ssh"],
                               check=True, timeout=30)
                log.warning("SSH disabled from the web UI")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as e:
            return jsonify({"error": f"could not change SSH state: {e}"}), 500
        return jsonify({"ok": True, "enabled": enable})

    @app.post("/api/reboot")
    def reboot():
        """Full OS reboot — what a kernel security update needs; the app
        restart below is not enough for that."""
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "reboot"])
        return jsonify({"ok": True})

    @app.post("/api/restart")
    def restart():
        # systemd-run detaches the restart from this process, so the HTTP
        # response gets out before the service goes down.
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "restart", "marquee.service"])
        return jsonify({"ok": True})

    return app


def start_web(config, state, netmgr=None, updater=None):
    """Start the web server thread. Returns the server (call .shutdown())."""
    from werkzeug.serving import make_server

    app = create_app(config, state, netmgr, updater)
    port = config.get()["web"]["port"]
    server = make_server("0.0.0.0", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Web UI listening on port %d", port)
    return server

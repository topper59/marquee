"""Web UI: post-setup settings page (and, in setup mode, the wizard/portal).

Runs as a daemon thread inside the display process via werkzeug's make_server
(no reloader, clean shutdown). A failure here must never take down rendering —
app.py wraps startup, and all handlers only touch the Config/State objects.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time

from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, render_template, session

from flask import redirect

import marquee
import marquee.config
from marquee import update
from marquee.config import RESTART_REQUIRED
from marquee.display.state import DisplayMode
from marquee.factoryreset import SSHD_DROPIN
from marquee.netmgr import AP_IP, local_ip, wifi_mac
from marquee.plex import auth as plex_auth
from marquee.plex.discovery import gdm_discover, probe_server


def render_schedule(disp: dict) -> bool:
    """Is the clock's display window open right now?

    Imported lazily inside the call because render.py pulls in rgbmatrix, and
    the web module has to stay importable on a machine without the panel.
    """
    from marquee.config import parse_hhmm
    from marquee.display.render import is_within_schedule
    return is_within_schedule(parse_hhmm(disp["schedule_start"]),
                              parse_hhmm(disp["schedule_stop"]))

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

# What the setup portal is allowed to reach while the AP is up.
#
# The AP is an OPEN network — it has to be, since nobody can be given a
# passphrase for a device they have not set up yet — and the password gate is
# necessarily off in that state. So anyone within radio range is, briefly, an
# anonymous caller with full reach. Everything the wizard genuinely needs is
# listed here; everything else (enabling SSH, factory reset, reboot, software
# updates, setting the settings password) waits until the device is on a
# network its owner controls.
AP_ALLOWED_EXACT = frozenset({
    "/", "/api/status", "/api/wifi/scan", "/api/wifi/join", "/api/settings",
    "/api/network/clear-error",
})
AP_ALLOWED_PREFIXES = ("/static/", "/api/plex/")

# Settings that must never be writable through the generic settings API.
# web.password is hashed by /api/password; writing it here would store the
# raw string and lock everyone out, since login compares against a digest.
SETTINGS_DENY = frozenset({"web.password"})

# Login throttling. Small enough not to annoy a fat-fingered owner, harsh
# enough that guessing over the LAN is not a realistic attack.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 60


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


class _Cached:
    """A value refreshed at most every `ttl` seconds.

    /api/status is polled every 10s by every open settings page, and each call
    used to fork three helpers (systemctl, and nmcli twice) on a Pi whose
    spare CPU is the matrix refresh's. None of these answers change between
    two polls, and a stale one for a few seconds costs nothing.
    """

    def __init__(self, fn, ttl: float):
        self.fn, self.ttl = fn, ttl
        self.at, self.value = 0.0, None
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            now = time.monotonic()
            if now - self.at >= self.ttl or self.value is None:
                try:
                    self.value = self.fn()
                except Exception:
                    log.debug("cached probe failed", exc_info=True)
                    self.value = self.value if self.value is not None else ""
                self.at = now
            return self.value

    def invalidate(self):
        with self.lock:
            self.at = 0.0


def create_app(config, state, netmgr=None, updater=None) -> Flask:
    app = Flask(__name__)
    # Sessions only carry the "authed" flag; a fresh key per process simply
    # re-asks for the password after a restart.
    app.secret_key = secrets.token_hex(32)
    # Update bundles arrive as one multipart upload.
    app.config["MAX_CONTENT_LENGTH"] = update.MAX_BUNDLE_BYTES

    def in_ap_mode() -> bool:
        return netmgr is not None and netmgr.in_ap_mode()

    # Shelled-out facts, refreshed no faster than the page can use them.
    ssh_active = _Cached(_ssh_active, 15)
    net_ip = _Cached(lambda: (netmgr.ip_address() if netmgr else "") or local_ip(), 15)
    net_ssid = _Cached(lambda: netmgr.connected_ssid() if netmgr else "", 30)

    @app.context_processor
    def inject_theme():
        """Render the theme into <html> so the first paint is already right.

        Setting it from JS after load would flash the wrong palette, which is
        exactly what someone picking dark mode is trying to avoid.
        """
        return {"theme": config.get()["web"]["theme"]}

    # ── Cross-site request blocking ───────────────────────────────────────
    @app.before_request
    def block_cross_site():
        """Refuse state-changing requests that a *different* site set off.

        Requiring a JSON body already stops most of this — a cross-site form
        cannot set application/json — but it is not a rule the whole API can
        follow: the update upload is genuinely multipart, which a form can
        send. Browsers attach Origin to every cross-origin POST and
        Sec-Fetch-Site to every request, so checking those covers the lot.

        Absent headers are allowed through on purpose. curl, and the Home
        Assistant `rest_command` in the README, send neither — and a request
        with no browser behind it is not a request some other site tricked a
        browser into making.
        """
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify({"error": "cross-site request refused"}), 403
        origin = request.headers.get("Origin")
        if origin:
            netloc = urlsplit(origin).netloc
            if netloc and netloc != request.host:
                log.warning("Refused a cross-site %s to %s from %s",
                            request.method, request.path, origin)
                return jsonify({"error": "cross-site request refused"}), 403
        return None

    # ── Setup-AP exposure ─────────────────────────────────────────────────
    @app.before_request
    def restrict_ap_mode():
        """While the open setup AP is up, serve only the wizard."""
        if not in_ap_mode():
            return None
        path = request.path
        if path in AP_ALLOWED_EXACT or path.startswith(AP_ALLOWED_PREFIXES):
            return None
        if path in CAPTIVE_PROBES:
            return None      # captive_redirect below answers these
        log.warning("Refused %s during setup: not reachable until the display "
                    "is on a real network", path)
        return jsonify({"error": "not available during setup"}), 403

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

    # Failed logins per client address: {ip: (count, locked_until)}. In memory
    # and per process, which is the right lifetime — a restart re-asks for the
    # password anyway, since the session key is regenerated with it.
    login_failures = {}
    login_lock = threading.Lock()

    def login_blocked(ip: str) -> float:
        """Seconds left on this address's lockout, 0 if it may try."""
        with login_lock:
            count, until = login_failures.get(ip, (0, 0.0))
            return max(0.0, until - time.monotonic())

    def note_login(ip: str, ok: bool):
        with login_lock:
            if ok:
                login_failures.pop(ip, None)
                return
            count, _ = login_failures.get(ip, (0, 0.0))
            count += 1
            until = (time.monotonic() + LOGIN_LOCKOUT_S
                     if count >= LOGIN_MAX_FAILURES else 0.0)
            if until:
                log.warning("Locking out %s after %d failed logins", ip, count)
                count = 0
            login_failures[ip] = (count, until)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            # Unsalted SHA-256 is fast by design, so nothing but a lockout
            # stops a script working through a short password at wire speed.
            ip = request.remote_addr or "?"
            wait = login_blocked(ip)
            if wait:
                error = f"Too many attempts — wait {int(wait) + 1}s."
            else:
                given = hashlib.sha256(
                    request.form.get("password", "").encode()).hexdigest()
                stored = config.get()["web"]["password"] or ""
                ok = bool(stored) and secrets.compare_digest(given, stored)
                note_login(ip, ok)
                if ok:
                    session["authed"] = True
                    return redirect("/")
                error = "Wrong password"
        cfg = config.get()
        return render_template("login.html", error=error,
                               device_name=cfg["device"]["name"],
                               version=marquee.__version__)

    @app.post("/api/password")
    def set_password():
        # A *missing* body used to mean "" here, i.e. "remove the password".
        # That turned an empty cross-site POST into a way to disable the
        # settings password, so absence is now an error and only an explicit
        # empty string clears it.
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "password" not in body:
            return jsonify({"error": "expected a JSON object with a "
                                     "'password' field"}), 400
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
            ha_blank = state.ha_blank
            plex_offline = state.plex_offline
        ip = ssid = ""
        if not in_ap_mode():
            # local_ip() covers unmanaged devices (network.manage=false),
            # where netmgr never learns an address.
            ip = net_ip()
            ssid = net_ssid()
        return jsonify({
            "device": cfg["device"]["name"],
            "version": marquee.__version__,
            "provisioned": cfg["provisioned"],
            # Set by apt when a security update (kernel, libc) wants a
            # reboot; the Device page offers the restart, never forces it.
            "reboot_required": os.path.exists("/var/run/reboot-required"),
            "ssh_active": ssh_active(),
            "plex_url": cfg["plex"]["url"],
            "sessions": sessions,
            "dim": dim,
            # A panel blanked by ha.tv_action looks exactly like a broken
            # one, and the settings page is where someone goes to find out
            # which it is — so say so, the same way plex_offline does.
            "ha_blank": ha_blank,
            "plex_offline": plex_offline,
            "panel": _panel_state(),
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
        denied = SETTINGS_DENY & patch.keys()
        if denied:
            return jsonify({"error": f"{', '.join(sorted(denied))} cannot be "
                                     "set here"}), 400
        try:
            changed = config.update(patch)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        # A fresh addressing attempt supersedes whatever the last one said.
        if netmgr is not None and any(k.startswith("network.ipv4") for k in patch):
            netmgr.clear_ipv4_error()
            net_ip.invalidate()
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

    # ── Panel override ────────────────────────────────────────────────────
    # Deliberately its own endpoint rather than a field in the Display
    # section: this is a thing you do, not a setting you save, and it has to
    # take effect the instant it is pressed. It is also the endpoint a Home
    # Assistant `rest_command` calls — see the README.
    @app.post("/api/panel")
    def set_panel():
        body = request.get_json(silent=True) or {}
        want = str(body.get("state", "")).strip().lower()
        if want not in ("on", "off", "auto"):
            return jsonify({"error": "state must be on, off or auto"}), 400
        # minutes=0 (or absent) means "until someone says otherwise".
        try:
            minutes = int(body.get("minutes") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "minutes must be a number"}), 400
        if not (0 <= minutes <= 60 * 24 * 30):
            return jsonify({"error": "minutes out of range"}), 400

        if want == "auto":
            patch = {"display.override": "none", "display.override_until": 0}
        else:
            patch = {"display.override": want,
                     "display.override_until":
                         int(time.time() + minutes * 60) if minutes else 0}
        try:
            config.update(patch)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        log.info("Panel override → %s%s", want,
                 f" for {minutes} min" if minutes and want != "auto" else "")
        return jsonify(_panel_state())

    def _panel_state() -> dict:
        """What the panel is doing and why — the answer both the settings page
        and a Home Assistant sensor want."""
        cfg = config.get()
        disp = cfg["display"]
        override = marquee.config.effective_override(
            disp["override"], disp["override_until"])
        with state.lock:
            ha_blank = state.ha_blank
        in_schedule = render_schedule(disp)
        on = override == "on" or (
            override != "off" and in_schedule and not ha_blank)
        return {
            "on": on,
            "override": override,
            # 0 when the override has no clock on it; the page turns this into
            # "off until 9:42 PM" rather than making anyone read a timestamp.
            "override_until": disp["override_until"] if override != "none" else 0,
            "in_schedule": in_schedule,
            "ha_blank": ha_blank,
        }

    @app.get("/api/panel")
    def get_panel():
        return jsonify(_panel_state())

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

    # ── Settings backup / restore ─────────────────────────────────────────
    # Deliberately absent from AP_ALLOWED_EXACT: the backup carries the Plex
    # and Home Assistant tokens, and the setup AP is an open network.
    @app.get("/api/backup")
    def download_backup():
        doc = config.export_backup()
        slug = re.sub(r"[^A-Za-z0-9]+", "-",
                      doc["device_name"]).strip("-").lower() or "marquee"
        name = f"{slug}-settings-{time.strftime('%Y%m%d')}.json"
        return Response(
            json.dumps(doc, indent=2) + "\n",
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/restore")
    def upload_restore():
        """Apply a backup file. Multipart from the settings page, or a plain
        JSON body so the same file can be pushed with curl."""
        f = request.files.get("file")
        if f is not None:
            try:
                doc = json.loads(f.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return jsonify({"error": "that file is not a settings backup"}), 400
        else:
            doc = request.get_json(silent=True)
            if doc is None:
                return jsonify({"error": "no backup file in the upload"}), 400
        try:
            changed, skipped = config.import_backup(doc)
        except (ValueError, TypeError) as e:
            return jsonify({"error": str(e)}), 400
        log.info("Settings restored from a backup: %d changed, %d skipped",
                 len(changed), len(skipped))
        return jsonify({
            "ok": True,
            "changed": sorted(changed),
            "skipped": skipped,
            "restart_required": sorted(changed & RESTART_REQUIRED),
            "from_version": str(doc.get("app_version") or ""),
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
        # Absence used to read as enabled=False, so an empty cross-site POST
        # switched SSH off. The caller has to say which way it wants.
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "enabled" not in body:
            return jsonify({"error": "expected a JSON object with an "
                                     "'enabled' field"}), 400
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
            ssh_active.invalidate()
            return jsonify({"error": f"could not change SSH state: {e}"}), 500
        ssh_active.invalidate()
        return jsonify({"ok": True, "enabled": enable})

    def _confirmed() -> bool:
        """Did this request actually come from the settings page?

        A cross-origin HTML form can POST here without any script running, but
        it cannot send a JSON body — so requiring the same {"confirm": true}
        the factory-reset endpoint asks for is what stops a page in another
        tab from power-cycling the display. It is not a substitute for the
        optional password; it is the floor underneath a device with none set.
        """
        return (request.get_json(silent=True) or {}).get("confirm") is True

    @app.post("/api/reboot")
    def reboot():
        """Full OS reboot — what a kernel security update needs; the app
        restart below is not enough for that."""
        if not _confirmed():
            return jsonify({"error": "confirmation required"}), 400
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "reboot"])
        return jsonify({"ok": True})

    @app.post("/api/restart")
    def restart():
        # systemd-run detaches the restart from this process, so the HTTP
        # response gets out before the service goes down.
        if not _confirmed():
            return jsonify({"error": "confirmation required"}), 400
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

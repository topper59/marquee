"""Web UI: post-setup settings page (and, in setup mode, the wizard/portal).

Runs as a daemon thread inside the display process via werkzeug's make_server
(no reloader, clean shutdown). A failure here must never take down rendering —
app.py wraps startup, and all handlers only touch the Config/State objects.
"""

import logging
import subprocess
import threading

from flask import Flask, jsonify, request, render_template

import nowplaying
from nowplaying.config import RESTART_REQUIRED

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


def create_app(config, state) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        cfg = config.get()
        return render_template("settings.html",
                               device_name=cfg["device"]["name"],
                               version=nowplaying.__version__)

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

    @app.post("/api/restart")
    def restart():
        # systemd-run detaches the restart from this process, so the HTTP
        # response gets out before the service goes down.
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "restart", "plex-matrix.service"])
        return jsonify({"ok": True})

    return app


def start_web(config, state):
    """Start the web server thread. Returns the server (call .shutdown())."""
    from werkzeug.serving import make_server

    app = create_app(config, state)
    port = config.get()["web"]["port"]
    server = make_server("0.0.0.0", port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Web UI listening on port %d", port)
    return server

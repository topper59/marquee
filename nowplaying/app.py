"""Process entry point: wires config, state, and the thread loops together.

Layout (mirrors the Matrix Portal S3 build):
  - Left:  64x64 poster art (center-cropped square)
  - Right: 64x64 area for title (scrolling if needed) + subtitle + user + progress bar
  - Idle:  centered clock + "Nothing playing"
  - Multi-session: cycles between active sessions every display.cycle_seconds

Data source: Plex Media Server /status/sessions
Image source: Plex /photo/:/transcode (server-side resize, cropped locally)
"""

import signal
import logging
import threading

from nowplaying.config import Config

log = logging.getLogger("plex-matrix")


def main():
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    config = Config()
    cfg = config.get()
    logging.getLogger().setLevel(cfg["log_level"])
    log.info("Starting nowplaying (provisioned=%s, schedule %s→%s)",
             cfg["provisioned"],
             cfg["display"]["schedule_start"], cfg["display"]["schedule_stop"])

    # Imported here so config/logging are settled first, and so logic tests can
    # stub out rgbmatrix before anything touches it.
    from nowplaying.display.state import State
    from nowplaying.display.matrix import build_matrix
    from nowplaying.display.render import render_loop
    from nowplaying.plex.client import fetcher_loop
    from nowplaying.ha import ha_poller_loop

    matrix = build_matrix(cfg["matrix"], cfg["display"]["brightness_normal"])
    state  = State()
    stop   = threading.Event()

    from nowplaying.netmgr import NetManager
    netmgr = NetManager(config, state, stop)
    netmgr.start()   # exits immediately unless network.manage is enabled

    from nowplaying.resetbtn import ResetButton
    ResetButton(config, state, stop, netmgr).start()

    web_server = None
    if cfg["web"]["enabled"]:
        try:
            from nowplaying.web.server import start_web
            web_server = start_web(config, state, netmgr)
        except Exception:
            # The panel must keep working even if the web UI cannot start
            # (port taken, missing dependency, …).
            log.exception("Web UI failed to start — continuing without it")

    def handle_signal(signum, frame):
        log.info("Signal %d received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    fetcher   = threading.Thread(target=fetcher_loop,   args=(config, state, stop), daemon=True)
    ha_poller = threading.Thread(target=ha_poller_loop, args=(config, state, stop), daemon=True)
    fetcher.start()
    ha_poller.start()

    try:
        render_loop(matrix, config, state, stop)
    finally:
        stop.set()
        if web_server is not None:
            web_server.shutdown()
        fetcher.join(timeout=2)
        ha_poller.join(timeout=2)
        matrix.Clear()
        log.info("Stopped")

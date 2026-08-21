"""Process entry point: wires config, state, and the three loops together.

Layout (mirrors the Matrix Portal S3 build):
  - Left:  64x64 poster art (center-cropped square)
  - Right: 64x64 area for title (scrolling if needed) + subtitle + user + progress bar
  - Idle:  centered clock + "Nothing playing"
  - Multi-session: cycles between active sessions every CYCLE_SECONDS

Data source: Plex Media Server /status/sessions
Image source: Plex /photo/:/transcode (server-side resize, cropped locally)
"""

import signal
import logging
import threading

from nowplaying import config
from nowplaying.display.state import State
from nowplaying.display.matrix import build_matrix
from nowplaying.display.render import render_loop
from nowplaying.plex.client import Plex, fetcher_loop
from nowplaying.ha import HomeAssistant, ha_poller_loop

log = logging.getLogger("plex-matrix")


def main():
    log.info("Starting plex-matrix (schedule: %s, %s→%s)",
             "enabled" if config.SCHEDULE_ENABLE else "disabled",
             config.SCHEDULE_START.strftime("%H:%M"),
             config.SCHEDULE_STOP.strftime("%H:%M"))

    matrix   = build_matrix()
    state    = State()
    plex     = Plex(config.PLEX_URL, config.PLEX_TOKEN, config.PLEX_VERIFY_SSL)
    ha       = HomeAssistant(config.HA_URL, config.HA_TOKEN)
    stop     = threading.Event()

    def handle_signal(signum, frame):
        log.info("Signal %d received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    fetcher   = threading.Thread(target=fetcher_loop,   args=(plex, state, stop),     daemon=True)
    ha_poller = threading.Thread(target=ha_poller_loop, args=(ha, state, stop),       daemon=True)
    fetcher.start()
    ha_poller.start()

    try:
        render_loop(matrix, state, stop)
    finally:
        stop.set()
        fetcher.join(timeout=2)
        ha_poller.join(timeout=2)
        matrix.Clear()
        log.info("Stopped")

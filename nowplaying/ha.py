"""Home Assistant client and the dim-state poller thread.

Optional integration: when disabled or unconfigured the poller idles and the
panel simply never dims. The client is (re)built whenever the HA section of
the config changes, so enabling it from the web UI needs no restart.
"""

import logging
import threading
from typing import Optional

import requests

from nowplaying.display.state import State

log = logging.getLogger("plex-matrix")


class HomeAssistant:
    def __init__(self, base_url: str, token: str, timeout: float = 4):
        self.base_url = base_url
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def get_state(self, entity_id: str) -> Optional[str]:
        try:
            r = self.s.get(
                f"{self.base_url}/api/states/{entity_id}",
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json().get("state")
        except Exception as e:
            log.warning("HA state fetch failed for %s: %s", entity_id, e)
            return None

    def should_dim(self, tv_entity: str, require_sunset: bool = True) -> bool:
        """True when the panel should drop to the dimmed brightness.

        The TV being on is always required; sunset is an optional second
        condition so a room that is dark all day can dim regardless.
        """
        tv_state = self.get_state(tv_entity)
        tv_on = tv_state not in (None, "off", "unavailable", "unknown", "standby")
        if not require_sunset:
            log.debug("HA dim check — tv=%s (%s), sunset not required",
                      tv_entity, tv_state)
            return tv_on
        # Skip the second request when the answer cannot change.
        if not tv_on:
            log.debug("HA dim check — tv=%s (%s) off", tv_entity, tv_state)
            return False
        sun_state = self.get_state("sun.sun")
        log.debug("HA dim check — tv=%s (%s) sun.sun=%s", tv_entity, tv_state, sun_state)
        return sun_state == "below_horizon"


def ha_poller_loop(config, state: State, stop: threading.Event):
    client = None
    client_sig = None
    while not stop.is_set():
        hc = config.get()["ha"]
        configured = hc["enabled"] and hc["url"] and hc["token"] and hc["tv_entity"]
        if not configured:
            if client is not None:
                client = client_sig = None
                log.info("HA integration disabled — dim off")
            with state.lock:
                state.dim = False
        else:
            sig = (hc["url"], hc["token"])
            if sig != client_sig:
                client = HomeAssistant(hc["url"], hc["token"])
                client_sig = sig
                log.info("HA integration active (%s)", hc["url"])
            try:
                dim = client.should_dim(hc["tv_entity"], hc["require_sunset"])
                with state.lock:
                    state.dim = dim
                log.debug("Dim state updated: %s", dim)
            except Exception as e:
                log.warning("HA poll cycle failed: %s", e)
        stop.wait(hc["poll_seconds"])

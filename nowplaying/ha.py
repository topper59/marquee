"""Home Assistant client and the dim-state poller thread."""

import logging
import threading
from typing import Optional

import requests

from nowplaying import config
from nowplaying.display.state import State

log = logging.getLogger("plex-matrix")


class HomeAssistant:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def get_state(self, entity_id: str) -> Optional[str]:
        try:
            r = self.s.get(
                f"{self.base_url}/api/states/{entity_id}",
                timeout=config.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("state")
        except Exception as e:
            log.warning("HA state fetch failed for %s: %s", entity_id, e)
            return None

    def should_dim(self, tv_entity: str) -> bool:
        tv_state  = self.get_state(tv_entity)
        sun_state = self.get_state("sun.sun")
        tv_on        = tv_state not in (None, "off", "unavailable", "unknown", "standby")
        after_sunset = sun_state == "below_horizon"
        log.debug("HA dim check — tv=%s (%s) sun=%s (%s)",
                  tv_entity, tv_state, "sun.sun", sun_state)
        return tv_on and after_sunset


def ha_poller_loop(ha: HomeAssistant, state: State, stop: threading.Event):
    while not stop.is_set():
        try:
            dim = ha.should_dim(config.HA_TV_ENTITY)
            with state.lock:
                state.dim = dim
            log.debug("Dim state updated: %s", dim)
        except Exception as e:
            log.warning("HA poll cycle failed: %s", e)
        stop.wait(config.HA_POLL_SECONDS)

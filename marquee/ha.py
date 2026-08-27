"""Home Assistant client and the TV-state poller thread.

Optional integration: when disabled or unconfigured the poller idles and the
panel simply never reacts to the TV. The client is (re)built whenever the HA
section of the config changes, so enabling it from the web UI needs no
restart. What the TV being on does to the panel is `ha.tv_action`: "dim"
drops it to the dim brightness, "off" blanks it outright, which is what a
theater room wants — a dim panel is still a light source in a dark room.
"""

import logging
import threading
from typing import Optional

import requests

from marquee.display.state import State

log = logging.getLogger(__name__)


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

    def tv_active(self, tv_entity: str, require_sunset: bool = True) -> bool:
        """True when the panel should react to the TV (dim or go off).

        The TV being on is always required; sunset is an optional second
        condition so a room that is dark all day can react regardless.
        """
        tv_state = self.get_state(tv_entity)
        tv_on = tv_state not in (None, "off", "unavailable", "unknown", "standby")
        if not require_sunset:
            log.debug("HA TV check — tv=%s (%s), sunset not required",
                      tv_entity, tv_state)
            return tv_on
        # Skip the second request when the answer cannot change.
        if not tv_on:
            log.debug("HA TV check — tv=%s (%s) off", tv_entity, tv_state)
            return False
        sun_state = self.get_state("sun.sun")
        log.debug("HA TV check — tv=%s (%s) sun.sun=%s", tv_entity, tv_state, sun_state)
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
                log.info("HA integration disabled — TV no longer affects the panel")
            with state.lock:
                state.dim = False
                state.ha_blank = False
        else:
            sig = (hc["url"], hc["token"])
            if sig != client_sig:
                client = HomeAssistant(hc["url"], hc["token"])
                client_sig = sig
                log.info("HA integration active (%s)", hc["url"])
            try:
                tv_on = client.tv_active(hc["tv_entity"], hc["require_sunset"])
                blank = tv_on and hc["tv_action"] == "off"
                with state.lock:
                    state.dim = tv_on and not blank
                    state.ha_blank = blank
                log.debug("HA state updated: tv=%s action=%s",
                          tv_on, hc["tv_action"])
            except Exception as e:
                log.warning("HA poll cycle failed: %s", e)
        stop.wait(hc["poll_seconds"])

"""Factory-reset / info button.

Hardware: momentary button between the Matrix Bonnet's #25 breakout pad
(GPIO25) and GND. Internal pull-up, active low. GPIO25 is free on this build —
GPIO24 is not (E address line on 1/32-scan panels). Reading it via gpiozero is
independent of rgbmatrix's register-level GPIO use.

Behavior:
  short press (<3s)  → info page on the panel for 10s (IP, hostname, version)
  hold 3–10s         → live "Reset in N — release to cancel" countdown
  hold 10s           → factory reset: config wiped, WiFi profiles deleted,
                       service restarts into the setup AP

If the GPIO stack is unavailable (dev stubs, unsupported board) the feature
logs once and disables itself.
"""

import logging
import threading
import time

import marquee
from marquee import factoryreset
from marquee.display.state import State, DisplayMode

log = logging.getLogger(__name__)

BUTTON_GPIO = 25
INFO_HOLD_MAX = 3.0    # released before this → info page
RESET_HOLD = 10.0      # held this long → factory reset
INFO_SHOW_S = 10.0


class ResetButton(threading.Thread):
    def __init__(self, config, state: State, stop: threading.Event, netmgr=None):
        super().__init__(daemon=True, name="resetbtn")
        self.config = config
        self.state = state
        self.stop_event = stop
        self.netmgr = netmgr

    def _show_info(self):
        ip = ""
        if self.netmgr is not None:
            ip = self.netmgr.ip_address()
        cfg = self.config.get()
        lines = [
            cfg["device"]["name"],
            ip or "no network",
            "marquee.local",
            f"v{marquee.__version__}",
        ]
        self.state.set_mode(DisplayMode.INFO, lines=lines)

    def _factory_reset(self):
        log.warning("FACTORY RESET triggered by button")
        self.state.set_mode(DisplayMode.INFO, lines=["Factory reset", "", "restarting…"])
        factoryreset.reset(self.config.path)

    def run(self):
        try:
            from gpiozero import Button
            button = Button(BUTTON_GPIO, pull_up=True, bounce_time=0.02)
        except Exception as e:
            log.info("Reset button unavailable (%s) — feature disabled", e)
            return
        log.info("Reset button watching GPIO%d", BUTTON_GPIO)

        pressed_at = None
        info_until = 0.0
        while not self.stop_event.wait(0.1):
            now = time.monotonic()
            if button.is_pressed:
                if pressed_at is None:
                    pressed_at = now
                held = now - pressed_at
                if held >= RESET_HOLD:
                    self._factory_reset()
                    return
                if held >= INFO_HOLD_MAX:
                    remaining = int(RESET_HOLD - held) + 1
                    self.state.set_mode(DisplayMode.RESETTING, seconds=remaining)
            else:
                if pressed_at is not None:
                    held = now - pressed_at
                    pressed_at = None
                    if held < INFO_HOLD_MAX:
                        self._show_info()
                        info_until = now + INFO_SHOW_S
                    else:
                        log.info("Factory reset canceled at %.1fs", held)
                        self.state.clear_mode(DisplayMode.RESETTING)
                elif info_until and now >= info_until:
                    info_until = 0.0
                    self.state.clear_mode(DisplayMode.INFO)

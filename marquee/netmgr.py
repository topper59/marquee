"""WiFi provisioning state machine on top of NetworkManager.

Inert unless config network.manage is true (migrated owner devices keep it
false until the flow is signed off). When active:

    boot ─► station profile saved? ── yes ─► connect (timeout) ─► ONLINE
                    │ no / timeout                                  │
                    ▼                                               │
                 AP MODE  ◄── join failed (bad password) ◄──────────┘ lost+no profile
              open AP + captive portal; panel shows SSID/URL
                    │ portal submits credentials
                    ▼
                 JOINING ── connected ─► ONLINE

Safety properties: the AP profile is autoconnect=no and the station profile
autoconnect=yes, so a power cycle always lands back in station mode; an
ONLINE device with a saved profile never falls back to AP on mere
connectivity loss (a router reboot must not strand the panel in setup mode).

All nmcli access goes through an injectable run_cmd so the transitions are
testable off-hardware.
"""

import logging
import subprocess
import threading
import time

from marquee.display.state import State, DisplayMode

log = logging.getLogger(__name__)

AP_CON = "marquee-ap"
STATION_CON = "marquee-wifi"
AP_IP = "10.42.0.1"
WIFI_DEV = "wlan0"

# Seconds between accepting portal credentials and tearing down the AP.
AP_HANDOFF_GRACE_S = 8


def default_run_cmd(args: list[str], timeout: float = 45):
    """Run a command, returning (rc, stdout). Never raises on failure rc."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            log.debug("cmd %s rc=%d: %s", " ".join(args), p.returncode,
                      (p.stderr or p.stdout).strip()[:200])
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("cmd %s failed: %s", " ".join(args), e)
        return 1, ""


def _unescape_nmcli(field: str) -> str:
    return field.replace("\\:", ":").replace("\\\\", "\\")


def wifi_mac() -> str:
    """wlan0's hardware address, upper-case ('' if the interface is absent)."""
    try:
        with open(f"/sys/class/net/{WIFI_DEV}/address") as f:
            return f.read().strip().upper()
    except OSError:
        return ""


def local_ip() -> str:
    """This host's LAN address, without shelling out to nmcli.

    Opening a UDP socket toward an off-link address sends no packets; it just
    makes the kernel pick the route it would use, which is the interface the
    user can reach the web UI on.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET-1, never routed anywhere
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


class NetManager(threading.Thread):
    def __init__(self, config, state: State, stop: threading.Event,
                 run_cmd=default_run_cmd):
        super().__init__(daemon=True, name="netmgr")
        self.config = config
        self.state = state
        self.stop_event = stop
        self.run_cmd = run_cmd      # injectable for tests
        self._lock = threading.Lock()
        self._join_request = None   # (ssid, psk) from the portal
        self.scan_cache: list[dict] = []
        self.status = "passive"     # passive|connecting|online|ap|joining
        self.last_error = ""
        self.ipv4_error = ""
        # Addressing is only pushed to NetworkManager when it *changes*; the
        # profile already stores it across reboots, so re-applying every boot
        # would drop the link for nothing.
        self._ipv4_sig = self._ipv4_signature(config.get()["network"])

    @staticmethod
    def _ipv4_signature(net: dict) -> tuple:
        return (net["ipv4_method"], net["ipv4_address"], net["ipv4_prefix"],
                net["ipv4_gateway"], net["ipv4_dns"])

    # ── nmcli helpers ─────────────────────────────────────────────────────
    def wifi_scan(self) -> list[dict]:
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                            "dev", "wifi", "list", "--rescan", "yes"])
        if rc != 0:
            return self.scan_cache
        seen = {}
        for line in out.splitlines():
            # SSID may contain escaped colons; SIGNAL and SECURITY cannot.
            parts = line.rsplit(":", 2)
            if len(parts) != 3:
                continue
            ssid = _unescape_nmcli(parts[0])
            if not ssid or ssid.startswith(self.config.get()["network"]["ap_ssid_prefix"]):
                continue
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            secured = parts[2] not in ("", "--")
            if ssid not in seen or seen[ssid]["signal"] < signal:
                seen[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}
        nets = sorted(seen.values(), key=lambda n: -n["signal"])
        if nets:
            self.scan_cache = nets
        return nets

    def _profiles(self) -> list[tuple[str, str]]:
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"])
        if rc != 0:
            return []
        rows = []
        for line in out.splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) == 2:
                rows.append((_unescape_nmcli(parts[0]), parts[1]))
        return rows

    def station_profile_names(self) -> list[str]:
        return [name for name, typ in self._profiles()
                if typ == "802-11-wireless" and name != AP_CON]

    def _wlan_state(self) -> str:
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "DEVICE,STATE", "dev", "status"])
        for line in out.splitlines():
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[0] == WIFI_DEV:
                return parts[1]
        return "unknown"

    def wlan_connected(self) -> bool:
        return self._wlan_state() == "connected"

    def ip_address(self) -> str:
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "IP4.ADDRESS", "dev", "show", WIFI_DEV])
        for line in out.splitlines():
            if line.startswith("IP4.ADDRESS"):
                return line.split(":", 1)[1].split("/")[0]
        return ""

    def connected_ssid(self) -> str:
        """SSID wlan0 is currently associated to ('' if none)."""
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev",
                                "wifi", "list", "--rescan", "no"])
        for line in out.splitlines():
            active, _, ssid = line.partition(":")
            if active == "yes" and ssid:
                return _unescape_nmcli(ssid)
        return ""

    def mac_address(self) -> str:
        return wifi_mac()

    def active_station_profile(self) -> str:
        """Profile currently up on wlan0, ignoring the setup AP.

        Prefers whatever nmcli says is active so a hand-provisioned device
        (network.manage=false, some other profile name) can still be given a
        static address; falls back to our own profile name.
        """
        rc, out = self.run_cmd(["nmcli", "-t", "-f", "GENERAL.CONNECTION",
                                "dev", "show", WIFI_DEV])
        for line in out.splitlines():
            if line.startswith("GENERAL.CONNECTION"):
                name = _unescape_nmcli(line.split(":", 1)[1]).strip()
                if name and name not in ("--", AP_CON):
                    return name
        names = self.station_profile_names()
        if STATION_CON in names:
            return STATION_CON
        return names[0] if names else ""

    def ap_ssid(self) -> str:
        prefix = self.config.get()["network"]["ap_ssid_prefix"]
        suffix = wifi_mac().replace(":", "")[-4:] or "0000"
        return f"{prefix}-{suffix}"

    def in_ap_mode(self) -> bool:
        return self.status == "ap"

    # ── AP / station transitions ──────────────────────────────────────────
    def _ap_up(self) -> bool:
        names = [n for n, _ in self._profiles()]
        if AP_CON not in names:
            ssid = self.ap_ssid()
            rc, _ = self.run_cmd(["nmcli", "con", "add", "type", "wifi",
                              "ifname", WIFI_DEV, "con-name", AP_CON,
                              "autoconnect", "no", "ssid", ssid,
                              "802-11-wireless.mode", "ap",
                              "802-11-wireless.band", "bg",
                              "ipv4.method", "shared",
                              "ipv4.addresses", f"{AP_IP}/24",
                              "ipv6.method", "disabled"])
            if rc != 0:
                log.error("Could not create AP profile")
                return False
        rc, _ = self.run_cmd(["nmcli", "con", "up", AP_CON])
        return rc == 0

    def _ap_down(self):
        self.run_cmd(["nmcli", "con", "down", AP_CON])

    def _join(self, ssid: str, psk: str) -> bool:
        self.run_cmd(["nmcli", "con", "delete", STATION_CON])
        cmd = ["nmcli", "con", "add", "type", "wifi", "con-name", STATION_CON,
               "ifname", WIFI_DEV, "ssid", ssid,
               "connection.autoconnect", "yes"]
        if psk:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        rc, _ = self.run_cmd(cmd)
        if rc != 0:
            return False
        timeout = self.config.get()["network"]["join_timeout_s"]
        rc, _ = self.run_cmd(["nmcli", "--wait", str(timeout), "con", "up", STATION_CON],
                         timeout=timeout + 15)
        if rc != 0 or not self._wait_connected(10):
            self.run_cmd(["nmcli", "con", "delete", STATION_CON])
            return False
        return True

    def _wait_connected(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.stop_event.is_set():
            if self.wlan_connected():
                return True
            self.stop_event.wait(2)
        return self.wlan_connected()

    # ── static addressing ─────────────────────────────────────────────────
    def _set_ipv4(self, profile: str, net: dict | None) -> bool:
        """Write addressing onto `profile`. net=None means back to DHCP."""
        if net is None or net["ipv4_method"] == "auto":
            args = ["ipv4.method", "auto", "ipv4.addresses", "",
                    "ipv4.gateway", "", "ipv4.dns", ""]
        else:
            args = ["ipv4.addresses", f"{net['ipv4_address']}/{net['ipv4_prefix']}",
                    "ipv4.gateway", net["ipv4_gateway"],
                    "ipv4.dns", net["ipv4_dns"],
                    # method last: nmcli validates manual mode against the
                    # address already set on the profile.
                    "ipv4.method", "manual"]
        rc, _ = self.run_cmd(["nmcli", "con", "mod", profile] + args)
        return rc == 0

    def apply_ipv4(self, net: dict) -> str:
        """Push addressing onto the station profile. Returns "" or an error.

        wlan0 is the only link on this hardware, so a static address that
        does not come up is a stranded device. Anything short of a confirmed
        connection rolls the profile back to DHCP and brings it up again.
        """
        manual = net["ipv4_method"] == "manual"
        if manual and not (net["ipv4_address"] and net["ipv4_gateway"]):
            return "a static address needs both an IP address and a gateway"
        profile = self.active_station_profile()
        if not profile:
            return "no saved WiFi profile to configure"

        log.info("Applying %s addressing to %s", net["ipv4_method"], profile)
        if not self._set_ipv4(profile, net):
            return "NetworkManager rejected those addressing settings"
        rc, _ = self.run_cmd(["nmcli", "--wait", "30", "con", "up", profile],
                             timeout=45)
        if rc == 0 and self._wait_connected(20):
            log.info("Addressing applied — now at %s", self.ip_address())
            return ""

        log.error("Static addressing did not come up — reverting %s to DHCP", profile)
        self._set_ipv4(profile, None)
        self.run_cmd(["nmcli", "--wait", "30", "con", "up", profile], timeout=45)
        return ("that address did not come up — the display reverted to "
                "automatic (DHCP)")

    def _maybe_apply_ipv4(self):
        """Apply addressing when, and only when, the config changed."""
        net = self.config.get()["network"]
        sig = self._ipv4_signature(net)
        if sig == self._ipv4_sig:
            return
        # Record first: a failed apply must not retry on every loop pass.
        self._ipv4_sig = sig
        self.ipv4_error = self.apply_ipv4(net)
        if self.ipv4_error:
            # Reverting means the stored config now lies; put it back in step
            # so the settings page shows what the device is actually doing.
            self.config.update({"network.ipv4_method": "auto"})
            self._ipv4_sig = self._ipv4_signature(self.config.get()["network"])

    def clear_ipv4_error(self):
        """Drop the last addressing failure notice.

        The message outlives the attempt on purpose — the browser is almost
        always disconnected at the moment a static address fails — so it has
        to be dismissed explicitly or by a fresh attempt, or it would sit on
        the settings page until the next restart.
        """
        self.ipv4_error = ""

    # ── portal API ────────────────────────────────────────────────────────
    def request_join(self, ssid: str, psk: str):
        with self._lock:
            self._join_request = (ssid, psk)
        self.last_error = ""

    def _take_join_request(self):
        with self._lock:
            req, self._join_request = self._join_request, None
        return req

    # ── state machine ─────────────────────────────────────────────────────
    def _enter_ap(self):
        # brcmfmac often cannot scan once the AP is up — capture the
        # neighborhood first so the portal has a network list to show.
        self.wifi_scan()
        if self._ap_up():
            self.status = "ap"
            ssid = self.ap_ssid()
            log.info("Setup AP up: %s (%s)", ssid, AP_IP)
            self.state.set_mode(DisplayMode.SETUP, ssid=ssid, url=AP_IP)
        else:
            self.status = "error"
            self.state.set_mode(DisplayMode.ERROR,
                                text="WiFi setup failed", hint="power cycle to retry")

    def _passive_loop(self):
        """network.manage=false: provisioning is off, but static addressing
        is still ours to apply."""
        while not self.stop_event.is_set():
            self._maybe_apply_ipv4()
            self.stop_event.wait(5)

    def run_loop(self):
        cfg = self.config.get()["network"]
        if not cfg["manage"]:
            log.info("Network management disabled (network.manage=false)")
            self._passive_loop()
            return

        if self.station_profile_names():
            self.status = "connecting"
            log.info("Waiting for saved WiFi (%ds timeout)", cfg["boot_connect_timeout_s"])
            if not self._wait_connected(cfg["boot_connect_timeout_s"]):
                log.warning("Saved WiFi did not connect — entering setup AP")
                self._enter_ap()
            else:
                self.status = "online"
                log.info("WiFi online (%s)", self.ip_address())
        else:
            log.info("No saved WiFi — entering setup AP")
            self._enter_ap()

        while not self.stop_event.is_set():
            req = self._take_join_request()
            if req is not None:
                ssid, psk = req
                was_ap = self.status == "ap"
                self.status = "joining"
                self.state.set_mode(DisplayMode.CONNECTING, ssid=ssid)
                if was_ap:
                    # Let the portal's HTTP response reach the phone and give
                    # the captive sheet a moment to show the reconnect
                    # instructions before the AP (and the sheet) vanish.
                    self.stop_event.wait(AP_HANDOFF_GRACE_S)
                    self._ap_down()
                if self._join(ssid, psk):
                    self.status = "online"
                    self.last_error = ""
                    if self.config.get()["network"]["ipv4_method"] == "manual":
                        # _join rebuilds the profile from scratch, so a saved
                        # static address has to be pushed onto it again.
                        self._ipv4_sig = None
                    self.state.set_mode(DisplayMode.NORMAL)
                    log.info("Joined %s (%s)", ssid, self.ip_address())
                else:
                    self.last_error = f"Couldn't join {ssid} — wrong password?"
                    log.warning(self.last_error)
                    self._enter_ap()
            elif self.status == "online":
                self._maybe_apply_ipv4()
                if not self.wlan_connected() and not self.station_profile_names():
                    # Profile gone (e.g. deleted underneath us): back to setup.
                    log.warning("No WiFi profile remains — entering setup AP")
                    self._enter_ap()
            self.stop_event.wait(1 if self.status in ("ap", "joining") else 15)

    def run(self):
        try:
            self.run_loop()
        except Exception:
            log.exception("NetManager crashed — network provisioning inactive")

#!/usr/bin/env python3
"""Logic smoke tests, run on the Pi (needs PIL, not the panel):

    /opt/marquee/venv/bin/python /opt/marquee/tests/on_pi_smoke.py

Stubs out rgbmatrix before loading the package so the pure logic is exercised
without touching the hardware.
"""

import os
import sys
import json
import time
import types
import tempfile

m = types.ModuleType("rgbmatrix")
m.RGBMatrix = object
m.RGBMatrixOptions = object
m.graphics = types.SimpleNamespace(Font=object, Color=object, DrawText=None)
sys.modules["rgbmatrix"] = m

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading  # noqa: E402

import marquee.config as cfgmod  # noqa: E402
from marquee.config import (  # noqa: E402
    Config, parse_hhmm, hex_to_rgb, scale_rgb,
)
from marquee.display.render import (  # noqa: E402
    is_within_schedule, is_within_dim_window,
)
from marquee.display.matrix import (  # noqa: E402
    wrap_two_lines, format_remaining, compute_text_layout,
)
from marquee.display.state import Session, State  # noqa: E402
from marquee.plex import filters  # noqa: E402
import marquee.plex.client as plexclient  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    status = "ok" if cond else "FAIL"
    if not cond:
        failures += 1
    print(f"  {status}  {name}")


class FakeFont:
    def __init__(self, height, baseline):
        self.height = height
        self.baseline = baseline


def sess(key, thumb="t"):
    return Session(session_key=key, title=key, subtitle="", user="u",
                   progress=0.5, thumb_path=thumb)


print("wrap_two_lines")
check("short text stays on one line", wrap_two_lines("Hello", 12) == ("Hello", ""))
l1, l2 = wrap_two_lines("S04E03 Got Richmond's Trophy Back", 12)
check("no word reordering (line1 prefix)", l1 == "S04E03 Got")
check("overflow ellipsized", l2.endswith("…"))
check("single overlong word truncated", wrap_two_lines("Antidisestablishmentarianism", 8)[0] == "")

print("format_remaining")
check("empty without duration", format_remaining(0, 0) == "")
check("hours form", format_remaining(2 * 3600e3 + 4 * 60e3, 3600e3) == "1h04m left")
check("minutes form", format_remaining(42 * 60e3, 0) == "42m left")
check("seconds form", format_remaining(38e3, 0) == "38s left")
check("clamps negative", format_remaining(10e3, 20e3) == "0s left")

print("compute_text_layout")
fonts = [FakeFont(13, 11), FakeFont(13, 11), FakeFont(7, 6), FakeFont(7, 6)]
ys = compute_text_layout(fonts, bottom=50)
check("one baseline per font", len(ys) == 4)
check("monotonic baselines", all(a < b for a, b in zip(ys, ys[1:])))
tops = [y - f.baseline for y, f in zip(ys, fonts)]
bottoms = [t + f.height for t, f in zip(tops, fonts)]
check("blocks do not overlap", all(bottoms[i] <= tops[i + 1] for i in range(3)))
check("fits region", bottoms[-1] <= 50)

# Hiding the user line hands its space back to the other rows rather than
# leaving a hole where it was.
three = compute_text_layout(fonts[:3], bottom=50)
check("one baseline per remaining font", len(three) == 3)
check("still monotonic", all(a < b for a, b in zip(three, three[1:])))
t3 = [y - f.baseline for y, f in zip(three, fonts[:3])]
b3 = [t + f.height for t, f in zip(t3, fonts[:3])]
check("three blocks do not overlap", all(b3[i] <= t3[i + 1] for i in range(2)))
check("three blocks fit the region", b3[-1] <= 50)
check("the rows spread out rather than leaving a gap", b3[-1] > bottoms[-2])

print("State")
st = State()
st.replace([sess("a"), sess("b")])
check("first session current", st.current().session_key == "a")
st.maybe_cycle(0)
check("cycles to next", st.current().session_key == "b")
st.replace([sess("b"), sess("c")])
check("current key survives replace", st.current().session_key == "b")
st.replace([sess("x")])
check("vanished key falls back to first", st.current().session_key == "x")
img = object()
s1 = sess("k", thumb="same"); s1.poster = img
st.replace([s1])
s2 = sess("k", thumb="same")
st.replace([s2])
check("poster carried when thumb matches", st.current().poster is img)
s3 = sess("k", thumb="different")
st.replace([s3])
check("poster dropped when thumb changes", st.current().poster is None)

print("Config")
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "config.json")

    c = Config(path)
    d = c.get()
    check("defaults load", d["display"]["cycle_seconds"] == 10)
    check("client id generated", len(d["device"]["client_id"]) == 36)
    check("unprovisioned by default", d["provisioned"] is False)
    # A fresh or factory-reset device must raise its own setup AP.
    check("network managed by default", d["network"]["manage"] is True)
    check("no plex server by default", d["plex"]["url"] == "")
    check("file written 0600", oct(os.stat(path).st_mode & 0o777) == "0o600")

    gen0 = c.generation
    changed = c.update({"display.cycle_seconds": "15", "plex.url": "http://x:32400/"})
    check("update reports changes", changed == {"display.cycle_seconds", "plex.url"})
    check("int coerced", c.get()["display"]["cycle_seconds"] == 15)
    check("url normalized", c.get()["plex"]["url"] == "http://x:32400")
    check("generation bumped", c.generation == gen0 + 1)
    check("noop update no bump",
          c.update({"display.cycle_seconds": 15}) == set() and c.generation == gen0 + 1)

    try:
        c.update({"display.cycle_seconds": 1})
        check("range rejected", False)
    except ValueError:
        check("range rejected", True)
    try:
        c.update({"display.schedule_start": "25:00"})
        check("bad time rejected", False)
    except ValueError:
        check("bad time rejected", True)
    try:
        c.update({"nonsense.key": 1})
        check("unknown key rejected", False)
    except ValueError:
        check("unknown key rejected", True)
    check("failed update leaves value", c.get()["display"]["cycle_seconds"] == 15)

    # Static addressing: a bad address on this device's only link strands it,
    # so validation is deliberately strict.
    for bad in ("192.168.1", "192.168.1.256", "192.168.1.1.1",
                "192.168.01.5", "not.an.ip.addr", "192.168.1.a"):
        try:
            c.update({"network.ipv4_address": bad})
            check(f"bad ipv4 rejected ({bad})", False)
        except ValueError:
            check(f"bad ipv4 rejected ({bad})", True)
    c.update({"network.ipv4_address": "192.168.2.50",
              "network.ipv4_gateway": "192.168.2.1",
              "network.ipv4_dns": "192.168.2.1 1.1.1.1",
              "network.ipv4_method": "manual"})
    check("ipv4 address stored", c.get()["network"]["ipv4_address"] == "192.168.2.50")
    check("dns list normalized", c.get()["network"]["ipv4_dns"] == "192.168.2.1, 1.1.1.1")
    check("empty ipv4 allowed under DHCP",
          c.update({"network.ipv4_method": "auto", "network.ipv4_gateway": ""})
          == {"network.ipv4_method", "network.ipv4_gateway"})
    try:
        c.update({"network.ipv4_method": "dhcp"})
        check("unknown ipv4 method rejected", False)
    except ValueError:
        check("unknown ipv4 method rejected", True)
    check("addressing is not a restart-required change",
          not ({"network.ipv4_method", "network.ipv4_address"} & cfgmod.RESTART_REQUIRED))

    # Cross-field rules: a half-filled static address must be refused at save
    # time, not discovered by the device after it has dropped off the network.
    c.update({"network.ipv4_method": "auto", "network.ipv4_address": "",
              "network.ipv4_gateway": "", "network.ipv4_prefix": 24})
    for name, bad in (
            ("manual with nothing filled in", {"network.ipv4_method": "manual"}),
            ("manual without a gateway",
             {"network.ipv4_method": "manual", "network.ipv4_address": "192.168.2.50"}),
            ("manual without an address",
             {"network.ipv4_method": "manual", "network.ipv4_gateway": "192.168.2.1"}),
            ("gateway off the subnet",
             {"network.ipv4_method": "manual", "network.ipv4_address": "192.168.2.50",
              "network.ipv4_gateway": "10.0.0.1"}),
            ("prefix that excludes the gateway",
             {"network.ipv4_method": "manual", "network.ipv4_address": "192.168.2.50",
              "network.ipv4_gateway": "192.168.2.1", "network.ipv4_prefix": 30})):
        try:
            c.update(bad)
            check(f"rejected: {name}", False)
        except ValueError:
            check(f"rejected: {name}", True)
    check("rejected combination changes nothing",
          c.get()["network"]["ipv4_method"] == "auto"
          and c.get()["network"]["ipv4_address"] == ""
          and c.get()["network"]["ipv4_prefix"] == 24)
    check("complete static address accepted",
          c.update({"network.ipv4_method": "manual",
                    "network.ipv4_address": "192.168.2.50",
                    "network.ipv4_gateway": "192.168.2.1",
                    "network.ipv4_prefix": 24}))
    check("returning to auto needs no addresses",
          "network.ipv4_method" in c.update({"network.ipv4_method": "auto"}))
    # An unrelated save must never be blocked by stored addressing.
    c.update({"network.ipv4_method": "manual"})   # valid: addresses still stored
    check("unrelated settings still savable",
          c.update({"plex.poll_seconds": 11}) == {"plex.poll_seconds"})
    c.update({"network.ipv4_method": "auto"})

    c2 = Config(path)
    check("persisted across reload", c2.get()["display"]["cycle_seconds"] == 15)
    check("client id stable", c2.get()["device"]["client_id"] == d["device"]["client_id"])

    # Corrupt file falls back instead of boot-looping
    bad = os.path.join(td, "bad.json")
    with open(bad, "w") as f:
        f.write("{ not json")
    cb = Config(bad)
    check("corrupt file recovers", cb.get()["display"]["cycle_seconds"] == 10)
    check("corrupt file preserved", os.path.exists(bad + ".corrupt"))

print("factory reset")
from marquee import factoryreset  # noqa: E402
from marquee.netmgr import STATION_CON  # noqa: E402

with tempfile.TemporaryDirectory() as td:

    # Neither nmcli nor systemd-run may actually run from a test, and the
    # default config path must be redirected — main() with no argument would
    # otherwise wipe this Pi's real /var/lib/marquee/config.json.
    ran = []
    cpath = os.path.join(td, "wipe.json")
    real_run, real_popen = factoryreset.subprocess.run, factoryreset.subprocess.Popen
    real_cfg_path = cfgmod.CONFIG_PATH
    factoryreset.subprocess.run = lambda args, **kw: ran.append(args)
    factoryreset.subprocess.Popen = lambda args, **kw: ran.append(args)
    cfgmod.CONFIG_PATH = cpath
    try:
        Config(cpath)
        check("config exists before reset", os.path.exists(cpath))

        factoryreset.reset(cpath, keep_wifi=True, restart=False)
        check("reset removes config", not os.path.exists(cpath))
        check("keep_wifi leaves profiles alone", not ran)

        try:
            factoryreset.reset(cpath, keep_wifi=True, restart=False)
            check("reset of a missing config does not raise", True)
        except Exception as e:
            check(f"reset of a missing config does not raise ({e})", False)

        # A wiped config must come back unprovisioned, i.e. into the wizard.
        check("device is unprovisioned after reset",
              Config(cpath).get()["provisioned"] is False)

        ran.clear()
        factoryreset.reset(cpath, keep_wifi=False, restart=True)
        deleted = [a for a in ran if a[:3] == ["nmcli", "con", "delete"]]
        check("full reset deletes the station profile",
              any(STATION_CON in a for a in deleted))
        check("full reset schedules a restart",
              any("systemd-run" in a[0] for a in ran))

        # Argument parsing: a typo must not be silently read as a full wipe.
        ran.clear()
        check("CLI rejects unknown args", factoryreset.main(["--wipe-everything"]) == 2)
        check("rejected CLI args do nothing", not ran)
        check("CLI --keep-wifi -y runs",
              factoryreset.main(["--keep-wifi", "-y", "--no-restart"]) == 0)
        check("CLI --keep-wifi kept profiles", not ran)
        check("CLI full reset deletes profiles",
              factoryreset.main(["-y", "--no-restart"]) == 0 and ran)
    finally:
        factoryreset.subprocess.run = real_run
        factoryreset.subprocess.Popen = real_popen
        cfgmod.CONFIG_PATH = real_cfg_path

print("NetManager")
import threading
from marquee.netmgr import NetManager, AP_CON, STATION_CON
from marquee.display.state import DisplayMode


class FakeNM:
    """Scripted nmcli. Profiles and link state mutate like the real thing."""
    def __init__(self):
        self.profiles = {}
        self.wlan = "disconnected"
        self.fail_join = False
        self.fail_up = set()      # profile names whose `con up` fails
        self.ipv4 = {}            # profile → {nmcli property: value}
        self.active = ""
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(" ".join(args))
        j = self.calls[-1]
        if "dev wifi list" in j:
            return 0, "My\\: Net:78:WPA2\nOther:45:\nMy\\: Net:12:WPA2\nMarquee-Setup-AB12:99:\n"
        if "NAME,TYPE con show" in j:
            return 0, "".join(f"{n}:802-11-wireless\n" for n in self.profiles)
        if "con add" in j:
            self.profiles[args[args.index("con-name") + 1]] = True
            return 0, ""
        if "con mod" in j:
            name = args[3]
            if name not in self.profiles:
                return 1, ""
            props = self.ipv4.setdefault(name, {})
            for k, v in zip(args[4::2], args[5::2]):
                props[k] = v
            return 0, ""
        if "con delete" in j:
            self.profiles.pop(args[-1], None)
            return 0, ""
        if "con up" in j:
            name = args[-1]
            if (name == STATION_CON and self.fail_join) or name in self.fail_up:
                self.wlan = "disconnected"
                return 1, ""
            self.wlan = "connected"
            self.active = name
            return 0, ""
        if "con down" in j:
            self.wlan = "disconnected"
            self.active = ""
            return 0, ""
        if "dev status" in j:
            return 0, f"wlan0:{self.wlan}\nlo:unmanaged\n"
        if "GENERAL.CONNECTION" in j:
            return 0, f"GENERAL.CONNECTION:{self.active or '--'}\n"
        if "dev show" in j:
            return 0, "IP4.ADDRESS[1]:192.168.5.7/24\n"
        return 0, ""


with tempfile.TemporaryDirectory() as td:
    nc = Config(os.path.join(td, "net.json"))
    nc.update({"network.join_timeout_s": 10})
    fake = FakeNM()
    nstate = State()
    nstop = threading.Event()
    nm = NetManager(nc, nstate, nstop, run_cmd=fake)

    nets = nm.wifi_scan()
    check("scan parses escaped colon ssid", nets[0]["ssid"] == "My: Net")
    check("scan dedupes to strongest", nets[0]["signal"] == 78)
    check("scan hides own setup AP", all(not n["ssid"].startswith("Marquee-Setup")
                                         for n in nets))
    check("scan marks security", nets[0]["secured"] and not nets[1]["secured"])
    check("no station profiles yet", nm.station_profile_names() == [])

    def wait_status(want, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if nm.status == want:
                return True
            time.sleep(0.1)
        return False

    nm.start()
    check("boots into AP mode", wait_status("ap"))
    check("AP profile created", AP_CON in fake.profiles)
    mode, payload = nstate.get_mode()
    check("panel shows setup screen", mode is DisplayMode.SETUP and
          payload["ssid"].startswith("Marquee-Setup-"))

    fake.fail_join = True
    nm.request_join("My: Net", "wrongpass")
    check("failed join returns to AP", wait_status("joining") and wait_status("ap"))
    check("failed join reported", "My: Net" in nm.last_error)
    check("bad profile deleted", STATION_CON not in fake.profiles)

    fake.fail_join = False
    nm.request_join("My: Net", "rightpass")
    check("successful join goes online", wait_status("online"))
    check("station profile kept", STATION_CON in fake.profiles)
    mode, _ = nstate.get_mode()
    check("panel back to normal", mode is DisplayMode.NORMAL)
    check("ip parsed", nm.ip_address() == "192.168.5.7")
    psk_cmd = [c for c in fake.calls if "wifi-sec.psk" in c][-1]
    check("psk passed to nmcli", "rightpass" in psk_cmd)
    nstop.set()
    nm.join(timeout=5)

print("static addressing")
with tempfile.TemporaryDirectory() as td:
    ic = Config(os.path.join(td, "ip.json"))
    ifake = FakeNM()
    ifake.profiles[STATION_CON] = True
    ifake.active = STATION_CON
    inm = NetManager(ic, State(), threading.Event(), run_cmd=ifake)

    check("active profile found", inm.active_station_profile() == STATION_CON)
    ifake.active = AP_CON
    check("AP is never the station profile",
          inm.active_station_profile() == STATION_CON)
    ifake.active = STATION_CON

    check("manual needs an address",
          "gateway" in inm.apply_ipv4({"ipv4_method": "manual", "ipv4_address": "",
                                       "ipv4_prefix": 24, "ipv4_gateway": "",
                                       "ipv4_dns": ""}))

    manual = {"ipv4_method": "manual", "ipv4_address": "192.168.2.50",
              "ipv4_prefix": 24, "ipv4_gateway": "192.168.2.1",
              "ipv4_dns": "192.168.2.1, 1.1.1.1"}
    check("manual applies cleanly", inm.apply_ipv4(manual) == "")
    props = ifake.ipv4[STATION_CON]
    check("address written with prefix", props["ipv4.addresses"] == "192.168.2.50/24")
    check("gateway written", props["ipv4.gateway"] == "192.168.2.1")
    check("dns written", props["ipv4.dns"] == "192.168.2.1, 1.1.1.1")
    check("method manual", props["ipv4.method"] == "manual")

    # The whole point of the rollback: a static address that will not come up
    # must not leave this device unreachable on its only link.
    ifake.fail_up.add(STATION_CON)
    err = inm.apply_ipv4(manual)
    check("failed apply reports an error", bool(err) and "DHCP" in err)
    check("failed apply reverts to DHCP",
          ifake.ipv4[STATION_CON]["ipv4.method"] == "auto")
    check("revert clears the static address",
          ifake.ipv4[STATION_CON]["ipv4.addresses"] == "")
    ifake.fail_up.clear()

    check("auto applies cleanly",
          inm.apply_ipv4({"ipv4_method": "auto", "ipv4_address": "",
                          "ipv4_prefix": 24, "ipv4_gateway": "",
                          "ipv4_dns": ""}) == "")
    check("auto clears the gateway", ifake.ipv4[STATION_CON]["ipv4.gateway"] == "")

    # Applies only on change, and a rollback puts the config back in step
    # with what the device is actually doing.
    ifake.calls.clear()
    inm._maybe_apply_ipv4()
    check("unchanged config touches nothing",
          not [c for c in ifake.calls if "con mod" in c])
    ic.update({"network.ipv4_method": "manual",
               "network.ipv4_address": "192.168.2.50",
               "network.ipv4_gateway": "192.168.2.1"})
    inm._maybe_apply_ipv4()
    check("changed config is applied",
          ifake.ipv4[STATION_CON]["ipv4.method"] == "manual" and not inm.ipv4_error)

    ifake.fail_up.add(STATION_CON)
    ic.update({"network.ipv4_address": "192.168.9.99",
               "network.ipv4_gateway": "192.168.9.1"})
    inm._maybe_apply_ipv4()
    check("rollback surfaces an error", bool(inm.ipv4_error))
    check("rollback rewrites the config to auto",
          ic.get()["network"]["ipv4_method"] == "auto")
    ifake.calls.clear()
    inm._maybe_apply_ipv4()
    check("a failed apply is not retried every pass",
          not [c for c in ifake.calls if "con mod" in c])

    check("no profile is an error, not a crash",
          NetManager(ic, State(), threading.Event(),
                     run_cmd=FakeNM()).apply_ipv4(manual) != "")

    # The notice outlives the attempt (the browser is disconnected when it
    # happens), so it has to be dismissible or a new attempt has to clear it.
    inm.ipv4_error = "stale"
    inm.clear_ipv4_error()
    check("error can be dismissed", inm.ipv4_error == "")

print("HA TV rule")
from marquee.ha import HomeAssistant  # noqa: E402


class FakeHA(HomeAssistant):
    def __init__(self, states):
        self.states = states
        self.asked = []

    def get_state(self, entity_id):
        self.asked.append(entity_id)
        return self.states.get(entity_id)


TV = "media_player.tv"
for tv, sun, want_sunset, want_always in (
        ("playing", "below_horizon", True, True),
        ("playing", "above_horizon", False, True),
        ("off",     "below_horizon", False, False),
        (None,      "below_horizon", False, False),
        ("standby", "below_horizon", False, False)):
    ha = FakeHA({TV: tv, "sun.sun": sun})
    check(f"tv={tv} sun={sun} → active after sunset {want_sunset}",
          ha.tv_active(TV, True) is want_sunset)
    ha2 = FakeHA({TV: tv, "sun.sun": sun})
    check(f"tv={tv} sun={sun} → active whenever on {want_always}",
          ha2.tv_active(TV, False) is want_always)
    check(f"tv={tv}: sun not consulted when not required",
          "sun.sun" not in ha2.asked)

# ha.tv_action decides which flag the poller raises: "dim" drops the
# brightness, "off" blanks the panel, and never both at once.
import marquee.ha as hamod  # noqa: E402


class OnePass(threading.Event):
    """Ends the poller loop at its trailing wait, after exactly one cycle."""

    def wait(self, timeout=None):
        self.set()
        return True


for action, tv, want_dim, want_blank in (
        ("dim", "playing", True,  False),
        ("off", "playing", False, True),
        ("dim", "off",     False, False),
        ("off", "off",     False, False)):
    with tempfile.TemporaryDirectory() as td:
        c = cfgmod.Config(os.path.join(td, "config.json"))
        c.update({"ha.enabled": True, "ha.url": "http://ha:8123",
                  "ha.token": "t", "ha.tv_entity": TV,
                  "ha.require_sunset": False, "ha.tv_action": action,
                  "ha.poll_seconds": 5})
        st = State()
        fake = FakeHA({TV: tv})
        orig = hamod.HomeAssistant
        hamod.HomeAssistant = lambda url, token: fake
        stop = OnePass()
        try:
            hamod.ha_poller_loop(c, st, stop)
        finally:
            hamod.HomeAssistant = orig
        check(f"action={action} tv={tv} → dim={want_dim} blank={want_blank}",
              st.dim is want_dim and st.ha_blank is want_blank)


with tempfile.TemporaryDirectory() as td:
    c = cfgmod.Config(os.path.join(td, "config.json"))
    try:
        c.update({"ha.tv_action": "nap"})
        check("unknown tv_action rejected", False)
    except ValueError:
        check("unknown tv_action rejected", True)

print("web link flow")
with tempfile.TemporaryDirectory() as td:
    import marquee.web.server as websrv

    # Stub the network calls: this exercises our state handling, not plex.tv.
    websrv.probe_server = lambda url, token="", timeout=4: {
        "ok": True, "url": "https://server:32400", "machine_id": "abc",
        "auth_required": False,
    }
    websrv.plex_auth = types.SimpleNamespace(
        pin_create=lambda cid: {"id": 1, "code": "WXYZ"},
        pin_poll=lambda cid, pid: {"token": "", "expired": False},
        account_servers=lambda cid, tok: [],
    )

    wc = Config(os.path.join(td, "web.json"))
    wstate = State()
    client = websrv.create_app(wc, wstate).test_client()

    client.post("/api/plex/auth/start", json={})
    check("link code goes on the panel", wstate.get_mode()[0] is DisplayMode.LINK_CODE)
    check("link code payload carries expiry",
          wstate.get_mode()[1].get("expires_at", 0) > time.monotonic())

    # The reported bug: abandon sign-in, add the server by hand instead.
    r = client.post("/api/plex/select", json={"url": "server:32400"})
    check("manual add saves", r.get_json()["saved"] is True)
    check("manual add clears the link code",
          wstate.get_mode()[0] is DisplayMode.NORMAL)
    check("stale poll is rejected",
          client.post("/api/plex/auth/poll").status_code == 400)

    client.post("/api/plex/auth/start", json={})
    client.post("/api/plex/auth/cancel")
    check("cancel clears the link code", wstate.get_mode()[0] is DisplayMode.NORMAL)

    # A setup screen raised by netmgr must survive an unrelated link teardown.
    wstate.set_mode(DisplayMode.SETUP, ssid="X", url="10.42.0.1")
    client.post("/api/plex/auth/cancel")
    check("cancel does not stomp other screens",
          wstate.get_mode()[0] is DisplayMode.SETUP)

print("plex sign-in cancel")
with tempfile.TemporaryDirectory() as td:
    import marquee.web.server as websrv

    websrv.plex_auth = types.SimpleNamespace(
        pin_create=lambda cid: {"id": 7, "code": "ABCD"},
        pin_poll=lambda cid, pid: {"token": "", "expired": False},
        account_servers=lambda cid, tok: [],
    )
    pc = Config(os.path.join(td, "cancel.json"))
    pstate = State()
    pclient = websrv.create_app(pc, pstate).test_client()

    pclient.post("/api/plex/auth/start", json={})
    check("code is on the panel", pstate.get_mode()[0] is DisplayMode.LINK_CODE)
    r = pclient.post("/api/plex/auth/cancel")
    check("cancel is accepted", r.status_code == 200)
    check("cancel takes the code off the panel",
          pstate.get_mode()[0] is DisplayMode.NORMAL)
    # sendBeacon posts with no JSON body and a text/plain content type.
    pclient.post("/api/plex/auth/start", json={})
    r = pclient.post("/api/plex/auth/cancel", data="",
                     content_type="text/plain;charset=UTF-8")
    check("a beacon-shaped cancel works too",
          r.status_code == 200 and pstate.get_mode()[0] is DisplayMode.NORMAL)
    # Cancelling twice, or with nothing running, must not blow up or disturb
    # whatever else the panel is showing.
    r = pclient.post("/api/plex/auth/cancel")
    check("cancelling nothing is harmless", r.status_code == 200)
    pstate.set_mode(DisplayMode.SETUP, ssid="X", url="1.2.3.4")
    pclient.post("/api/plex/auth/cancel")
    check("cancel does not clear an unrelated screen",
          pstate.get_mode()[0] is DisplayMode.SETUP)

print("theme")
with tempfile.TemporaryDirectory() as td:
    import marquee.web.server as websrv

    tc = Config(os.path.join(td, "theme.json"))
    check("defaults to following the viewer's device",
          tc.get()["web"]["theme"] == "auto")
    check("theme needs no restart", "web.theme" not in cfgmod.RESTART_REQUIRED)
    try:
        tc.update({"web.theme": "solarized"})
        check("unknown theme rejected", False)
    except ValueError:
        check("unknown theme rejected", True)

    tclient = websrv.create_app(tc, State()).test_client()
    # Rendered server-side: setting it from JS would flash the wrong palette.
    body = tclient.get("/").get_data(as_text=True)
    check("theme rendered into the page", 'data-theme="auto"' in body)
    tc.update({"web.theme": "dark"})
    check("theme change reaches the next render",
          'data-theme="dark"' in tclient.get("/").get_data(as_text=True))
    # The login page renders before any session exists, so it needs it too.
    tc.update({"web.password": "0" * 64})
    check("login page is themed too",
          'data-theme="dark"' in tclient.get("/login").get_data(as_text=True))

print("web factory reset")
with tempfile.TemporaryDirectory() as td:
    import marquee.web.server as websrv

    # systemd-run must not actually fire from a test — this Pi would wipe
    # itself. Capture the argv instead and check what would have run.
    spawned = []
    real_popen = websrv.subprocess.Popen
    websrv.subprocess.Popen = lambda args, **kw: spawned.append(args)
    try:
        rc = Config(os.path.join(td, "reset.json"))
        rstate = State()
        rclient = websrv.create_app(rc, rstate).test_client()

        r = rclient.post("/api/factory-reset", json={})
        check("reset without confirmation refused", r.status_code == 400)
        check("refused reset spawns nothing", not spawned)
        r = rclient.post("/api/factory-reset", json={"confirm": "yes"})
        check("only a real true confirms", r.status_code == 400 and not spawned)

        r = rclient.post("/api/factory-reset", json={"confirm": True})
        check("confirmed reset accepted", r.status_code == 200)
        check("reset runs detached", spawned and spawned[0][0] == "systemd-run")
        argv = " ".join(spawned[0])
        check("reset runs the shared implementation",
              "marquee.factoryreset" in argv and " -y" in argv)
        check("full reset does not keep wifi", "--keep-wifi" not in argv)
        check("reset starts in the package directory",
              f"WorkingDirectory={websrv.PACKAGE_ROOT}" in argv)
        check("reset uses this interpreter", sys.executable in argv)
        check("panel says what is happening",
              rstate.get_mode()[0] is DisplayMode.INFO)

        spawned.clear()
        r = rclient.post("/api/factory-reset",
                         json={"confirm": True, "keep_wifi": True})
        check("keep-wifi reset accepted",
              r.status_code == 200 and r.get_json()["keep_wifi"] is True)
        check("keep-wifi passes the flag", "--keep-wifi" in " ".join(spawned[0]))
        check("keep-wifi tells the browser where to come back",
              "marquee.local" in r.get_json()["reconnect_to"])

        # The wipe itself must still be gated behind the password.
        rc.update({"web.password": "0" * 64})
        spawned.clear()
        r = rclient.post("/api/factory-reset", json={"confirm": True})
        check("reset needs the settings password", r.status_code == 401)
        check("unauthorized reset spawns nothing", not spawned)
    finally:
        websrv.subprocess.Popen = real_popen

print("session filter")


def fsess(user="u", player="p", mtype="episode", state="playing"):
    return Session(session_key="k", title="t", subtitle="", user=user,
                   progress=0.0, thumb_path="", state=state,
                   player=player, media_type=mtype)


check("empty filter is the identity",
      len(filters.apply_filter([fsess(), fsess()], {})) == 2)
check("absent rules pass everything", filters.allowed(fsess(), {"users": []}))
check("allow-list keeps a match",
      filters.allowed(fsess(user="James"), {"users": ["James"]}))
check("allow-list drops a non-match",
      not filters.allowed(fsess(user="Guest"), {"users": ["James"]}))
check("matching ignores case and padding",
      filters.allowed(fsess(user="James"), {"users": ["  jAMes "]}))
check("comma string works as a rule",
      filters.allowed(fsess(user="Ana"), {"users": "James, Ana"}))
check("deny-list beats the allow-list",
      not filters.allowed(fsess(user="James"),
                          {"users": ["James"], "ignore_users": ["james"]}))
check("player allow-list applies",
      not filters.allowed(fsess(player="Kitchen iPad"),
                          {"players": ["Living Room TV"]}))
check("player deny-list applies",
      not filters.allowed(fsess(player="Kitchen iPad"),
                          {"ignore_players": ["kitchen ipad"]}))
check("hide_paused drops a paused session",
      not filters.allowed(fsess(state="paused"), {"hide_paused": True}))
check("hide_paused keeps a playing one",
      filters.allowed(fsess(state="playing"), {"hide_paused": True}))
check("media type allow-list applies",
      filters.allowed(fsess(mtype="movie"), {"media_types": ["movie"]}))
check("media type allow-list excludes",
      not filters.allowed(fsess(mtype="episode"), {"media_types": ["movie"]}))
check("unknown types collapse to 'other'", filters.bucket("clip") == "other")
check("'other' is selectable",
      filters.allowed(fsess(mtype="trailer"), {"media_types": ["other"]}))
# A session with no username must not be caught by a deny-list of real names.
check("blank user is not denied by name",
      filters.allowed(fsess(user=""), {"ignore_users": ["James"]}))
check("but a blank user fails an allow-list",
      not filters.allowed(fsess(user=""), {"users": ["James"]}))

print("filter rules in config")
with tempfile.TemporaryDirectory() as d:
    fc = Config(os.path.join(d, "config.json"))
    check("defaults show everything",
          filters.apply_filter([fsess()], fc.get()["plex"]["filter"]) != [])
    fc.update({"plex.filter.users": "James, Ana ,"})
    check("comma string stored as a list",
          fc.get()["plex"]["filter"]["users"] == ["James", "Ana"])
    fc.update({"plex.filter.media_types": ["Movie", "episode"]})
    check("media types folded to lower case",
          fc.get()["plex"]["filter"]["media_types"] == ["movie", "episode"])
    try:
        fc.update({"plex.filter.media_types": "movie, banana"})
        rejected = False
    except ValueError:
        rejected = True
    check("an unknown media type is rejected", rejected)
    check("and the stored rule is untouched",
          fc.get()["plex"]["filter"]["media_types"] == ["movie", "episode"])
    # An old config.json predates the whole subsection.
    legacy = os.path.join(d, "legacy.json")
    with open(legacy, "w") as f:
        json.dump({"version": 1, "plex": {"url": "http://x:32400"}}, f)
    check("upgrading a config without filters gets the defaults",
          Config(legacy).get()["plex"]["filter"]["hide_paused"] is False)

print("plex offline flag")

with tempfile.TemporaryDirectory() as d:
    oc = Config(os.path.join(d, "config.json"))
    oc.update({"plex.url": "http://x:32400", "plex.poll_seconds": 1,
               "plex.stale_after_failures": 2})
    ostate = State()

    class FakePlex:
        """Fails on demand, so the stale threshold can be walked up to."""
        mode = "ok"

        def __init__(self, *a, **kw):
            pass

        def get_activity(self):
            if FakePlex.mode == "fail":
                raise OSError("connection refused")
            return [fsess(user="James")]

        def fetch_poster(self, *a, **kw):
            return None

    def run(passes):
        """Let fetcher_loop make exactly `passes` polls, then fall out of it.

        The consecutive-failure count lives in a local, so the threshold can
        only be reached inside a single invocation — stopping and re-entering
        would quietly reset it and the test would prove nothing.
        """
        stop = threading.Event()
        left = [passes]
        def wait(_timeout=None):
            left[0] -= 1
            if left[0] <= 0:
                stop.set()
            return stop.is_set()
        stop.wait = wait
        plexclient.fetcher_loop(oc, ostate, stop)

    real_plex = plexclient.Plex
    plexclient.Plex = FakePlex
    try:
        FakePlex.mode = "ok"
        run(1)
        check("a good poll leaves the panel online", ostate.plex_offline is False)
        check("and the session lands", len(ostate.sessions) == 1)

        # One short of the threshold is still "online" — the counter starts
        # clean each invocation, so this really is a single failure.
        FakePlex.mode = "fail"
        run(1)
        check("one failure is not yet an outage", ostate.plex_offline is False)

        # Two failures in a row is exactly the configured threshold.
        run(2)
        check("the stale threshold marks it offline", ostate.plex_offline is True)
        check("and the sessions are cleared", ostate.sessions == [])

        FakePlex.mode = "ok"
        run(1)
        check("recovery clears the flag", ostate.plex_offline is False)

        # A filter that excludes everything must not read as an outage.
        oc.update({"plex.filter.users": "Nobody"})
        run(1)
        check("filtered-out sessions are not an outage",
              ostate.sessions == [] and ostate.plex_offline is False)

        # Clearing the server is a setup state, not a failure state.
        oc.update({"plex.filter.users": ""})
        FakePlex.mode = "fail"
        run(2)
        oc.update({"plex.url": ""})
        run(1)
        check("clearing the server clears the outage",
              ostate.plex_offline is False)
    finally:
        plexclient.Plex = real_plex

print("accent colour")
check("hex parses", hex_to_rgb("#e5a00d") == (229, 160, 13))
check("a bare hex parses", hex_to_rgb("e5a00d") == (229, 160, 13))
check("shorthand expands", hex_to_rgb("#abc") == (170, 187, 204))
for bad_c in ("#12345", "nope", "", "#gggggg"):
    try:
        hex_to_rgb(bad_c)
        ok = False
    except ValueError:
        ok = True
    check(f"rejects {bad_c!r}", ok)
check("scaling dims", scale_rgb((200, 100, 50), 0.5) == (100, 50, 25))
check("scaling clamps at the top", scale_rgb((200, 100, 50), 5) == (255, 255, 250))
check("scaling never goes negative", scale_rgb((200, 100, 50), -1) == (0, 0, 0))
# The derived "time remaining" shade must stay close to the hand-picked value
# it replaced, or every existing panel changes appearance on upgrade.
was = (170, 120, 20)
now_c = scale_rgb(hex_to_rgb("#e5a00d"), cfgmod.REMAIN_SCALE)
check("the default still looks like the old amber",
      all(abs(a - b) <= 12 for a, b in zip(was, now_c)))

with tempfile.TemporaryDirectory() as d:
    ac = Config(os.path.join(d, "config.json"))
    check("accent defaults to plex amber",
          ac.get()["display"]["accent"] == "#e5a00d")
    ac.update({"display.accent": "3AA0FF"})
    check("stored normalized with a hash and lower case",
          ac.get()["display"]["accent"] == "#3aa0ff")
    try:
        ac.update({"display.accent": "burgundy"})
        rejected = False
    except ValueError:
        rejected = True
    check("a non-colour is rejected", rejected)
    try:
        ac.update({"display.idle_mode": "disco"})
        rejected = False
    except ValueError:
        rejected = True
    check("an unknown idle mode is rejected", rejected)
    for mode in ("clock", "blank", "poster"):
        ac.update({"display.idle_mode": mode})
        check(f"idle mode {mode!r} accepted",
              ac.get()["display"]["idle_mode"] == mode)

    check("the poster starts on the left",
          ac.get()["display"]["poster_side"] == "left")
    try:
        ac.update({"display.poster_side": "middle"})
        rejected = False
    except ValueError:
        rejected = True
    check("an unknown poster side is rejected", rejected)
    for side in ("Right", "left"):
        ac.update({"display.poster_side": side})
        check(f"poster side {side!r} accepted and folded",
              ac.get()["display"]["poster_side"] == side.lower())

    check("scrolling starts at normal speed",
          ac.get()["display"]["scroll_speed"] == "normal")
    try:
        ac.update({"display.scroll_speed": "ludicrous"})
        rejected = False
    except ValueError:
        rejected = True
    check("an unknown scroll speed is rejected", rejected)
    for speed in cfgmod.SCROLL_SPEEDS:
        ac.update({"display.scroll_speed": speed})
        check(f"scroll speed {speed!r} accepted",
              ac.get()["display"]["scroll_speed"] == speed)
    # Speeds are px/sec now, so the frame rate can move without retuning
    # them. Sub-pixel is still the whole point: at 60fps every one of these
    # is well under a pixel a frame, which only works because the render
    # loop's accumulator is a float.
    frame_s = cfgmod.SCROLL_FRAME_MS / 1000
    check("speeds are ordered slow < normal < fast",
          cfgmod.SCROLL_SPEEDS["slow"] < cfgmod.SCROLL_SPEEDS["normal"]
          < cfgmod.SCROLL_SPEEDS["fast"])
    check("every speed is under a pixel a frame",
          all(v * frame_s < 1 for v in cfgmod.SCROLL_SPEEDS.values()))
    # The panel runs at ~120Hz; the loop should divide into that, not beat
    # against it. 60fps is every second refresh.
    check("the frame period is a whole number of 120Hz panel refreshes",
          abs((cfgmod.SCROLL_FRAME_MS / (1000 / 120)) % 1) < 1e-9)

    check("the user line is shown by default",
          ac.get()["display"]["show_user"] is True)
    ac.update({"display.show_user": False})
    check("and can be turned off",
          ac.get()["display"]["show_user"] is False)

print("sub-pixel title scrolling")
# Needs the real BDF fonts, which is why this lives in the on-Pi suite.
from marquee.display.matrix import load_pil_title_font  # noqa: E402
from marquee.display.render import make_title_phases    # noqa: E402

_pf = load_pil_title_font()
check("the title font compiles for PIL", _pf is not None)
if _pf is not None:
    TITLE = "A Very Long Episode Title That Has To Scroll"
    H = 13
    phases = make_title_phases(TITLE, _pf, H)
    check("one phase per sub-pixel step",
          len(phases) == cfgmod.SCROLL_SUBPIXEL)
    check("every phase is the same size",
          len({p.size for p in phases}) == 1)
    check("the strip is as wide as the text",
          phases[0].width == int(_pf.getlength(TITLE)))

    # Phase 0 is the string on whole pixels; the rest must carry partial
    # coverage or nothing has actually been gained over DrawText.
    def levels(img):
        return {px[0] for px in img.convert("RGB").getdata()}

    check("phase 0 is crisp (no partial columns)",
          levels(phases[0]) <= {0, 220})
    check("later phases are anti-aliased",
          all(levels(p) - {0, 220} for p in phases[1:]))
    check("no two phases are identical",
          len({p.tobytes() for p in phases}) == len(phases))

    # A phase that emits less light than its neighbours makes the title pulse
    # as it slides, so what has to hold across phases is the *light*, not the
    # 8-bit values: the panel library luminance-corrects everything it is
    # handed, and summing coded values would call a pulsing title conserved.
    def cie(v):
        x = v / 255 * 100
        return x / 902.3 if x <= 8 else ((x + 16) / 116) ** 3

    light = [sum(cie(px[0]) for px in p.convert("RGB").getdata())
             for p in phases]
    check("emitted light is conserved across phases (<2% spread)",
          (max(light) - min(light)) / max(light) < 0.02)
    # And the correction is actually doing something — without it the split
    # phases come out well over 2% dark, which is the bug this guards.
    raw = [sum(px[0] for px in p.convert("RGB").getdata()) for p in phases]
    check("partial phases carry more coded ink than the crisp one",
          min(raw[1:]) > raw[0] * 1.02)

    # The addressing the render loop uses: whole pixels choose the window,
    # the fraction chooses the phase. Every offset in range must yield a
    # 64px crop that fits inside the strip — an off-by-one here is a
    # blacked-out or crashed title, not a cosmetic glitch.
    span = phases[0].width - 64
    bad = []
    steps = int(span * cfgmod.SCROLL_SUBPIXEL)
    for i in range(steps + 1):
        off = i / cfgmod.SCROLL_SUBPIXEL
        sx, k = divmod(int(off * cfgmod.SCROLL_SUBPIXEL), cfgmod.SCROLL_SUBPIXEL)
        if not (0 <= k < len(phases)) or sx < 0 or sx + 64 > phases[k].width:
            bad.append(off)
    check("every offset from 0 to the far end addresses a valid window",
          not bad)
    # And the two ends specifically, since those are what the bounce clamps to.
    check("offset 0 starts at the first column",
          divmod(0, cfgmod.SCROLL_SUBPIXEL) == (0, 0))
    sx, k = divmod(int(span * cfgmod.SCROLL_SUBPIXEL), cfgmod.SCROLL_SUBPIXEL)
    check("the far end lands exactly on the last column",
          sx + 64 == phases[k].width and k == 0)

print("title strip caching")
_a = Session(session_key="1", title="Same Title", subtitle="", user="",
             progress=0.0, thumb_path="/t1")
_a.title_phases = ["CACHED"]
_st = State()
_st.replace([_a])
_st.replace([Session(session_key="1", title="Same Title", subtitle="", user="",
                     progress=0.5, thumb_path="/t1")])
check("the strip survives a poll that did not change the title",
      _st.current().title_phases == ["CACHED"])
_st.replace([Session(session_key="1", title="A New Title", subtitle="", user="",
                     progress=0.5, thumb_path="/t1")])
check("and is dropped when the title changes",
      _st.current().title_phases is None)

print("schedule and dim windows")
t = lambda h, m=0: __import__("datetime").time(h, m)
# Equal endpoints mean opposite things, deliberately: an unset schedule must
# leave the panel on, an unset dim window must leave brightness alone.
check("equal endpoints = always on",
      is_within_schedule(t(0), t(0), t(3)) is True)
check("equal non-midnight endpoints are also always on",
      is_within_schedule(t(7), t(7), t(3)) is True)
check("equal endpoints = never dim",
      is_within_dim_window(t(0), t(0), t(3)) is False)
check("equal non-midnight endpoints never dim",
      is_within_dim_window(t(7), t(7), t(9)) is False)
check("daytime window includes its start", is_within_schedule(t(8), t(23), t(8)))
check("daytime window excludes its stop", not is_within_schedule(t(8), t(23), t(23)))
check("inside a daytime window", is_within_schedule(t(8), t(23), t(12)))
check("before a daytime window", not is_within_schedule(t(8), t(23), t(7)))
# The device in use runs 08:00 → 00:00, so end-of-day must keep working.
check("end-of-day stop stays on late", is_within_schedule(t(8), t(0), t(23, 59)))
check("end-of-day stop is off early", not is_within_schedule(t(8), t(0), t(3)))
check("overnight dim window wraps midnight",
      is_within_dim_window(t(22), t(7), t(2)))
check("overnight dim window is off midday",
      not is_within_dim_window(t(22), t(7), t(12)))
check("overnight dim window includes its start",
      is_within_dim_window(t(22), t(7), t(22)))
check("overnight dim window excludes its stop",
      not is_within_dim_window(t(22), t(7), t(7)))

print("idle poster memory")
istate = State()
check("nothing held on a fresh device", istate.last_poster is None)
held = sess("a")
held.poster = "ARTWORK"          # stand-in; render only hands it to PIL
istate.replace([held])
check("nothing held while something is playing", istate.last_poster is None)
istate.replace([])
check("the last poster is kept when the list empties",
      istate.last_poster == "ARTWORK")
nxt = sess("b")
nxt.poster = "SECOND"
istate.replace([nxt])
istate.replace([])
check("and replaced by the next thing that plays",
      istate.last_poster == "SECOND")
# A session that never got its artwork must not wipe what is already held.
bare = sess("c")
istate.replace([bare])
istate.replace([])
check("a posterless session does not clear the held art",
      istate.last_poster == "SECOND")

print("parse_hhmm")
check("parses", parse_hhmm("07:30").hour == 7)
for bad_t in ("7", "07:60", "24:00", "a:b", ""):
    try:
        parse_hhmm(bad_t)
        ok = False
    except (ValueError, TypeError):
        ok = True
    check(f"rejects {bad_t!r}", ok)

print("update bundles")

import io  # noqa: E402
import tarfile  # noqa: E402
import marquee.update as update  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

check("versions compare numerically, not textually",
      update.is_newer("1.2.10", "1.2.9") and not update.is_newer("1.2.3", "1.2.10"))
check("v-prefix is accepted", update.is_newer("v2.0.0", "1.9.9"))
for bad_v in ("beta", "1.2.x", ""):
    try:
        update.parse_version(bad_v)
        ok = False
    except ValueError:
        ok = True
    check(f"rejects version {bad_v!r}", ok)


def make_bundle(tmp, version="9.9.9", product="marquee", key=None,
                extra_member=None, tamper=False, sign_key=None):
    """A miniature but structurally complete bundle, signed with a test key."""
    key = key or Ed25519PrivateKey.generate()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", json.dumps(
            {"product": product, "version": version}).encode())
        add("requirements.txt", b"flask>=3.0\n")
        add("marquee/__init__.py",
            f'__version__ = "{version}"\n'.encode())
        if extra_member:
            add(extra_member, b"#!/bin/sh\n")
    payload = buf.getvalue()
    sig = (sign_key or key).sign(payload)
    if tamper:
        payload = payload[:-1] + bytes([payload[-1] ^ 1])
    path = os.path.join(tmp, "test.mqup")
    with tarfile.open(path, "w") as tar:
        for name, data in (("payload.tar.gz", payload), ("payload.sig", sig)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path, key.public_key().public_bytes_raw().hex()


with tempfile.TemporaryDirectory() as d:
    path, pub = make_bundle(d)
    check("a signed bundle verifies",
          update.verify_bundle(path, pub)["version"] == "9.9.9")

    path, pub = make_bundle(d, tamper=True)
    try:
        update.verify_bundle(path, pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("a tampered payload is refused", ok)

    other = Ed25519PrivateKey.generate()
    path, pub = make_bundle(d, sign_key=other)
    try:
        update.verify_bundle(path, pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("a bundle signed with the wrong key is refused", ok)

    path, pub = make_bundle(d, product="other-thing")
    try:
        update.verify_bundle(path, pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("another product's bundle is refused", ok)

    path, pub = make_bundle(d, extra_member="evil.sh")
    try:
        update.verify_bundle(path, pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("a payload with a stray file is refused", ok)

    path, pub = make_bundle(d, version="not.a.version")
    try:
        update.verify_bundle(path, pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("an unparsable version is refused", ok)

    with open(os.path.join(d, "junk.mqup"), "wb") as f:
        f.write(b"MZ this is not a tar at all")
    try:
        update.verify_bundle(os.path.join(d, "junk.mqup"), pub)
        ok = False
    except update.UpdateError:
        ok = True
    check("a non-tar file is refused, not crashed on", ok)

print("update checker")

with tempfile.TemporaryDirectory() as d:
    update.STATE_DIR = d
    update.RESULT_PATH = os.path.join(d, "last_result.json")
    update.write_result("rolled_back", "1.0.0", "1.1.0", "boom")
    r = update.read_result()
    check("a result survives the round trip",
          r["status"] == "rolled_back" and r["to"] == "1.1.0")

    class FakeResp:
        def __init__(self, doc):
            self.doc = doc

        def raise_for_status(self):
            pass

        def json(self):
            return self.doc

    class FakeRequests:
        doc = {}

        @staticmethod
        def get(url, **kw):
            if isinstance(FakeRequests.doc, Exception):
                raise FakeRequests.doc
            return FakeResp(FakeRequests.doc)

    real_requests = sys.modules.get("requests")
    sys.modules["requests"] = FakeRequests
    real_repo = update.UPDATE_REPO
    update.UPDATE_REPO = "someone/marquee"
    try:
        up = update.Updater(None, threading.Event())

        FakeRequests.doc = {
            "tag_name": "v9.9.9",
            "body": "notes",
            "assets": [{"name": "marquee-9.9.9.mqup",
                        "browser_download_url": "https://x/marquee-9.9.9.mqup"}],
        }
        st = up.check_now()
        check("a newer release is offered",
              st["available"] and st["available"]["version"] == "9.9.9")

        FakeRequests.doc = {"tag_name": "v0.0.1", "assets": [
            {"name": "marquee-0.0.1.mqup", "browser_download_url": "u"}]}
        st = up.check_now()
        check("an older release is not", st["available"] is None)

        FakeRequests.doc = {"tag_name": "v9.9.9", "assets": [
            {"name": "notes.txt", "browser_download_url": "u"}]}
        st = up.check_now()
        check("a release without a bundle asset is not", st["available"] is None)

        FakeRequests.doc = {"tag_name": "beta", "assets": [
            {"name": "marquee-x.mqup", "browser_download_url": "u"}]}
        st = up.check_now()
        check("an unparsable tag is not an offer", st["available"] is None)

        FakeRequests.doc = OSError("no route to host")
        st = up.check_now()
        check("a failed check reports instead of raising",
              "could not check" in st["check_error"])
        check("and clears nothing it did not learn", st["available"] is None)
    finally:
        update.UPDATE_REPO = real_repo
        if real_requests is not None:
            sys.modules["requests"] = real_requests

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")

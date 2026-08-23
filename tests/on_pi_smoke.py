#!/usr/bin/env python3
"""Logic smoke tests, run on the Pi (needs PIL, not the panel):

    /opt/plex-matrix/venv/bin/python /opt/plex-matrix/tests/on_pi_smoke.py

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

import nowplaying.config as cfgmod  # noqa: E402
from nowplaying.config import Config, parse_hhmm  # noqa: E402
from nowplaying.display.matrix import (  # noqa: E402
    wrap_two_lines, format_remaining, compute_text_layout,
)
from nowplaying.display.state import Session, State  # noqa: E402

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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".factory-reset")

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

    # Migration from a legacy env file
    env = os.path.join(td, "legacy.env")
    with open(env, "w") as f:
        f.write("# comment\nPLEX_URL=https://192.168.1.3:32400\n"
                "PLEX_VERIFY_SSL=false\nHA_TOKEN=tok123\nPOLL_SECONDS=10\n"
                "BRIGHTNESS_NORMAL=45\nSCHEDULE_START=08:00\nSCHEDULE_STOP=00:00\n"
                "TAUTULLI_URL=http://dead\nBOGUS LINE\n")
    cfgmod.LEGACY_ENV_PATH = env
    mpath = os.path.join(td, "migrated.json")
    cm = Config(mpath)
    md = cm.get()
    check("migrated plex url", md["plex"]["url"] == "https://192.168.1.3:32400")
    check("migrated poll", md["plex"]["poll_seconds"] == 10)
    check("migrated brightness", md["display"]["brightness_normal"] == 45)
    check("migrated schedule", md["display"]["schedule_start"] == "08:00")
    check("ha auto-enabled with token", md["ha"]["enabled"] is True and md["ha"]["token"] == "tok123")
    check("legacy ha url default kept", md["ha"]["url"] == "https://ha.example.com")
    check("legacy tv entity default kept", md["ha"]["tv_entity"] == "media_player.living_room_tv")
    check("migrated device provisioned", md["provisioned"] is True)
    check("migration leaves network alone", md["network"]["manage"] is False)
    check("migration defaults to DHCP", md["network"]["ipv4_method"] == "auto")
    check("migration keeps sunset requirement", md["ha"]["require_sunset"] is True)
    check("unknown env keys ignored", "TAUTULLI_URL" not in json.dumps(md))

    # Factory-reset marker suppresses migration
    open(cfgmod.NO_MIGRATE_MARKER, "w").close()
    cr = Config(os.path.join(td, "reset.json"))
    check("marker blocks migration", cr.get()["provisioned"] is False)

    # Corrupt file falls back instead of boot-looping
    bad = os.path.join(td, "bad.json")
    with open(bad, "w") as f:
        f.write("{ not json")
    cb = Config(bad)
    check("corrupt file recovers", cb.get()["display"]["cycle_seconds"] == 10)
    check("corrupt file preserved", os.path.exists(bad + ".corrupt"))

print("factory reset")
from nowplaying import factoryreset  # noqa: E402
from nowplaying.netmgr import STATION_CON  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".factory-reset")

    # Neither nmcli nor systemd-run may actually run from a test, and the
    # default config path must be redirected — main() with no argument would
    # otherwise wipe this Pi's real /var/lib/nowplaying/config.json.
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
        check("reset writes no-migrate marker",
              os.path.exists(cfgmod.NO_MIGRATE_MARKER))
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
from nowplaying.netmgr import NetManager, AP_CON, STATION_CON
from nowplaying.display.state import DisplayMode


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
            return 0, "My\\: Net:78:WPA2\nOther:45:\nMy\\: Net:12:WPA2\nNowPlaying-Setup-AB12:99:\n"
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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
    nc = Config(os.path.join(td, "net.json"))
    nc.update({"network.join_timeout_s": 10})
    fake = FakeNM()
    nstate = State()
    nstop = threading.Event()
    nm = NetManager(nc, nstate, nstop, run_cmd=fake)

    nets = nm.wifi_scan()
    check("scan parses escaped colon ssid", nets[0]["ssid"] == "My: Net")
    check("scan dedupes to strongest", nets[0]["signal"] == 78)
    check("scan hides own setup AP", all(not n["ssid"].startswith("NowPlaying-Setup")
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
          payload["ssid"].startswith("NowPlaying-Setup-"))

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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
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

print("HA dim rule")
from nowplaying.ha import HomeAssistant  # noqa: E402


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
    check(f"tv={tv} sun={sun} → dim after sunset {want_sunset}",
          ha.should_dim(TV, True) is want_sunset)
    ha2 = FakeHA({TV: tv, "sun.sun": sun})
    check(f"tv={tv} sun={sun} → dim whenever on {want_always}",
          ha2.should_dim(TV, False) is want_always)
    check(f"tv={tv}: sun not consulted when not required",
          "sun.sun" not in ha2.asked)

print("web link flow")
with tempfile.TemporaryDirectory() as td:
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
    import nowplaying.web.server as websrv

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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
    import nowplaying.web.server as websrv

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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
    import nowplaying.web.server as websrv

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
    cfgmod.LEGACY_ENV_PATH = os.path.join(td, "none.env")
    cfgmod.NO_MIGRATE_MARKER = os.path.join(td, ".marker")
    import nowplaying.web.server as websrv

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
              "nowplaying.factoryreset" in argv and " -y" in argv)
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
              "nowplaying.local" in r.get_json()["reconnect_to"])

        # The wipe itself must still be gated behind the password.
        rc.update({"web.password": "0" * 64})
        spawned.clear()
        r = rclient.post("/api/factory-reset", json={"confirm": True})
        check("reset needs the settings password", r.status_code == 401)
        check("unauthorized reset spawns nothing", not spawned)
    finally:
        websrv.subprocess.Popen = real_popen

print("parse_hhmm")
check("parses", parse_hhmm("07:30").hour == 7)
for bad_t in ("7", "07:60", "24:00", "a:b", ""):
    try:
        parse_hhmm(bad_t)
        ok = False
    except (ValueError, TypeError):
        ok = True
    check(f"rejects {bad_t!r}", ok)

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")

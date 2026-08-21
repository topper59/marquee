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
        if "con delete" in j:
            self.profiles.pop(args[-1], None)
            return 0, ""
        if "con up" in j:
            name = args[-1]
            if name == STATION_CON and self.fail_join:
                return 1, ""
            self.wlan = "connected"
            return 0, ""
        if "con down" in j:
            self.wlan = "disconnected"
            return 0, ""
        if "dev status" in j:
            return 0, f"wlan0:{self.wlan}\nlo:unmanaged\n"
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

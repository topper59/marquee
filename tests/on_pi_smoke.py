#!/usr/bin/env python3
"""Logic smoke tests, run on the Pi (needs PIL, not the panel):

    /opt/plex-matrix/venv/bin/python /opt/plex-matrix/tests/on_pi_smoke.py

Stubs out rgbmatrix before loading the package so the pure logic is exercised
without touching the hardware.
"""

import os
import sys
import types

# Package import must succeed without hardware or a real env file.
os.environ.setdefault("HA_TOKEN", "test-token")

m = types.ModuleType("rgbmatrix")
m.RGBMatrix = object
m.RGBMatrixOptions = object
m.graphics = types.SimpleNamespace(Font=object, Color=object, DrawText=None)
sys.modules["rgbmatrix"] = m

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nowplaying import config  # noqa: E402
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
old_cycle = config.CYCLE_SECONDS
config.CYCLE_SECONDS = 0
st.maybe_cycle()
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
config.CYCLE_SECONDS = old_cycle

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")

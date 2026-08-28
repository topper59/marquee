#!/usr/bin/env python3
"""Force an idle screen onto the panel for a while, then put things back.

The idle screens — the clock, the held poster, and the two slideshows — only
appear when nothing is playing, which on a busy server is never. This borrows
the session filter to make the panel *think* the room is quiet: an allow-list
nobody matches filters every stream out, the panel goes idle, and the filter
comes off again afterwards.

    # on the Pi
    /opt/marquee/venv/bin/python /opt/marquee/tests/preview_idle.py recent_added
    /opt/marquee/venv/bin/python /opt/marquee/tests/preview_idle.py recent_played 120

Everything goes through the running service's HTTP API rather than
config.json. The service holds its own Config in memory and only re-reads on
its own generation counter, so a settings change written straight to the file
would be ignored until a restart.

A detached revert is armed before anything changes, the same way a firewall or
WiFi change on this device is (see CLAUDE.md): if this script is killed, the
SSH session drops, or the Pi is simply walked away from, the panel still comes
back on its own.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1"
# A user nobody is called, so the allow-list matches nothing and every stream
# is filtered out. Deliberately obvious in the settings UI if it is ever seen.
SENTINEL_USER = "marquee-preview-nobody"
# Unique per run, like the reset and update appliers: a fixed name collides
# with the previous run's unit if systemd has not collected it yet, and
# systemd-run then refuses to arm — silently, unless the caller checks.
REVERT_UNIT = f"marquee-preview-revert-{int(time.time())}"
MODES = ("clock", "blank", "poster", "recent_played", "recent_added")


def api(path: str, payload=None):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or "{}")


def settings_patch(patch: dict):
    return api("/api/settings", patch)


def arm_deadman(restore: dict, seconds: int) -> bool:
    """Schedule the restore as a detached one-shot, before anything changes.

    Returns whether it is genuinely armed. A dead-man that quietly failed to
    arm is worse than none at all — you would go on believing the panel
    restores itself — so the caller refuses to touch anything if this is False.
    """
    res = subprocess.run(
        ["systemd-run", "--collect", f"--on-active={seconds}",
         f"--unit={REVERT_UNIT}",
         # systemd timers default to AccuracySec=1min, which lets the revert
         # drift up to a minute late — harmless for safety, confusing to
         # watch. --timer-property, not -p: the latter sets properties on the
         # service unit, and AccuracySec belongs to the timer.
         "--timer-property=AccuracySec=1s",
         "curl", "-s", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", json.dumps(restore), f"{API}/api/settings"],
        capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Could not arm the revert timer: "
              f"{(res.stderr or res.stdout).strip()}", file=sys.stderr)
        return False
    return True


def cancel_deadman():
    subprocess.run(["systemctl", "stop", f"{REVERT_UNIT}.timer"],
                   capture_output=True)
    subprocess.run(["systemctl", "stop", f"{REVERT_UNIT}.service"],
                   capture_output=True)


def main(argv) -> int:
    mode = argv[0] if argv else "recent_added"
    seconds = int(argv[1]) if len(argv) > 1 else 60
    if mode not in MODES:
        print(f"usage: preview_idle.py [{'|'.join(MODES)}] [seconds]",
              file=sys.stderr)
        return 2

    try:
        cfg = api("/api/settings")
    except (urllib.error.URLError, OSError) as e:
        print(f"Cannot reach the settings API ({e}) — is marquee.service up?",
              file=sys.stderr)
        return 1
    if cfg["web"]["password"]:
        print("A settings password is set, so this script cannot drive the "
              "API. Clear it under Device, or preview by hand.", file=sys.stderr)
        return 1

    restore = {
        "display.idle_mode": cfg["display"]["idle_mode"],
        "plex.filter.users": cfg["plex"]["filter"]["users"],
    }
    # Armed first and generously, so every way this can end — Ctrl-C, a dropped
    # link, a closed laptop — still restores the panel. Nothing is changed
    # until it is confirmed armed.
    if not arm_deadman(restore, seconds + 30):
        print("Refusing to filter the panel without a working revert timer.",
              file=sys.stderr)
        return 1
    print(f"Revert armed as {REVERT_UNIT} (+{seconds + 30}s).")

    try:
        settings_patch({"display.idle_mode": mode,
                        "plex.filter.users": [SENTINEL_USER]})
        print(f"Panel forced to '{mode}' for {seconds}s "
              f"(every stream filtered out).")
        # The filter only bites on the fetcher's next poll, so wait for the
        # panel to actually go idle rather than claiming it already has.
        poll = cfg["plex"]["poll_seconds"]
        for _ in range(int(poll * 3) + 5):
            if not api("/api/status")["sessions"]:
                break
            time.sleep(1)
        else:
            print("  (streams are still getting through — is the filter set?)")
        print(f"  panel is idle; showing {mode}")
        print(f"  will restore idle_mode={restore['display.idle_mode']!r}, "
              f"users={restore['plex.filter.users']!r}")
        for left in range(seconds, 0, -10):
            print(f"  {left}s…", flush=True)
            time.sleep(min(10, left))
    except KeyboardInterrupt:
        print("\ninterrupted — restoring now")
    finally:
        settings_patch(restore)
        cancel_deadman()
        print("Restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

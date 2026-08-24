"""Factory reset, shared by the hardware button and the command line.

The button path lives in resetbtn.py; both call reset() here so there is one
implementation of "what a reset actually wipes".

A reset writes the no-migrate marker (so the legacy env file cannot resurrect
the old settings), deletes config.json, optionally deletes the WiFi profiles,
and restarts the service into the setup wizard.

Command line (on the Pi; the package lives on the path, not installed in the
venv, so it needs the working directory or PYTHONPATH):

    cd /opt/marquee
    venv/bin/python -m marquee.factoryreset --keep-wifi -y
    venv/bin/python -m marquee.factoryreset -y

Run it detached (`systemd-run --collect -p WorkingDirectory=/opt/marquee
…`) when invoking over SSH without --keep-wifi: deleting the station profile
drops the connection, which would otherwise kill the reset partway through.

--keep-wifi is the one to use for testing the wizard: it wipes the settings
(so setup runs again from the Plex step) but leaves the station profile alone,
so the device stays on the network and SSH keeps working. A full reset deletes
the WiFi credentials and the device comes back up in AP mode, unreachable
except over the setup AP or ethernet.
"""

import os
import logging
import subprocess
import sys
import time

from marquee import config as cfgmod
from marquee.netmgr import AP_CON, STATION_CON

log = logging.getLogger(__name__)


def reset(config_path: str = None, keep_wifi: bool = False,
          restart: bool = True) -> None:
    """Wipe configuration (and optionally WiFi profiles), then restart."""
    path = config_path or cfgmod.CONFIG_PATH

    try:
        os.makedirs(os.path.dirname(cfgmod.NO_MIGRATE_MARKER), exist_ok=True)
        with open(cfgmod.NO_MIGRATE_MARKER, "w") as f:
            f.write(str(time.time()) + "\n")
    except OSError as e:
        log.error("Could not write no-migrate marker: %s", e)

    try:
        os.remove(path)
        log.info("Removed %s", path)
    except OSError:
        pass

    if keep_wifi:
        log.info("Keeping WiFi profiles (--keep-wifi)")
    else:
        for con in (STATION_CON, AP_CON):
            subprocess.run(["nmcli", "con", "delete", con],
                           capture_output=True, timeout=15)
        log.info("Deleted WiFi profiles %s, %s", STATION_CON, AP_CON)

    if restart:
        # Detached so the caller (web request, SSH session, button thread)
        # is not killed along with the service.
        subprocess.Popen(["systemd-run", "--collect", "--on-active=2",
                          "systemctl", "restart", "marquee.service"])
        log.info("Service restart scheduled")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keep_wifi = "--keep-wifi" in argv
    restart = "--no-restart" not in argv
    assume_yes = "-y" in argv or "--yes" in argv
    unknown = [a for a in argv if a not in
               ("--keep-wifi", "--no-restart", "-y", "--yes")]
    if unknown:
        print(f"unknown argument: {unknown[0]}", file=sys.stderr)
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2

    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")

    what = ("Wipe settings and restart into setup (WiFi profiles kept)"
            if keep_wifi else
            "Wipe settings AND WiFi profiles — the device will come back in "
            "AP mode and you will lose SSH")
    if not assume_yes:
        print(what)
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    else:
        log.warning("FACTORY RESET: %s", what)

    reset(keep_wifi=keep_wifi, restart=restart)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Factory reset, shared by the hardware button and the command line.

The button path lives in resetbtn.py; both call reset() here so there is one
implementation of "what a reset actually wipes".

A reset deletes config.json, optionally deletes the WiFi profiles, and
restarts the service into the setup wizard.

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

from marquee import config as cfgmod
from marquee.netmgr import AP_CON, STATION_CON, wifi_profile_names

log = logging.getLogger(__name__)

# Written by the settings page when SSH is turned on there. Its presence is
# how a reset knows SSH was enabled through the UI (and should be turned off
# again) rather than set up by hand on a dev machine (and left alone).
SSHD_DROPIN = "/etc/ssh/sshd_config.d/20-marquee.conf"


def _nmcli(args: list, timeout: float = 15) -> tuple:
    """Run nmcli through *this module's* subprocess, returning (rc, stdout).

    Deliberately not netmgr's identical helper: the smoke test neutralises a
    reset by stubbing `factoryreset.subprocess`, and a second path into nmcli
    would slip straight past that stub and delete the test machine's real WiFi
    profiles. One choke point, and the stub covers everything.
    """
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "")


def _delete_wifi_profiles(run_cmd=None) -> None:
    """Delete every saved WiFi profile, not just the ones we created.

    Out-of-box means no network of its own, and the settings page promises
    exactly that: the display leaves your WiFi and raises Marquee-Setup. A
    device provisioned by hand — or by an image whose profile is named after
    the SSID — keeps its connection under some other name, so deleting only
    marquee-wifi/marquee-ap left it online and never entering setup mode.

    If the enumeration itself fails (no nmcli, NetworkManager down) fall back
    to the two names we know. A reset that quietly keeps the network is worse
    than one that deletes less than it hoped to.
    """
    run_cmd = run_cmd or _nmcli
    names = wifi_profile_names(run_cmd)
    if not names:
        names = [STATION_CON, AP_CON]
        log.warning("Could not list WiFi profiles — falling back to %s",
                    ", ".join(names))
    for con in names:
        run_cmd(["nmcli", "con", "delete", con], timeout=15)
    log.info("Deleted WiFi profiles: %s", ", ".join(names))


def reset(config_path: str = None, keep_wifi: bool = False,
          restart: bool = True) -> None:
    """Wipe configuration (and optionally WiFi profiles), then restart."""
    path = config_path or cfgmod.CONFIG_PATH

    try:
        os.remove(path)
        log.info("Removed %s", path)
    except OSError:
        pass

    # Out-of-box means SSH off — but only when the settings page enabled it
    # (the drop-in is its receipt). A dev box with hand-configured SSH and
    # no drop-in keeps its access, which is also what makes reset tests
    # safe to run over SSH.
    if os.path.exists(SSHD_DROPIN):
        try:
            os.remove(SSHD_DROPIN)
            subprocess.run(["systemctl", "disable", "--now", "ssh"],
                           capture_output=True, timeout=30)
            log.info("Disabled the web-enabled SSH access")
        except (OSError, subprocess.SubprocessError) as e:
            log.error("Could not disable SSH: %s", e)

    if keep_wifi:
        log.info("Keeping WiFi profiles (--keep-wifi)")
    else:
        _delete_wifi_profiles()

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

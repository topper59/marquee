#!/usr/bin/env python3
"""Build and sign a Marquee update bundle. Runs on the dev machine only.

    release/.venv/bin/python release/build.py            # → dist/marquee-<v>.mqup
    release/.venv/bin/python release/build.py --keygen   # one-time key creation

The signing key is the security boundary for every shipped device, so it
lives outside the repo (~/.config/marquee-release/signing.key), is created
exactly once, and is never given to CI. The matching public key is pasted
into marquee/update.py:PUBLIC_KEY_HEX — this script refuses to build a
bundle the shipped verifier would reject, so a key mismatch is caught here,
not on a customer's device.

Needs the `cryptography` package (use release/.venv, which is gitignored).
"""

import argparse
import io
import json
import os
import re
import sys
import tarfile
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.expanduser("~/.config/marquee-release/signing.key")
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = (".pyc", ".DS_Store")

sys.path.insert(0, REPO)
from marquee import update as device_update  # noqa: E402  (stdlib-only import)


def read_version() -> str:
    with open(os.path.join(REPO, "marquee", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__\s*=\s*"([^"]+)"', f.read()).group(1)


def keygen():
    if os.path.exists(KEY_PATH):
        sys.exit(f"refusing to overwrite the existing key at {KEY_PATH} — "
                 "a new key would orphan every device in the field")
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes_raw()
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed.hex() + "\n")
    pub = key.public_key().public_bytes_raw().hex()
    print(f"signing key written to {KEY_PATH} — BACK IT UP somewhere offline.")
    print("paste this into marquee/update.py as PUBLIC_KEY_HEX:")
    print(f'PUBLIC_KEY_HEX = "{pub}"')


def load_key() -> Ed25519PrivateKey:
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip()))
    except FileNotFoundError:
        sys.exit(f"no signing key at {KEY_PATH} — run with --keygen first")


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip anything machine-specific so a rebuild of the same tree is the
    same bytes apart from timestamps."""
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def build_payload(version: str) -> bytes:
    manifest = json.dumps({
        "product": "marquee",
        "version": version,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(_clean(info), io.BytesIO(manifest))
        tar.add(os.path.join(REPO, "requirements.txt"), "requirements.txt",
                filter=_clean)
        for dirpath, dirnames, filenames in sorted(os.walk(os.path.join(REPO, "marquee"))):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                if name.endswith(EXCLUDE_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                arc = os.path.relpath(full, REPO)
                tar.add(full, arc, filter=_clean)
    return buf.getvalue()


def build():
    version = read_version()
    key = load_key()
    payload = build_payload(version)
    sig = key.sign(payload)

    out_dir = os.path.join(REPO, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"marquee-{version}{device_update.BUNDLE_EXT}")
    with tarfile.open(out, "w") as tar:
        for name, data in (("payload.tar.gz", payload), ("payload.sig", sig)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(_clean(info), io.BytesIO(data))

    # The proof that matters: the exact verifier shipped on devices accepts
    # this file with the key currently embedded in marquee/update.py.
    manifest = device_update.verify_bundle(out)
    assert manifest["version"] == version
    print(f"built and verified {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--keygen", action="store_true",
                   help="create the signing key (one time only)")
    args = p.parse_args()
    keygen() if args.keygen else build()

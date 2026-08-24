#!/usr/bin/env bash
# Cut a Marquee release: build + sign the bundle, tag, and publish on GitHub.
#
#   release/release.sh ["release notes"]
#
# Reads the version from marquee/__init__.py — bump it (and commit) first.
# Needs: the signing key (release/build.py --keygen, done once), and an
# authenticated `gh` CLI. The bundle is signed locally; GitHub only ever
# hosts the finished artifact.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=release/.venv/bin/python
[ -x "$PY" ] || { echo "run: python3 -m venv release/.venv && release/.venv/bin/pip install cryptography" >&2; exit 1; }

v=$("$PY" -c "import re;print(re.search(r'__version__ = \"([^\"]+)\"',open('marquee/__init__.py').read()).group(1))")
notes=${1:-"Marquee $v"}

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "working tree is dirty — commit first so the tag matches the bundle" >&2
  exit 1
fi

"$PY" release/build.py

git tag -a "v$v" -m "Marquee $v"
git push origin "v$v"
gh release create "v$v" "dist/marquee-$v.mqup" --title "Marquee $v" --notes "$notes"
echo "released v$v"

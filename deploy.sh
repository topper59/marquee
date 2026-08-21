#!/usr/bin/env bash
# Push the nowplaying package to the Pi and restart the service.
#   ./deploy.sh          push + restart + tail logs
#   ./deploy.sh --unit   also push the systemd unit (daemon-reload)
#   ./deploy.sh --env    also push /etc/plex-matrix.env
#   ./deploy.sh --deps   also pip install -r requirements.txt into the Pi venv
set -euo pipefail

PI=${PI:-root@192.168.2.129}
SSH="ssh -o ConnectTimeout=25"

push_unit=0; push_env=0; push_deps=0
for a in "$@"; do
  case "$a" in
    --unit) push_unit=1 ;;
    --env)  push_env=1 ;;
    --deps) push_deps=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

echo "→ syntax check"
python3 -m compileall -q nowplaying tests

echo "→ pushing package"
rsync -a --delete --exclude '__pycache__' -e "ssh -o ConnectTimeout=25" \
  nowplaying/ "$PI":/opt/plex-matrix/nowplaying/
rsync -a --exclude '__pycache__' -e "ssh -o ConnectTimeout=25" \
  tests requirements.txt "$PI":/opt/plex-matrix/

if [ "$push_deps" = 1 ]; then
  echo "→ installing deps"
  $SSH "$PI" '/opt/plex-matrix/venv/bin/pip install -q -r /opt/plex-matrix/requirements.txt'
fi

if [ "$push_env" = 1 ]; then
  echo "→ pushing env"
  scp -o ConnectTimeout=25 pi/etc/plex-matrix.env "$PI":/etc/plex-matrix.env
  $SSH "$PI" 'chmod 600 /etc/plex-matrix.env'
fi

if [ "$push_unit" = 1 ]; then
  echo "→ pushing unit"
  scp -o ConnectTimeout=25 pi/etc/plex-matrix.service "$PI":/etc/systemd/system/plex-matrix.service
  $SSH "$PI" 'systemctl daemon-reload'
fi

echo "→ restarting"
$SSH "$PI" 'systemctl restart plex-matrix.service && sleep 2 && systemctl is-active plex-matrix.service'

echo "→ recent logs"
$SSH "$PI" 'journalctl -u plex-matrix.service -n 20 --no-pager'

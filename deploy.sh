#!/usr/bin/env bash
# Push the marquee package to the Pi and restart the service.
#   ./deploy.sh          push + restart + tail logs
#   ./deploy.sh --unit   also push the systemd unit (daemon-reload)
#   ./deploy.sh --deps   also pip install -r requirements.txt into the Pi venv
set -euo pipefail

PI=${PI:-root@192.168.2.129}
SSH="ssh -o ConnectTimeout=25"

push_unit=0; push_deps=0
for a in "$@"; do
  case "$a" in
    --unit) push_unit=1 ;;
    --deps) push_deps=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

echo "→ syntax check"
python3 -m compileall -q marquee tests

echo "→ pushing package"
# /opt/marquee/marquee is a symlink into versions/<v> so the updater can swap
# releases atomically (see marquee/update.py). A dev push is just another
# version, named "dev"; the updater never prunes it.
$SSH "$PI" 'mkdir -p /opt/marquee/versions/dev'
rsync -a --delete --exclude '__pycache__' -e "ssh -o ConnectTimeout=25" \
  marquee/ "$PI":/opt/marquee/versions/dev/marquee/
rsync -a --exclude '__pycache__' -e "ssh -o ConnectTimeout=25" \
  tests requirements.txt "$PI":/opt/marquee/
$SSH "$PI" 'cd /opt/marquee && cp requirements.txt versions/dev/requirements.txt \
  && if [ ! -L marquee ]; then rm -rf marquee; fi \
  && ln -sfn versions/dev/marquee marquee'

if [ "$push_deps" = 1 ]; then
  echo "→ installing deps"
  $SSH "$PI" '/opt/marquee/venv/bin/pip install -q -r /opt/marquee/requirements.txt'
fi

if [ "$push_unit" = 1 ]; then
  echo "→ pushing unit + captive dnsmasq conf + firewall + apt policy"
  scp -o ConnectTimeout=25 pi/etc/marquee.service "$PI":/etc/systemd/system/marquee.service
  $SSH "$PI" 'mkdir -p /etc/NetworkManager/dnsmasq-shared.d'
  scp -o ConnectTimeout=25 pi/etc/captive-dnsmasq.conf "$PI":/etc/NetworkManager/dnsmasq-shared.d/captive.conf
  scp -o ConnectTimeout=25 pi/etc/nftables.conf "$PI":/etc/nftables.conf
  scp -o ConnectTimeout=25 pi/etc/20auto-upgrades "$PI":/etc/apt/apt.conf.d/20auto-upgrades
  scp -o ConnectTimeout=25 pi/etc/52marquee-upgrades "$PI":/etc/apt/apt.conf.d/52marquee-upgrades
  # -c first: a ruleset that does not parse must never reach a device whose
  # only link is the interface it filters.
  $SSH "$PI" 'nft -c -f /etc/nftables.conf \
    && systemctl enable nftables && systemctl reload-or-restart nftables \
    && systemctl daemon-reload'
fi

echo "→ restarting"
$SSH "$PI" 'systemctl restart marquee.service && sleep 2 && systemctl is-active marquee.service'

echo "→ recent logs"
$SSH "$PI" 'journalctl -u marquee.service -n 20 --no-pager'

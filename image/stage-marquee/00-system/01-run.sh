#!/bin/bash -e
# System identity and policy for a Marquee appliance. The config files come
# from pi/etc/ — the workflow copies them into files/ so this stage and a
# dev deploy (`deploy.sh --unit`) can never drift apart.

install -m 644 files/marquee.service "${ROOTFS_DIR}/etc/systemd/system/marquee.service"
install -d "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -m 644 files/captive-dnsmasq.conf \
	"${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/captive.conf"
install -m 644 files/nftables.conf "${ROOTFS_DIR}/etc/nftables.conf"
install -m 644 files/20auto-upgrades "${ROOTFS_DIR}/etc/apt/apt.conf.d/20auto-upgrades"
install -m 644 files/52marquee-upgrades "${ROOTFS_DIR}/etc/apt/apt.conf.d/52marquee-upgrades"

# This runs from an SD card 24/7 — cap the journal instead of grinding it.
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
cat > "${ROOTFS_DIR}/etc/systemd/journald.conf.d/marquee.conf" <<'EOF'
[Journal]
SystemMaxUse=64M
EOF

# The matrix library drives the panel with the PWM hardware the onboard
# audio would otherwise own.
echo "dtparam=audio=off" >> "${ROOTFS_DIR}/boot/firmware/config.txt"
echo "blacklist snd_bcm2835" > "${ROOTFS_DIR}/etc/modprobe.d/marquee-no-audio.conf"

# Reserve core 3 for the matrix refresh thread; must stay consistent with
# CPUAffinity=0 1 2 in marquee.service.
sed -i '1 s/$/ isolcpus=3/' "${ROOTFS_DIR}/boot/firmware/cmdline.txt"

on_chroot <<EOF
systemctl enable NetworkManager
systemctl enable nftables
systemctl enable marquee
systemctl disable ssh || true

# pi-gen insists on creating a login account, and every image built from this
# workflow gets the same one. SSH ships disabled, but the settings page offers
# to turn it on — and the moment someone does, that shared username and
# password are a way into every Marquee ever flashed. Lock the password so the
# account cannot authenticate; the SSH toggle sets a root password the owner
# chooses, which is the only credential this device should ever have.
passwd --lock marquee || true
passwd --lock root || true
EOF

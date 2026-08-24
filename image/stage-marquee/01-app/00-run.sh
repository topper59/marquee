#!/bin/bash -e
# Install the Marquee app in exactly the layout the updater manages:
# /opt/marquee/marquee -> versions/<v>/marquee, so the first over-the-air
# update a customer installs works the same as every later one.

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' files/marquee/__init__.py)
[ -n "${VERSION}" ] || { echo "could not read app version" >&2; exit 1; }

install -d "${ROOTFS_DIR}/opt/marquee/versions/${VERSION}"
cp -r files/marquee "${ROOTFS_DIR}/opt/marquee/versions/${VERSION}/marquee"
install -m 644 files/requirements.txt \
	"${ROOTFS_DIR}/opt/marquee/versions/${VERSION}/requirements.txt"
install -m 644 files/requirements.txt "${ROOTFS_DIR}/opt/marquee/requirements.txt"
ln -sfn "versions/${VERSION}/marquee" "${ROOTFS_DIR}/opt/marquee/marquee"

on_chroot <<EOF
set -e
git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix.git /opt/rpi-rgb-led-matrix
python3 -m venv /opt/marquee/venv
/opt/marquee/venv/bin/pip install --upgrade pip setuptools wheel cython
/opt/marquee/venv/bin/pip install -r /opt/marquee/requirements.txt
cd /opt/rpi-rgb-led-matrix
make build-python PYTHON=/opt/marquee/venv/bin/python
make install-python PYTHON=/opt/marquee/venv/bin/python
EOF

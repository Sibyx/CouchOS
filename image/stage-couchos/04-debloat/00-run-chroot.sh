#!/bin/bash -e
# The Pi 3B+ boots kernel8.img (linux-image-rpi-v8). Drop the Pi 5 kernel and
# all kernel headers that stage0 installs for every board.

pkgs="$(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'linux-image-*2712*' 'linux-headers-*' 2>/dev/null | awk '$1 == "ii" { print $2 }' || true)"
if [ -n "$pkgs" ]; then
	# shellcheck disable=SC2086
	apt-get purge -y $pkgs
fi

apt-get autoremove --purge -y
# Keep /var/lib/apt/lists: pi-gen's export-image step still installs packages.
apt-get clean

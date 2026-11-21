#!/bin/bash
# Runs inside a privileged debian container (see inspect.sh). Mounts the image
# read-only and checks what first-boot SSH access depends on.
set -euo pipefail

img_xz="$(ls -t /deploy/*couchos*.img.xz | head -n1)"
echo "== $img_xz"
xz -dc "$img_xz" >/tmp/couchos.img
loop="$(losetup --show -f -P -r /tmp/couchos.img)"
trap 'umount -R /mnt/root 2>/dev/null || true; losetup -d "$loop"' EXIT
# Docker Desktop's /dev may not get partition nodes automatically.
for n in 1 2; do
	if [ ! -e "${loop}p$n" ]; then
		IFS=: read -r major minor <"/sys/block/$(basename "$loop")/$(basename "$loop")p$n/dev"
		mknod "${loop}p$n" b "$major" "$minor"
	fi
done
mkdir -p /mnt/root
mount -o ro "${loop}p2" /mnt/root
mount -o ro "${loop}p1" /mnt/root/boot/firmware
R=/mnt/root
fail=0
check() { if eval "$2"; then echo "OK   $1"; else echo "FAIL $1"; fail=1; fi; }

key="$R/home/jdubec/.ssh/authorized_keys"
check "authorized_keys present and non-empty" "[ -s '$key' ]"
check "authorized_keys starts with ssh-" "head -c4 '$key' | grep -q '^ssh-'"
check "authorized_keys has no backslashes" "! grep -q '\\\\' '$key'"
check "authorized_keys owned by uid 1000, mode 600" "[ \"\$(stat -c '%u %a' '$key')\" = '1000 600' ]"
check "sshd drop-in installed" "grep -q 'PasswordAuthentication no' '$R/etc/ssh/sshd_config.d/10-couchos.conf'"
check "sshd_config includes drop-ins" "grep -q '^Include /etc/ssh/sshd_config.d/\\*.conf' '$R/etc/ssh/sshd_config'"
# Enable links point at absolute /usr/lib paths: test the link itself (-L), not its target.
check "ssh.service enabled" "[ -L '$R/etc/systemd/system/multi-user.target.wants/ssh.service' ]"
check "no baked-in host keys" "! ls '$R'/etc/ssh/ssh_host_*_key >/dev/null 2>&1"
check "host key regeneration unit enabled" "ls '$R'/etc/systemd/system/*.wants/ 2>/dev/null | grep -q regenerate_ssh_host_keys"
check "cmdline has root PARTUUID" "grep -q 'root=PARTUUID=[0-9a-f]*-02' '$R/boot/firmware/cmdline.txt'"
check "cmdline forces HDMI" "grep -q 'video=HDMI-A-1:1920x1080@60D' '$R/boot/firmware/cmdline.txt'"
check "CouchOS config.txt" "grep -q 'dtoverlay=disable-wifi' '$R/boot/firmware/config.txt'"
check "kernel8.img present" "[ -s '$R/boot/firmware/kernel8.img' ]"
check "no Pi 5 kernel" "[ ! -e '$R/boot/firmware/kernel_2712.img' ]"
check "Pi 3B+ device tree" "[ -s '$R/boot/firmware/bcm2710-rpi-3-b-plus.dtb' ]"
check "hostname beelzebub-tv" "[ \"\$(cat '$R/etc/hostname')\" = beelzebub-tv ]"
check "jdubec password locked" "grep -q '^jdubec:\\*:' '$R/etc/shadow'"
check "root password locked" "grep -q '^root:\\*:' '$R/etc/shadow'"
check "passwordless sudo" "grep -q 'NOPASSWD: ALL' '$R/etc/sudoers.d/010_jdubec-nopasswd'"
check "dbus-user-session installed" "[ -e '$R/usr/lib/systemd/user/dbus.socket' ]"
check "NetworkManager enabled" "[ -L '$R/etc/systemd/system/multi-user.target.wants/NetworkManager.service' ]"
check "avahi enabled" "[ -L '$R/etc/systemd/system/multi-user.target.wants/avahi-daemon.service' ]"

echo "== authorized_keys:"
cut -c1-60 "$key"
echo "== cmdline.txt:"
cat "$R/boot/firmware/cmdline.txt"
echo "== rootfs usage:"
du -sh "$R" --exclude="$R/boot/firmware" 2>/dev/null | tail -n1
exit "$fail"

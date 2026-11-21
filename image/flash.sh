#!/usr/bin/env bash
# Write the newest CouchOS image to an SD card on macOS.
#
#   image/flash.sh disk22        (find the disk with: diskutil list physical)
#
# Erases the whole disk. Refuses the internal system disk and anything larger
# than 256 GB, and asks for the disk name again before writing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DISK="${1:-}"
case "$DISK" in
	disk[0-9]*) ;;
	*) echo "usage: $0 diskN   (see: diskutil list physical)" >&2; exit 2 ;;
esac

IMG="$(ls -t "$HERE"/../out/*couchos*.img.xz 2>/dev/null | head -n1 || true)"
if [ -z "$IMG" ]; then
	echo "No image in out/. Run image/build.sh first." >&2
	exit 1
fi

info="$(diskutil info "/dev/$DISK")"
size_bytes="$(echo "$info" | awk -F'[()]' '/Disk Size/ { split($2, a, " "); print a[1]; exit }')"
# Built-in SD readers report "Internal" but also "Secure Digital" / "Removable".
if echo "$info" | grep -qE "Device Location: +Internal" && ! echo "$info" | grep -qE "Protocol: +Secure Digital|Removable Media: +Removable"; then
	echo "/dev/$DISK looks like an internal, non-removable disk. Refusing." >&2
	exit 1
fi
if [ -z "$size_bytes" ] || [ "$size_bytes" -gt 256000000000 ]; then
	echo "/dev/$DISK is larger than 256 GB (or unknown size). Refusing." >&2
	exit 1
fi

diskutil list "/dev/$DISK"
echo
echo "Image: $IMG"
printf 'This ERASES /dev/%s. Type the disk name again to continue: ' "$DISK"
read -r answer
[ "$answer" = "$DISK" ] || { echo "Aborted."; exit 1; }

diskutil unmountDisk "/dev/$DISK"
xz -dc "$IMG" | sudo dd of="/dev/r$DISK" bs=4m
sync
diskutil eject "/dev/$DISK"
echo "Done. Put the card in the Pi, connect Ethernet + HDMI, and power on."

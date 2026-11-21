#!/bin/bash -e
# Pi 3B+ tuned firmware config. cmdline.txt forces HDMI-A-1 on ("D" suffix)
# so Kodi finds a connector even when the TV is off or unplugged at boot.
# ROOTDEV is replaced with the root PARTUUID by export-image.

install -m 644 files/config.txt "${ROOTFS_DIR}/boot/firmware/config.txt"
install -m 644 files/cmdline.txt "${ROOTFS_DIR}/boot/firmware/cmdline.txt"

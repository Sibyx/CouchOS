#!/bin/bash -e
# Stop dpkg from installing docs from here on, and drop what stage0/1 installed.

install -m 644 files/01-couchos-nodoc "${ROOTFS_DIR}/etc/dpkg/dpkg.cfg.d/01-couchos-nodoc"

find "${ROOTFS_DIR}/usr/share/doc" -depth -type f ! -name copyright -delete
find "${ROOTFS_DIR}/usr/share/doc" -depth -type d -empty -delete
rm -rf "${ROOTFS_DIR}"/usr/share/man/* "${ROOTFS_DIR}"/usr/share/info/* "${ROOTFS_DIR}"/usr/share/lintian/*
find "${ROOTFS_DIR}/usr/share/locale" -mindepth 1 -maxdepth 1 ! -name locale.alias -exec rm -rf {} +

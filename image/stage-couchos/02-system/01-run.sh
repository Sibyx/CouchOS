#!/bin/bash -e
# Admin account, SSH, logging, radios, and services for the CouchOS base image.

# --- admin user: key-only SSH, passwordless sudo, no usable password --------
install -d -m 0700 -o 1000 -g 1000 "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh"
printf '%s\n' "${PUBKEY_SSH_FIRST_USER}" >"${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh/authorized_keys"
chown 1000:1000 "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh/authorized_keys"
chmod 0600 "${ROOTFS_DIR}/home/${FIRST_USER_NAME}/.ssh/authorized_keys"

install -m 0644 files/sshd-couchos.conf "${ROOTFS_DIR}/etc/ssh/sshd_config.d/10-couchos.conf"

printf '%s ALL=(ALL) NOPASSWD: ALL\n' "${FIRST_USER_NAME}" >"${ROOTFS_DIR}/etc/sudoers.d/010_${FIRST_USER_NAME}-nopasswd"
chmod 0440 "${ROOTFS_DIR}/etc/sudoers.d/010_${FIRST_USER_NAME}-nopasswd"

# Host keys are regenerated on first boot (raspberrypi-sys-mods).
rm -f "${ROOTFS_DIR}"/etc/ssh/ssh_host_*_key*

# --- logging ----------------------------------------------------------------
install -d "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
install -m 0644 files/journald-couchos.conf "${ROOTFS_DIR}/etc/systemd/journald.conf.d/10-couchos.conf"

# --- radios: Wi-Fi off (also disabled in config.txt), Bluetooth unblocked ----
mkdir -p "${ROOTFS_DIR}/var/lib/systemd/rfkill/"
for addr in 3f215040.serial soc; do
	echo 0 >"${ROOTFS_DIR}/var/lib/systemd/rfkill/platform-${addr}:bluetooth"
done
mkdir -p "${ROOTFS_DIR}/var/lib/NetworkManager"
printf '[main]\nWirelessEnabled=false\n' >"${ROOTFS_DIR}/var/lib/NetworkManager/NetworkManager.state"

# --- timezone ---------------------------------------------------------------
echo "${TIMEZONE_DEFAULT}" >"${ROOTFS_DIR}/etc/timezone"
rm -f "${ROOTFS_DIR}/etc/localtime"

on_chroot <<-EOF
	dpkg-reconfigure -f noninteractive tzdata

	for GRP in adm sudo video render input audio plugdev netdev bluetooth systemd-journal; do
		if getent group "\$GRP" >/dev/null; then adduser ${FIRST_USER_NAME} "\$GRP"; fi
	done
	usermod --pass='*' ${FIRST_USER_NAME}
	usermod --pass='*' root

	systemctl enable ssh avahi-daemon NetworkManager bluetooth
	systemctl enable rpi-resize || true

	# Ansible owns updates; no surprise apt runs while watching a film.
	systemctl disable apt-daily.timer apt-daily-upgrade.timer || true
	systemctl mask apt-daily.service apt-daily-upgrade.service
EOF

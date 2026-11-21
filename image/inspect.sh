#!/usr/bin/env bash
# Check the newest built image before flashing (SSH keys, boot config, ...).
# The appliance has no console login, so a broken SSH setup means reflashing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
docker run --rm --privileged --platform linux/arm64 \
	-v "$HERE/../out:/deploy:ro" \
	-v "$HERE/inspect-in-container.sh:/inspect.sh:ro" \
	docker.io/library/debian:trixie \
	sh -c 'apt-get update -qq >/dev/null && apt-get install -y -qq xz-utils >/dev/null && bash /inspect.sh'

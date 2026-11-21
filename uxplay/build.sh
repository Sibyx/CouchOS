#!/usr/bin/env bash
# Build UxPlay for the Pi (trixie arm64) in Docker and drop the artefacts in
# uxplay/dist/:
#   uxplay-<version>-trixie-arm64.tar.gz   (usr/local/bin/uxplay, ...)
#   uxplay-<version>-trixie-arm64.DEPENDS  (runtime apt packages)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="${UXPLAY_VERSION:-1.73.7}"
NAME="uxplay-${VERSION}-trixie-arm64"
TAG="couchos-uxplay-build:${VERSION}"

mkdir -p "$HERE/dist"
docker build --platform linux/arm64 --build-arg "UXPLAY_VERSION=${VERSION}" -t "$TAG" "$HERE"

cid="$(docker create --platform linux/arm64 "$TAG")"
trap 'docker rm -f "$cid" >/dev/null' EXIT
docker cp "$cid:/out/DEPENDS" "$HERE/dist/${NAME}.DEPENDS"
docker cp "$cid:/out/usr" - | gzip -9 >"$HERE/dist/${NAME}.tar.gz"

echo "Built:"
ls -lh "$HERE/dist/${NAME}".*

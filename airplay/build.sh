#!/usr/bin/env bash
# Build inputstream.airplay for the Pi (Kodi 22, trixie arm64) in Docker and
# drop the artefacts in airplay/dist/:
#   inputstream.airplay-<ref>-kodi22-arm64.tar.gz   (inputstream.airplay/...)
#   inputstream.airplay-<ref>-kodi22-arm64.DEPENDS  (runtime apt packages)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REF="${AIRPLAY_REF:-c2af2d92905bc45bcc6fba3dcfc473c200dd1618}"
NAME="inputstream.airplay-${REF:0:7}-kodi22-arm64"
TAG="couchos-airplay-build:${REF:0:7}"

mkdir -p "$HERE/dist"
docker build --platform linux/arm64 --build-arg "AIRPLAY_REF=${REF}" -t "$TAG" "$HERE"

cid="$(docker create --platform linux/arm64 "$TAG")"
trap 'docker rm -f "$cid" >/dev/null' EXIT
docker cp "$cid:/out/DEPENDS" "$HERE/dist/${NAME}.DEPENDS"
docker cp "$cid:/out/inputstream.airplay" - | gzip -9 >"$HERE/dist/${NAME}.tar.gz"

echo "Built:"
ls -lh "$HERE/dist/${NAME}".*

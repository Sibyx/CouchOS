#!/usr/bin/env bash
# Build dashd for the Pi (trixie arm64) in Docker; native on Apple Silicon.
#   dashd/dist/dashd-<version>-trixie-arm64
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$HERE/Cargo.toml" | head -n1)"
NAME="dashd-${VERSION}-trixie-arm64"
TAG="couchos-dashd-build:${VERSION}"

mkdir -p "$HERE/dist"
docker build --platform linux/arm64 -t "$TAG" "$HERE"

cid="$(docker create --platform linux/arm64 "$TAG")"
trap 'docker rm -f "$cid" >/dev/null' EXIT
docker cp "$cid:/out/dashd" "$HERE/dist/${NAME}"

echo "Built:"
ls -lh "$HERE/dist/${NAME}"

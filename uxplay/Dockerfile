# Builds UxPlay for Debian trixie arm64 (Raspberry Pi OS Lite trixie) natively
# on an Apple Silicon Mac. Debian trixie ships 1.71.1, which is affected by
# CVE-2025-60458 (double free in RTSP handling), so we build upstream.
FROM --platform=linux/arm64 docker.io/library/debian:trixie

ARG UXPLAY_VERSION=1.73.7

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git ca-certificates dpkg-dev \
      libssl-dev libplist-dev libavahi-compat-libdnssd-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "v${UXPLAY_VERSION}" https://github.com/FDH2/UxPlay.git /src

WORKDIR /src/build
RUN cmake .. -DNO_X11_DEPS=ON -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
 && make -j"$(nproc)" \
 && make install DESTDIR=/out \
 && strip /out/usr/local/bin/uxplay \
 && rm -rf /out/usr/local/share/doc /out/usr/local/share/man

# Runtime packages for the libraries the binary links against.
RUN ldd /out/usr/local/bin/uxplay \
      | awk '/=> \// { print $3 }' \
      | xargs -n1 readlink -f \
      | xargs dpkg -S 2>/dev/null \
      | cut -d: -f1 | sort -u > /out/DEPENDS \
 && cat /out/DEPENDS

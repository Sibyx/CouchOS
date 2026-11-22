# Builds popcornmix's inputstream.airplay (Kodi-native AirPlay receiver:
# mirroring, app video and audio through Kodi's own player) for the Pi's
# Kodi 22 (Raspberry Pi archive kodi22, trixie arm64), natively on an Apple
# Silicon Mac. Output: the add-on directory as Kodi loads it, under /out.
FROM --platform=linux/arm64 docker.io/library/debian:trixie

ARG AIRPLAY_REF=c2af2d92905bc45bcc6fba3dcfc473c200dd1618

# The Pi's own archive: kodi22-addons-dev must match the kodi22 on the Pi.
COPY raspberrypi-archive-keyring.pgp /usr/share/keyrings/raspberrypi-archive-keyring.pgp
RUN printf '%s\n' 'Types: deb' 'URIs: http://archive.raspberrypi.com/debian/' 'Suites: trixie' \
      'Components: main' 'Signed-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp' \
      > /etc/apt/sources.list.d/raspi.sources

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config git ca-certificates curl \
      libssl-dev libplist-dev libavahi-compat-libdnssd-dev kodi22-addons-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/popcornmix/inputstream.airplay.git /src \
 && git -C /src checkout --quiet "${AIRPLAY_REF}"

# CouchOS fixes on top of the pin (patches/, each says why; send upstream):
#  0001 long AirPlay video URLs (Plex) were cut off and silently dropped
#  0002 play HLS with ffmpeg when inputstream.adaptive is not installed
COPY patches/ /patches/
RUN for p in /patches/*.patch; do git -C /src apply --verbose "$p"; done

# Publish through Avahi (UxPlay's lib/dns_sd, its -DUSE_DNS_SD backend)
# instead of UxPlay's built-in responder (lib/mdnsd). The built-in one
# announces <hostname>.local itself; beside avahi-daemon that is a host name
# conflict and Avahi renames the Pi to beelzebub-tv-2.local (seen 2026-11-21).
# Both backends implement lib/dnssd.h, which is all the receiver uses.
RUN grep -q 'foreach(_part llhttp playfair mdnsd)' /src/CMakeLists.txt \
 && sed -i 's/foreach(_part llhttp playfair mdnsd)/foreach(_part llhttp playfair dns_sd)/' /src/CMakeLists.txt \
 && sed -i 's/^project(inputstream.airplay)$/&\nadd_definitions(-DSUPPRESS_AVAHI_COMPAT_WARNING)/' /src/CMakeLists.txt \
 && grep -n 'dns_sd\|SUPPRESS_AVAHI' /src/CMakeLists.txt

# UxPlay's protocol library, at the commit and checksum the add-on pins, with
# the add-on's carried patches. Unpacked here so the build itself stays offline.
RUN set -e; \
    rev=$(sed -n 's/.*UXPLAY_VERSION "\(.*\)".*/\1/p' /src/CMakeLists.txt); \
    sum=$(sed -n 's/.*UXPLAY_SHA256 "\(.*\)".*/\1/p' /src/CMakeLists.txt); \
    curl -fsSL -o /tmp/uxplay.tar.gz "https://github.com/FDH2/UxPlay/archive/${rev}.tar.gz"; \
    echo "${sum}  /tmp/uxplay.tar.gz" | sha256sum -c -; \
    mkdir /uxplay; tar -xzf /tmp/uxplay.tar.gz --strip-components=1 -C /uxplay; \
    for p in /src/depends/common/uxplay/*.patch; do \
      [ -e "$p" ] && patch -d /uxplay -p1 --forward -i "$p"; \
    done; true

# Where kodi22-addons-dev keeps KodiConfig.cmake (printed for the build log).
RUN dpkg -L kodi22-addons-dev | grep -iE 'kodi-?config\.cmake' | tee /tmp/kodiconfig

WORKDIR /src/build
RUN cmake .. -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_PREFIX_PATH="$(dirname "$(head -n1 /tmp/kodiconfig)")" \
      -DUXPLAY_SOURCE_DIR=/uxplay \
 && make -j"$(nproc)" \
 && make install DESTDIR=/staging

# Kodi's cmake forces the /usr prefix and splits the add-on in two
# (lib/.../kodi/addons: .so + bin/; share/kodi/addons: addon.xml, resources).
# Merge them into one directory, the layout of a user-installed binary add-on
# (~/.kodi/addons/inputstream.airplay).
RUN set -e; out=/out/inputstream.airplay; mkdir -p "$out"; \
    cp -a /staging/usr/lib/*/kodi/addons/inputstream.airplay/. "$out"/; \
    cp -a /staging/usr/share/kodi/addons/inputstream.airplay/. "$out"/; \
    strip "$out"/bin/airplay-receiver "$out"/inputstream.airplay.so.*.*; \
    test -x "$out"/bin/airplay-receiver; \
    find "$out" | sort

# Runtime packages for the libraries the add-on and the daemon link against.
RUN ldd /out/inputstream.airplay/bin/airplay-receiver /out/inputstream.airplay/*.so* \
      | awk '/=> \// { print $3 }' | sort -u \
      | xargs -n1 readlink -f \
      | xargs dpkg -S 2>/dev/null \
      | cut -d: -f1 | sort -u > /out/DEPENDS \
 && cat /out/DEPENDS

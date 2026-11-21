# CouchOS

A minimal media appliance for a **Raspberry Pi 3B+** driving a Samsung TV over HDMI:

- **Plex** on the TV: Kodi 21 (GBM, hardware H.264) + [PM4K](https://github.com/pannal/plex-for-kodi), opened automatically at boot
- **AirPlay** from iPhone/Mac: [UxPlay](https://github.com/FDH2/UxPlay) 1.73.7. The TV wakes up and switches input when you start mirroring
- **TV remote** works via HDMI-CEC; phone control via web UI, the Kodi app, or the JSON API
- **Bluetooth speaker** (e.g. JBL) via PipeWire
- **1 TB HDD** at `/srv/media` for local photos, music and videos

## Architecture

```
            ┌──────────────── Raspberry Pi 3B+ (Raspberry Pi OS Lite trixie, arm64) ───────────────┐
 iPhone ───►│ uxplay ─┐                        systemd --user of "tv" (linger)                  │
 AirPlay    │         │ -dacp file             ┌──────────────┐                                 │
            │         └──────────────────────► │   couchd     │◄── :8765 JSON API (LAN, no auth) │
 Browser ──►│ couch-web (podman, Caddy) :80 ──►│ arbiter, CEC │                                  │
            │                                  │ BT, volume   │── systemctl --user stop/start ─┐ │
 Plex srv ─►│ kodi (GBM) + PM4K ◄──────────────┴──────────────┘   JSON-RPC :8080               │ │
            │   │ DRM/KMS, V4L2 decode, /dev/cec0                                              │ │
            │   └────────────── HDMI ──► Samsung TV          PipeWire ──► HDMI / Bluetooth     │ │
            └──────────────────────────────────────────────────────────────────────────────────┘
```

- Kodi and UxPlay both need the DRM display. **couchd** watches UxPlay's `-dacp` file, which exists only while an AirPlay client is connected. On connect it stops Kodi and wakes the TV over CEC. When the client leaves (after a 3 s grace period) it starts Kodi again.
- Kodi, UxPlay, PipeWire and couchd run natively as **systemd user services** of the `tv` user (they need the GPU, CEC, V4L2 and audio). **Podman** (rootless quadlet) runs the web UI and is the place for future add-ons.
- Avahi advertises `_http._tcp` (web UI), `_couchos._tcp` (API), Kodi (`_xbmc-jsonrpc`), and AirPlay (`_airplay`, `_raop`).

## Layout

| Path | What |
|---|---|
| `image/` | pi-gen config and the `stage-couchos` stage (replaces stock stage2). `build.sh`, `flash.sh` |
| `uxplay/` | Docker build of UxPlay for trixie arm64 → `uxplay/dist/` |
| `ansible/` | Everything on top of the base image (roles: base, tv_user, storage, audio, bluetooth, kodi, uxplay, couchd, web) |
| `couchd/` | Control daemon (Python stdlib only) |
| `web/` | Web UI (single static page) |

## 1. Build the base image (on the Mac)

Needs Docker (arm64, native on Apple Silicon).

```sh
image/build.sh            # ~4 min → out/image_<date>-couchos.img.xz (pi-gen is cloned to ~/.cache/couchos)
uxplay/build.sh           # ~2 min → uxplay/dist/uxplay-1.73.7-trixie-arm64.{tar.gz,DEPENDS}
```

The image is pi-gen stage0 and stage1 plus `stage-couchos`, which:
- installs only: ssh, sudo, NetworkManager, avahi, bluez, podman, python3, v4l-utils, rpi-swap (zram), raspberrypi-sys-mods
- creates admin `jdubec` with keys from `https://github.com/Sibyx.keys`: key-only SSH, passwordless sudo, password locked
- turns Wi-Fi off (`dtoverlay=disable-wifi`), leaves Bluetooth on, and turns the 3.5 mm jack off
- tunes `config.txt` for media (CMA 256 MB, gpu_mem 128 for the H.264 decoder) and forces HDMI on (`video=HDMI-A-1:1920x1080@60D`)
- removes the Pi 5 kernel, kernel headers, docs, man pages and locales, and disables apt timers. Ansible owns updates

## 2. Check and flash

```sh
image/inspect.sh          # mounts the image read-only: SSH key, sshd, cmdline, hostname, ...
diskutil list physical    # the built-in reader shows up as "internal, physical"
image/flash.sh disk22     # asks you to type the disk name again, then sudo dd
```

## 3. Configure with Ansible

Boot the Pi with Ethernet and HDMI connected. A fresh image announces itself as `couchos.local` (images built after this change: `beelzebub-tv.local`).

Home naming convention: flat mDNS names `beelzebub-<device>.local`. Single-label `.local` names resolve on iOS, macOS and Linux, and Bonjour/AirPlay services stay in the default `local.` domain. The inventory name is the hostname, so this Pi is `beelzebub-tv`.

```sh
cd ansible
ansible-playbook site.yml -e ansible_host=192.168.1.68   # first run: renames couchos -> beelzebub-tv
ansible-playbook site.yml                                  # afterwards: via beelzebub-tv.local
```

HDD (first time only, **erases the disk**):

```sh
ssh jdubec@beelzebub-tv.local ls -l /dev/disk/by-id/ | grep usb
# set storage_device in group_vars/all.yml, then:
ansible-playbook site.yml -e storage_format=true -e storage_format_confirm=/dev/disk/by-id/usb-...
```

The Pi 3B+ can supply only 1.2 A across all USB ports. A 2.5" HDD can draw close to that at spin-up, so use a **powered** USB hub or an HDD with its own PSU.

## 4. Use it

- **TV:** switch to the Pi's HDMI input and Plex is on screen. Sign in once (PM4K shows a plex.tv/link code).
- **AirPlay:** iPhone → Screen Mirroring → *CouchOS*.
- **Phone:** open `http://beelzebub-tv.local/` in Safari, then Share → *Add to Home Screen*. Or use the official Kodi remote app: it finds Kodi over Bonjour, with no password on the LAN.
- **JBL:** put the speaker in pairing mode → web UI → *Scan for speakers* → *Pair*.

### API (no auth, trusted LAN)

```
GET  /api/status                      mode, now playing, AirPlay, temp/mem
POST /api/mode        {"mode": "kodi|airplay|idle"}
POST /api/tv          {"power": "on|standby"}
POST /api/kodi/input  {"action": "up|down|left|right|select|back|home|info|menu|playpause|stop|forward|rewind"}
POST /api/kodi/text   {"text": "...", "done": true}   types into the TV's on-screen keyboard
POST /api/kodi/restart
GET  /api/audio       POST /api/audio {"volume": 0-150} | {"delta": ±n} | {"mute": bool}
GET  /api/bluetooth   POST /api/bluetooth/scan
POST /api/bluetooth/{pair|connect|disconnect|remove}  {"mac": "AA:BB:..."}
POST /api/system/{reboot|poweroff}
```

## Limits of the hardware

- **No HEVC/H.265 hardware decoding on the Pi 3.** Software 1080p x265 stutters. Let the Plex server transcode those files (PM4K → Settings → set a max bitrate/quality so HEVC is converted to H.264), or keep H.264 copies.
- 1080p H.264 at up to ~40 Mbit/s plays well. For 50/60 fps judder, uncomment `core_freq_min=500` in `/boot/firmware/config.txt`.
- 1 GB RAM: Kodi's cache is capped at ~120 MB and artwork resolution is reduced (`advancedsettings.xml`).

## Verify on the device (first boot checklist)

These parts were designed from the documentation and need confirming on real hardware. The first two are the riskiest:

1. **AirPlay hand-off trigger.** While *screen mirroring*, `ls -l /home/tv/.uxplay.dacp` must show the file (UxPlay documents it for any connected client, but DACP is an audio remote-control protocol). If it doesn't appear, the arbiter never fires. Fallback: trigger on UxPlay's journal lines instead.
2. **kmssink after Kodi stops.** After the hand-off, `sudo journalctl _SYSTEMD_USER_UNIT=uxplay.service` must show no DRM permission or atomic-commit errors, and the iPhone screen must appear. If UxPlay opened the DRM device while Kodi was master, it may never get master. Fix: restart `uxplay.service` as part of the hand-off.
3. `sudo journalctl _SYSTEMD_USER_UNIT=kodi.service` (and `sudo systemctl --user -M tv@ status kodi`) shows GBM on `/dev/dri/card*`, and H.264 playback uses `drm_prime`/V4L2 (Kodi debug overlay: `o` key / player info).
4. **Kodi CEC on exit:** Kodi → Settings → System → Input → Peripherals → CEC → *Devices to power off when Kodi exits* = **None**. Otherwise the TV briefly goes to standby when AirPlay takes over (couchd wakes it again).
5. **UxPlay idle:** with Kodi running, start UxPlay and check Kodi keeps the display. If UxPlay grabs DRM at startup, move `After=kodi.service` to a start-on-demand design.
6. **AirPlay picture:** `kmssink` scaling and full screen on the Samsung. Adjust `uxplay_args` (e.g. `-vs "kmssink can-scale=true"`) if the image is not full screen.
7. `curl http://beelzebub-tv.local/api/status` and the web UI respond; `avahi-browse -art` lists the services.
8. Bluetooth: pair the JBL, then `wpctl status` shows it as the default sink.

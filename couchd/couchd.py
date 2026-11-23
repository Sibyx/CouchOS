#!/usr/bin/env python3
"""couchd - CouchOS control daemon.

Runs as a systemd --user service of the media user, next to Kodi.

* AirPlay inside Kodi: Kodi (and Plex in it) never stops. Two receivers
  (COUCHD_AIRPLAY_RECEIVER):
  - kodi: inputstream.airplay plays the stream in Kodi by itself. couchd
    reports the session (a sender connected on the AirPlay port, and what
    Kodi plays), wakes the TV when one starts, and ends it on request.
  - uxplay: UxPlay only receives and forwards RTP; airplay-bridge muxes it
    into MPEG-TS on tcp://127.0.0.1:5010. UxPlay writes its -dacp file only
    while a client is connected: couchd then makes Kodi play the stream and
    wakes the TV, and stops it when the client leaves.
* HDMI-CEC: through Kodi while it runs (it owns /dev/cec0), else cec-ctl.
* Bluetooth speakers (bluetoothctl) and volume (wpctl).
* Plex catalog for the phone (plexcatalog.py: an index of the libraries on the
  Pi, kept current in the background, and an artwork cache), browsed with
  PM4K's own sign-in (the token stays here); "play this on the TV" is handed
  to PM4K over JSONRPC.NotifyAll.
* JSON API on the LAN (trusted network, no auth) for the web UI and future
  phone apps.

Standard library only.
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from plexcatalog import Catalog, PlexClient, PlexError

VERSION = "0.3.0"

BIND = os.environ.get("COUCHD_BIND", "0.0.0.0")
PORT = int(os.environ.get("COUCHD_PORT", "8765"))
KODI_RPC = os.environ.get("COUCHD_KODI_RPC", "http://127.0.0.1:8080/jsonrpc")
DACP_FILE = Path(os.environ.get("COUCHD_DACP_FILE", str(Path.home() / ".uxplay.dacp")))
CEC_DEVICE = os.environ.get("COUCHD_CEC_DEVICE", "/dev/cec0")
OSD_NAME = os.environ.get("COUCHD_OSD_NAME", "CouchOS")
# Seconds the AirPlay client must be gone before the stream is stopped (reconnects).
RESTORE_DELAY = float(os.environ.get("COUCHD_RESTORE_DELAY", "2"))
# What Kodi plays during AirPlay (airplay-bridge output). The ".ts" suffix makes
# Kodi use its video player; udp:// gets Kodi's low-latency realtime handling.
AIRPLAY_URL = os.environ.get("COUCHD_AIRPLAY_URL", "tcp://127.0.0.1:5010/airplay.ts")
# Kodi's window id for full-screen video, and how long to keep pushing it to the
# front after an AirPlay client connects (Kodi's stream probe takes a while).
FULLSCREEN_VIDEO = 12005
FRONT_TIMEOUT = float(os.environ.get("COUCHD_FRONT_TIMEOUT", "30"))
# Which AirPlay receiver runs: "kodi" (inputstream.airplay) or "uxplay".
AIRPLAY_RECEIVER = os.environ.get("COUCHD_AIRPLAY_RECEIVER", "kodi")
# The AirPlay control port: a sender holds a connection to it for the session.
AIRPLAY_PORT = int(os.environ.get("COUCHD_AIRPLAY_PORT", "7000"))
# inputstream.airplay's daemon (it restarts it by itself when it exits).
AIRPLAY_DAEMON = b"/airplay-receiver"
PM4K_ID = "script.plexmod"

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
KODI_INPUTS = {
    "up": ("Input.Up", {}),
    "down": ("Input.Down", {}),
    "left": ("Input.Left", {}),
    "right": ("Input.Right", {}),
    "select": ("Input.Select", {}),
    "back": ("Input.Back", {}),
    "home": ("Input.Home", {}),
    "info": ("Input.Info", {}),
    "menu": ("Input.ContextMenu", {}),
    "playpause": ("Input.ExecuteAction", {"action": "playpause"}),
    "stop": ("Input.ExecuteAction", {"action": "stop"}),
    "forward": ("Input.ExecuteAction", {"action": "stepforward"}),
    "rewind": ("Input.ExecuteAction", {"action": "stepback"}),
}

log = logging.getLogger("couchd")


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class Raw:
    """A non-JSON response body (artwork)."""

    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type


# --- helpers -------------------------------------------------------------------


def run(*argv: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def unit_active(unit: str) -> bool:
    return run("systemctl", "--user", "is-active", "--quiet", unit).returncode == 0


def unit_action(action: str, unit: str) -> None:
    result = run("systemctl", "--user", action, unit, timeout=60)
    if result.returncode != 0:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"systemctl {action} {unit}: {result.stderr.strip()}")


def kodi_rpc(method: str, params: dict | None = None, timeout: float = 5) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(KODI_RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        reply = json.loads(resp.read())
    if "error" in reply:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"Kodi {method}: {reply['error']}")
    return reply.get("result")


HOST_LABEL = "Window(10000).Property(couchos.host)"
ACK_LABEL = "Window(10000).Property(couchos.ack)"
ACK_WAIT = 0.6  # seconds service.couchos has to confirm a command
_command_ids = itertools.count(int(time.time()))


def kodi_command(command: str, timeout: float = 5) -> None:
    """Run one of service.couchos's commands (couchos_commands.py) in Kodi.

    service.couchos is resident: a notification runs the command at once,
    and it confirms by putting the command's id on Kodi's home window.
    NotifyAll answers OK whether or not anyone handled it, so without that
    confirmation (the service isn't up, or its callbacks wait behind a dialog
    in the catalog) this starts the add-on instead (Addons.ExecuteAddon): a
    Python interpreter in Kodi first, seconds on a Pi 3, but it runs. The
    commands are idempotent, so a late confirmation running it twice is
    harmless."""
    labels = kodi_rpc("XBMC.GetInfoLabels", {"labels": [HOST_LABEL]}, timeout=timeout) or {}
    if labels.get(HOST_LABEL):
        ack = str(next(_command_ids))
        kodi_rpc("JSONRPC.NotifyAll", {"sender": "couchos", "message": "command",
                                        "data": {"command": command, "id": ack}}, timeout=timeout)
        deadline = time.monotonic() + ACK_WAIT
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if (kodi_rpc("XBMC.GetInfoLabels", {"labels": [ACK_LABEL]}, timeout=timeout) or {}).get(ACK_LABEL) == ack:
                return
        log.warning("service.couchos did not confirm %s; starting it as a script", command)
    kodi_rpc("Addons.ExecuteAddon", {"addonid": "service.couchos", "params": [command]}, timeout=timeout)


def read_first_line(path: str) -> str | None:
    try:
        return Path(path).read_text().split("\n", 1)[0].strip()
    except OSError:
        return None


# --- HDMI-CEC ------------------------------------------------------------------


class Cec:
    """Wake/standby the TV. Kodi owns the CEC adapter while it runs."""

    def tv(self, power: str) -> str:
        if power not in ("on", "standby"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "power must be 'on' or 'standby'")
        if unit_active("kodi.service"):
            try:
                command = "cec_on" if power == "on" else "cec_standby"
                kodi_command(command)
                return "kodi"
            except (OSError, ApiError) as exc:
                log.warning("CEC via Kodi failed (%s), trying cec-ctl", exc)
        self._cec_ctl(power)
        return "cec-ctl"

    def _cec_ctl(self, power: str) -> None:
        base = ("cec-ctl", "-d", CEC_DEVICE)
        # Claim a playback logical address (Kodi releases it when it stops).
        run(*base, "--playback", "--osd-name", OSD_NAME)
        if power == "on":
            run(*base, "--to", "0", "--image-view-on")
            phys = self._physical_address(base)
            if phys:
                run(*base, "--to", "15", "--active-source", f"phys-addr={phys}")
        else:
            run(*base, "--to", "0", "--standby")

    @staticmethod
    def _physical_address(base: tuple[str, ...]) -> str | None:
        out = run(*base).stdout
        match = re.search(r"Physical Address\s*:\s*([0-9a-f.]+)", out)
        return match.group(1) if match else None


# --- AirPlay -------------------------------------------------------------------


_LOOPBACK = {"0100007F", "00000000000000000000000001000000", "0000000000000000FFFF00000100007F"}


def airplay_senders(port: int = AIRPLAY_PORT) -> list[str]:
    """Remote ends of established TCP connections to the AirPlay port (hex
    addresses from /proc/net/tcp*). A sender keeps one open for its session."""
    peers = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
                continue
            local, remote = fields[1], fields[2]
            if int(local.rsplit(":", 1)[1], 16) != port:
                continue
            address = remote.rsplit(":", 1)[0]
            if address not in _LOOPBACK:
                peers.append(address)
    return peers


def airplay_daemon_pids() -> list[int]:
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            continue
        if argv0.endswith(AIRPLAY_DAEMON):
            pids.append(int(proc.name))
    return pids


def _is_plex_url(path: str) -> bool:
    return any(mark in path for mark in ("X-Plex-Token", "/library/parts/", "/video/:/transcode", "plex.direct"))


class AirPlay:
    """An iPhone/Mac showing something on the TV, inside Kodi.

    There are no modes: Plex is always the TV's home, AirPlay is a temporary
    full-screen video (or music) on top of it.

    receiver "kodi" (inputstream.airplay): the add-on opens and stops the
    stream itself. Mirroring and music play as airplay://...; video from an
    app plays the playlist the sender hands over (an ordinary http URL), so
    that counts as AirPlay while a sender is connected and it isn't Plex's.
    Ending a session: stop the player and end the add-on's daemon, which
    drops the sender's connection; the add-on starts a fresh daemon at once.

    receiver "uxplay": couchd opens the bridge's stream while UxPlay's -dacp
    file exists, and ends a session by restarting UxPlay.
    """

    def __init__(self, cec: Cec, receiver: str = AIRPLAY_RECEIVER):
        self.cec = cec
        self.receiver = receiver
        self.lock = threading.Lock()
        self.on_change: Callable[[], None] = lambda: None
        self.on_restart: Callable[[str, str], None] = lambda unit, reason: None
        self.active = False
        self.since: float | None = None
        self.kind: str | None = None  # screen, video, audio
        self.sender: str | None = None
        self._gone_since: float | None = None
        self._saved_refresh: int | None = None

    def start(self) -> None:
        threading.Thread(target=self._watch, name="airplay", daemon=True).start()

    def _watch(self) -> None:
        while True:
            try:
                self._tick() if self.receiver == "uxplay" else self._tick_kodi()
            except Exception:  # keep watching whatever happens
                log.exception("airplay tick failed")
            time.sleep(0.25 if self.receiver == "uxplay" else 1.0)

    @property
    def running(self) -> bool:
        if self.receiver == "uxplay":
            return unit_active("uxplay.service")
        return bool(airplay_daemon_pids())

    def status(self) -> dict:
        return {"receiver": self.receiver, "running": self.running, "active": self.active,
                "since": self.since, "kind": self.kind}

    # -- receiver "kodi"

    def _now_showing(self) -> tuple[str | None, int | None]:
        """(kind, player id) of the AirPlay item Kodi plays, if any."""
        senders = airplay_senders()
        for player in kodi_rpc("Player.GetActivePlayers", timeout=2) or []:
            item = kodi_rpc("Player.GetItem", {"playerid": player["playerid"], "properties": ["file"]}, timeout=2)
            path = (item or {}).get("item", {}).get("file", "")
            audio = player.get("type") == "audio"
            if path.startswith("airplay://"):
                return ("audio" if audio else "screen"), player["playerid"]
            if path.startswith(AIRPLAY_URL.split("/airplay")[0]):
                return "screen", player["playerid"]
            if senders and path.startswith("http") and not _is_plex_url(path):
                return ("audio" if audio else "video"), player["playerid"]
        return None, None

    def on_screen(self) -> bool:
        """Checked now, not from the last tick: the Play button asks."""
        if self.receiver == "uxplay":
            return self.active
        return self._now_showing()[0] is not None

    def _tick_kodi(self) -> None:
        senders = airplay_senders()
        # Kodi is only asked while a sender is connected: idle costs nothing.
        kind = self._now_showing()[0] if senders else None
        with self.lock:
            if kind and not self.active:
                self.active, self.kind, self.since = True, kind, time.time()
                self.sender = senders[0] if senders else None
                self._gone_since = None
                log.info("AirPlay session started (%s)", kind)
                self.on_change()
                try:
                    self.cec.tv("on")
                except (OSError, ApiError, subprocess.TimeoutExpired) as exc:
                    log.warning("CEC wake failed: %s", exc)
            elif kind and kind != self.kind:
                self.kind = kind
                self._gone_since = None
                self.on_change()
            elif kind:
                self._gone_since = None
            elif self.active:
                now = time.monotonic()
                if self._gone_since is None:
                    self._gone_since = now
                elif now - self._gone_since >= RESTORE_DELAY:
                    log.info("AirPlay session ended")
                    self.active, self.kind, self.since, self.sender = False, None, None, None
                    self._gone_since = None
                    self.on_change()

    def _disconnect_kodi(self) -> None:
        pids = airplay_daemon_pids()
        if not pids:
            raise ApiError(HTTPStatus.CONFLICT, "the AirPlay receiver is not running (is inputstream.airplay enabled?)")
        # What plays now, while the sender is still connected (afterwards an
        # app's video no longer looks like AirPlay).
        try:
            kind, player = self._now_showing()
        except (OSError, ApiError):
            kind, player = None, None
        # The daemon first: that drops the sender and ends mirroring and music.
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        log.info("AirPlay: ended the session (receiver %s restarts)", pids)
        # Video from an app plays the sender's playlist, which outlives the
        # daemon: stop it too. Not while a modal dialog is up -- the add-on
        # documents a deadlock for a stop landing then (inputstream.adaptive's
        # DRM failure dialog); it holds its own stops back for the same reason.
        if player is not None:
            try:
                time.sleep(1)
                still = any(p["playerid"] == player for p in kodi_rpc("Player.GetActivePlayers", timeout=2) or [])
                modal = kodi_rpc("XBMC.GetInfoBooleans", {"booleans": ["System.HasActiveModalDialog"]}, timeout=2) or {}
                if still and not modal.get("System.HasActiveModalDialog"):
                    kodi_rpc("Player.Stop", {"playerid": player})
                elif still:
                    log.warning("AirPlay %s still playing behind a dialog; not stopping it", kind)
            except (OSError, ApiError) as exc:
                log.warning("could not stop the AirPlay item: %s", exc)
        with self.lock:
            self.active, self.kind, self.since, self.sender = False, None, None, None
            self._gone_since = None
        self.on_change()

    # -- receiver "uxplay"

    def _tick(self) -> None:
        connected = DACP_FILE.exists()
        with self.lock:
            if connected and not self.active:
                self.active = True
                self.kind = "screen"
                self.since = time.time()
                self._gone_since = None
                log.info("AirPlay client connected")
                self.on_change()  # tell clients at once, before Kodi work
                self._begin()
            elif not connected and self.active:
                now = time.monotonic()
                if self._gone_since is None:
                    self._gone_since = now
                elif now - self._gone_since >= RESTORE_DELAY:
                    log.info("AirPlay client left")
                    self._end()
            elif connected:
                self._gone_since = None

    def _begin(self) -> None:
        # Open the stream before video arrives, so Kodi catches the first
        # keyframe (iOS sends very few). Keep the TV's mode fixed: a refresh
        # rate switch blanks the screen for seconds.
        try:
            self._saved_refresh = kodi_rpc("Settings.GetSettingValue", {"setting": "videoplayer.adjustrefreshrate"})["value"]
            kodi_rpc("Settings.SetSettingValue", {"setting": "videoplayer.adjustrefreshrate", "value": 0})
        except (OSError, ApiError, KeyError, TypeError) as exc:
            log.warning("could not pin refresh rate: %s", exc)
        try:
            kodi_rpc("Player.Open", {"item": {"file": AIRPLAY_URL}})
        except (OSError, ApiError) as exc:
            log.error("could not open AirPlay stream in Kodi: %s", exc)
        threading.Thread(target=self._bring_video_to_front, name="airplay-front", daemon=True).start()
        try:
            self.cec.tv("on")
        except (OSError, ApiError, subprocess.TimeoutExpired) as exc:
            log.warning("CEC wake failed: %s", exc)

    def _bring_video_to_front(self) -> None:
        """Keep the AirPlay video on screen while Kodi opens it.

        Player.Open does not switch to the video while a modal dialog is up
        (PM4K's busy spinner after a restart, or its exit prompt): the stream
        plays underneath and the TV shows Plex.
        """
        deadline = time.monotonic() + FRONT_TIMEOUT
        while self.active and time.monotonic() < deadline:
            try:
                window = kodi_rpc("GUI.GetProperties", {"properties": ["currentwindow"]}, timeout=2)
                if window["currentwindow"]["id"] == FULLSCREEN_VIDEO and self._playing_airplay():
                    return
                kodi_command("show_video", timeout=3)
            except (OSError, ApiError, KeyError, TypeError) as exc:
                log.debug("bring video to front: %s", exc)
            time.sleep(1)
        if self.active:
            log.warning("AirPlay video did not come to the front within %.0f s", FRONT_TIMEOUT)

    def _end(self) -> None:
        self.active = False
        self.kind = None
        self.since = None
        self._gone_since = None
        self.on_change()
        try:
            if self._playing_airplay():
                kodi_rpc("Player.Stop", {"playerid": 1})
            if self._saved_refresh is not None:
                kodi_rpc("Settings.SetSettingValue", {"setting": "videoplayer.adjustrefreshrate", "value": self._saved_refresh})
        except (OSError, ApiError) as exc:
            log.warning("could not stop AirPlay stream: %s", exc)
        self._saved_refresh = None

    @staticmethod
    def _playing_airplay() -> bool:
        item = kodi_rpc("Player.GetItem", {"playerid": 1, "properties": ["file"]}, timeout=3)
        return (item or {}).get("item", {}).get("file", "").startswith(AIRPLAY_URL.split("/airplay")[0])

    def disconnect(self) -> None:
        """End the session: the phone stops sharing and Plex is back."""
        if self.receiver != "uxplay":
            self._disconnect_kodi()
            return
        # Restarting UxPlay drops the session.
        self.on_restart("uxplay.service", "AirPlay stopped from the web page or app")
        unit_action("restart", "uxplay.service")
        with self.lock:
            if self.active:
                self._end()


# --- Bluetooth -------------------------------------------------------------------


class Bluetooth:
    def __init__(self) -> None:
        self.scanning = False

    @staticmethod
    def _ctl(*args: str, timeout: float = 20) -> str:
        result = run("bluetoothctl", *args, timeout=timeout)
        return result.stdout + result.stderr

    def devices(self) -> list[dict]:
        devices = []
        for line in self._ctl("devices").splitlines():
            match = re.match(r"Device ([0-9A-F:]{17}) (.*)", line.strip())
            if not match:
                continue
            mac, name = match.groups()
            info = self._ctl("info", mac)
            devices.append(
                {
                    "mac": mac,
                    "name": name,
                    "paired": "Paired: yes" in info,
                    "trusted": "Trusted: yes" in info,
                    "connected": "Connected: yes" in info,
                    "audio": "Audio Sink" in info,
                }
            )
        return devices

    def scan(self, seconds: int = 15) -> None:
        if self.scanning:
            return

        def worker() -> None:
            self.scanning = True
            try:
                self._ctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 10)
            finally:
                self.scanning = False

        threading.Thread(target=worker, name="bt-scan", daemon=True).start()

    def action(self, action: str, mac: str) -> str:
        mac = mac.upper()
        if not MAC_RE.match(mac):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid MAC address")
        if action == "pair":
            out = self._ctl("--agent", "NoInputNoOutput", "pair", mac, timeout=40)
            out += self._ctl("trust", mac)
            out += self._ctl("connect", mac, timeout=30)
        elif action in ("connect", "disconnect", "remove"):
            out = self._ctl(action, mac, timeout=30)
        else:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown bluetooth action {action}")
        return out.strip()


# --- audio ---------------------------------------------------------------------


def audio_status() -> dict:
    out = run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@").stdout
    match = re.search(r"Volume:\s*([\d.]+)", out)
    sink = run("wpctl", "inspect", "@DEFAULT_AUDIO_SINK@").stdout
    name = re.search(r'node\.description = "([^"]+)"', sink)
    return {
        "volume": round(float(match.group(1)) * 100) if match else None,
        "muted": "[MUTED]" in out,
        "sink": name.group(1) if name else None,
    }


def audio_set(body: dict) -> dict:
    if "volume" in body:
        volume = max(0, min(150, int(body["volume"])))
        run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{volume}%")
    if "delta" in body:
        delta = max(-50, min(50, int(body["delta"])))
        run("wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", f"{abs(delta)}%{'+' if delta >= 0 else '-'}")
    if "mute" in body:
        run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if body["mute"] else "0")
    return audio_status()


# --- system ----------------------------------------------------------------------


def read_meminfo() -> dict[str, int]:
    """/proc/meminfo in kB."""
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = int(value.split()[0])
    return meminfo


def system_status() -> dict:
    meminfo = read_meminfo()
    temp = read_first_line("/sys/class/thermal/thermal_zone0/temp")
    throttled = run("vcgencmd", "get_throttled").stdout.strip().partition("=")[2] or None
    return {
        "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])),
        "mem_total_mb": meminfo.get("MemTotal", 0) // 1024,
        "mem_available_mb": meminfo.get("MemAvailable", 0) // 1024,
        "temp_c": round(int(temp) / 1000, 1) if temp else None,
        "throttled": throttled,
        "load": os.getloadavg()[0],
    }


def save_json(path: Path, data: object) -> None:
    """Write atomically, so a power cut never leaves half a file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        tmp.replace(path)
    except OSError as exc:
        log.warning("could not save %s: %s", path, exc)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def find_hwmon(name: str) -> Path | None:
    for d in Path("/sys/class/hwmon").glob("hwmon*"):
        if read_first_line(str(d / "name")) == name:
            return d
    return None


# Interfaces that are not the box's own link to the LAN.
VIRTUAL_NET = re.compile(r"^(lo|veth|docker|podman|cni|br-|virbr|tap)")

# Per-minute power flags.
UNDER_VOLTAGE = 1
THROTTLED = 2


class Metrics:
    """24 h of CPU, memory, temperature, disk, network and power, per minute.

    Kept in memory and written to disk every few minutes (and on stop), so a
    couchd restart keeps the history without wearing the SD card. statvfs
    reads the in-memory superblock: it never spins the media HDD back up.

    Under-voltage dips are often shorter than a minute, so the rpi_volt alarm
    (a sysfs file, no subprocess) is polled every POLL seconds in between.
    """

    INTERVAL = 60
    POLL = 2
    KEEP = 24 * 3600
    SAVE_EVERY = 900
    # [t, cpu %, mem MB, temp °C, {mount: [used GB, total GB]}, rx Mbit/s, tx Mbit/s, power flags]
    FIELDS = 8

    def __init__(self, path: Path, disks: list[tuple[str, str]]):
        self.path = path
        self.disks = disks  # (mount point, label)
        self.lock = threading.Lock()
        self.samples: collections.deque = collections.deque(maxlen=self.KEEP // self.INTERVAL + 5)
        self._cpu_prev = self._cpu_times()
        self._net_prev = (time.monotonic(), self._net_bytes())
        volt = find_hwmon("rpi_volt")
        self._volt_alarm = volt / "in0_lcrit_alarm" if volt else None
        self._uv_seen = False
        self._dirty = False
        # Known up front: history loaded from disk is served before the first
        # sample, and the page needs the total to scale those memory values.
        self.mem_total_mb: int | None = read_meminfo().get("MemTotal", 0) // 1024 or None

    @staticmethod
    def _net_bytes() -> tuple[int, int]:
        rx = tx = 0
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, _, data = line.partition(":")
            if VIRTUAL_NET.match(name.strip()):
                continue
            fields = data.split()
            rx += int(fields[0])
            tx += int(fields[8])
        return rx, tx

    def _net_rates(self) -> tuple[float | None, float | None]:
        now, (rx, tx) = time.monotonic(), self._net_bytes()
        (then, (rx0, tx0)), self._net_prev = self._net_prev, (now, (rx, tx))
        dt = now - then
        if dt <= 0 or rx < rx0 or tx < tx0:  # counters reset (interface went down)
            return None, None
        return round((rx - rx0) * 8 / dt / 1e6, 3), round((tx - tx0) * 8 / dt / 1e6, 3)

    def poll_power(self) -> None:
        if self._volt_alarm and read_first_line(str(self._volt_alarm)) == "1":
            self._uv_seen = True

    @staticmethod
    def _cpu_times() -> tuple[int, int]:
        fields = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        return idle, sum(fields[:8])  # guest time is already in user/nice

    def _disk_use(self) -> dict[str, list[float]]:
        use = {}
        for mount, _label in self.disks:
            if not os.path.ismount(mount):
                continue
            st = os.statvfs(mount)
            total = st.f_blocks * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            use[mount] = [round(used / 1e9, 2), round(total / 1e9, 2)]
        return use

    def sample(self) -> None:
        idle, total = self._cpu_times()
        d_idle, d_total = idle - self._cpu_prev[0], total - self._cpu_prev[1]
        self._cpu_prev = (idle, total)
        cpu = round(100 * (1 - d_idle / d_total), 1) if d_total > 0 else None
        rx, tx = self._net_rates()
        mem = system_status()
        temp = mem["temp_c"]
        used_mb = mem["mem_total_mb"] - mem["mem_available_mb"]
        # vcgencmd get_throttled: bit 0 under-voltage, 1 frequency capped,
        # 2 throttled, 3 soft temperature limit (all "right now").
        self.poll_power()
        try:
            bits = int(mem["throttled"] or "0", 16)
        except ValueError:
            bits = 0
        power = (UNDER_VOLTAGE if self._uv_seen or bits & 0x1 else 0) | (THROTTLED if bits & 0xE else 0)
        self._uv_seen = False
        with self.lock:
            self.mem_total_mb = mem["mem_total_mb"]
            self.samples.append([int(time.time()), cpu, used_mb, temp, self._disk_use(), rx, tx, power])
            self._dirty = True

    def snapshot(self) -> dict:
        cutoff = time.time() - self.KEEP
        with self.lock:
            rows = [r for r in self.samples if r[0] >= cutoff]
        last = rows[-1][4] if rows else {}
        return {
            "interval": self.INTERVAL,
            "now": int(time.time()),
            "mem_total_mb": self.mem_total_mb,
            "t": [r[0] for r in rows],
            "cpu": [r[1] for r in rows],
            "mem_mb": [r[2] for r in rows],
            "temp_c": [r[3] for r in rows],
            "net_rx_mbps": [r[5] for r in rows],
            "net_tx_mbps": [r[6] for r in rows],
            "power": [r[7] for r in rows],
            "power_monitor": self._volt_alarm is not None,
            "disks": [
                {
                    "path": mount,
                    "label": label,
                    "used_gb": last[mount][0] if mount in last else None,
                    "total_gb": last[mount][1] if mount in last else None,
                    "used_gb_series": [r[4][mount][0] if mount in r[4] else None for r in rows],
                }
                for mount, label in self.disks
                if any(mount in r[4] for r in rows)
            ],
        }

    def load(self) -> None:
        rows = load_json(self.path)
        if not isinstance(rows, list):
            return
        cutoff = time.time() - self.KEEP
        with self.lock:
            for r in rows:
                if isinstance(r, list) and len(r) >= 5 and r[0] >= cutoff:
                    self.samples.append(r + [None] * (self.FIELDS - len(r)))  # older, shorter rows

    def save(self) -> None:
        with self.lock:
            if not self._dirty:
                return
            data = list(self.samples)
            self._dirty = False
        save_json(self.path, data)

    def start(self, every_minute: Callable[[], None] = lambda: None) -> None:
        self.load()
        threading.Thread(target=self._watch, args=(every_minute,), name="metrics", daemon=True).start()

    def _watch(self, every_minute: Callable[[], None]) -> None:
        last_save = next_sample = time.monotonic()
        next_sample += self.INTERVAL
        while True:
            time.sleep(self.POLL)
            try:
                self.poll_power()
                if time.monotonic() < next_sample:
                    continue
                next_sample = max(next_sample + self.INTERVAL, time.monotonic() + self.POLL)
                self.sample()
                every_minute()
                if time.monotonic() - last_save >= self.SAVE_EVERY:
                    self.save()
                    last_save = time.monotonic()
            except Exception:
                log.exception("metrics sample failed")


def metrics_disks() -> list[tuple[str, str]]:
    """COUCHD_DISKS="/=System,/srv/media=Media": mount points to chart."""
    spec = os.environ.get("COUCHD_DISKS", "/=System,/srv/media=Media")
    disks = []
    for item in spec.split(","):
        mount, _, label = item.strip().partition("=")
        if mount:
            disks.append((mount, label or mount))
    return disks


class Services:
    """State of the services CouchOS is made of, and 24 h of their restarts.

    systemd only knows the current run, so couchd compares each unit's start
    time once a minute and logs what changed. The last seen start times are
    saved with the log, so a couchd restart (or a reboot) doesn't lose them.
    """

    # (scope, unit, label). Units that aren't installed are left out.
    UNITS = [
        ("user", "kodi.service", "Kodi"),
        ("user", "uxplay.service", "AirPlay receiver"),
        ("user", "airplay-bridge.service", "AirPlay bridge"),
        ("user", "couchd.service", "Control daemon"),
        ("user", "couch-web.service", "Web page"),
        ("user", "wireplumber.service", "Audio"),
        ("system", "bluetooth.service", "Bluetooth"),
        ("system", "avahi-daemon.service", "Bonjour"),
    ]
    PROPS = "Id,LoadState,ActiveState,SubState,NRestarts,Result,ActiveEnterTimestampMonotonic"
    KEEP = 24 * 3600
    SETTLE = 180  # starts this soon after boot are part of the boot

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self._seen: dict[str, dict] = {}  # unit -> {"start": wall time, "restarts": n}
        self._boot: float | None = None
        self._reasons: dict[str, tuple[float, str]] = {}
        self._cache: tuple[float, list[dict]] | None = None
        self._dirty = False

    @staticmethod
    def boot_time() -> float:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])

    def _show(self, scope: str, units: list[str]) -> list[dict]:
        argv = ["systemctl", "show", "-p", self.PROPS, *units]
        if scope == "user":
            argv.insert(1, "--user")
        out = run(*argv).stdout
        rows = []
        for block in out.strip().split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if props:
                rows.append(props)
        return rows

    def query(self) -> list[dict]:
        """Current state of every installed unit (cached for a few seconds)."""
        with self.lock:
            if self._cache and time.monotonic() - self._cache[0] < 3:
                return self._cache[1]
        labels = {unit: (scope, label) for scope, unit, label in self.UNITS}
        now_wall, now_mono = time.time(), time.monotonic()
        result = []
        for scope in ("user", "system"):
            units = [u for s, u, _ in self.UNITS if s == scope]
            for p in self._show(scope, units):
                if p.get("LoadState") != "loaded" or p.get("Id") not in labels:
                    continue
                started_us = int(p.get("ActiveEnterTimestampMonotonic") or 0)
                result.append({
                    "unit": p["Id"],
                    "label": labels[p["Id"]][1],
                    "scope": scope,
                    "active": p.get("ActiveState"),
                    "sub": p.get("SubState"),
                    "result": p.get("Result"),
                    "restarts": int(p.get("NRestarts") or 0),
                    "since": round(now_wall - (now_mono - started_us / 1e6)) if started_us else None,
                })
        order = [u for _, u, _ in self.UNITS]
        result.sort(key=lambda r: order.index(r["unit"]))
        with self.lock:
            self._cache = (time.monotonic(), result)
        return result

    def expect(self, unit: str, reason: str) -> None:
        """couchd is about to restart a unit: say why in the log."""
        with self.lock:
            self._reasons[unit] = (time.time(), reason)

    def _event(self, t: float, kind: str, unit: str | None, label: str, detail: str = "") -> None:
        self.events.append({"t": round(t), "kind": kind, "unit": unit, "label": label, "detail": detail})
        self._dirty = True

    def check(self) -> None:
        """Compare with the last check and log restarts, crashes and boots."""
        units = self.query()
        boot = self.boot_time()
        with self.lock:
            if self._boot is not None and abs(boot - self._boot) > 60:
                self._event(boot, "boot", None, "Box started")
            self._boot = boot
            for u in units:
                prev = self._seen.get(u["unit"])
                state = {"start": u["since"], "restarts": u["restarts"], "active": u["active"]}
                self._seen[u["unit"]] = state
                if prev is None:
                    self._dirty = True
                    continue
                if u["active"] == "failed" and prev.get("active") != "failed":
                    self._event(time.time(), "failed", u["unit"], u["label"], u["result"] or "")
                if not u["since"] or u["since"] == prev.get("start") or u["since"] - boot < self.SETTLE:
                    continue
                if u["restarts"] > prev.get("restarts", 0):  # NRestarts counts only Restart= restarts
                    self._event(u["since"], "crash", u["unit"], u["label"], "crashed, systemd restarted it")
                    continue
                reason = self._reasons.pop(u["unit"], None)
                detail = reason[1] if reason and abs(reason[0] - u["since"]) < 120 else ""
                self._event(u["since"], "restart", u["unit"], u["label"], detail)
            cutoff = time.time() - self.KEEP
            self.events = sorted((e for e in self.events if e["t"] >= cutoff), key=lambda e: e["t"])

    def snapshot(self) -> dict:
        units = self.query()
        with self.lock:
            return {"units": units, "events": list(self.events)}

    def load(self) -> None:
        data = load_json(self.path)
        if not isinstance(data, dict):
            return
        cutoff = time.time() - self.KEEP
        with self.lock:
            self.events = [e for e in data.get("events", []) if isinstance(e, dict) and e.get("t", 0) >= cutoff]
            self._seen = data.get("seen") or {}
            self._boot = data.get("boot")

    def save(self) -> None:
        with self.lock:
            if not self._dirty:
                return
            data = {"events": self.events, "seen": self._seen, "boot": self._boot}
            self._dirty = False
        save_json(self.path, data)

    def start(self) -> None:
        self.load()
        try:
            self.check()  # logs a reboot or restarts that happened while couchd was down
        except Exception:
            log.exception("service check failed")


# --- Plex on the TV ------------------------------------------------------------------
# Browsing (the catalog index on the Pi, artwork) is plexcatalog.py.


class PlexOnTV:
    """Hand Plex items to PM4K, which plays them on the TV. Transcoding,
    resume and watched state stay PM4K's."""

    def __init__(self, airplay: AirPlay, catalog: Catalog | None):
        self.airplay = airplay
        self.catalog = catalog

    def play(self, key: str, play: bool = True, from_start: bool = False, replace: bool = False) -> dict:
        """Hand the item to PM4K on the TV (its home window opens it, see
        ansible/roles/kodi/files/pm4k-patches).

        Refuses while AirPlay is on screen unless `replace`: PM4K's playback
        would stop the stream and inputstream.airplay then stops the sender.
        If PM4K isn't running it is started first; that takes several seconds,
        so the open is sent from a background thread and the reply says
        `starting`.
        """
        if not key.isdigit():
            raise ApiError(HTTPStatus.BAD_REQUEST, "key must be a number")
        # Continue Watching changes as soon as it plays.
        if self.catalog:
            self.catalog.invalidate_hubs()
        try:
            if not replace and self.airplay.on_screen():
                raise ApiError(HTTPStatus.CONFLICT, "The TV is showing AirPlay. Stop it first, then play again.")
            if self._pm4k_running():
                self._notify_open(key, play, from_start)
                return {"ok": True, "starting": False}
        except OSError as exc:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Kodi is not reachable: {exc}") from exc
        threading.Thread(target=self._start_then_open, args=(key, play, from_start), name="plex-open", daemon=True).start()
        return {"ok": True, "starting": True}

    def _start_then_open(self, key: str, play: bool, from_start: bool) -> None:
        try:
            log.info("Plex: starting PM4K before opening %s", key)
            kodi_rpc("Addons.ExecuteAddon", {"addonid": PM4K_ID})
            deadline = time.monotonic() + 30
            while not self._pm4k_running():
                if time.monotonic() > deadline:
                    log.warning("Plex: PM4K did not start within 30 s; %s not opened", key)
                    return
                time.sleep(1)
            # "running" is set before the home window exists and hooks the
            # open signal; a notification before that is dropped by PM4K.
            time.sleep(3)
            self._notify_open(key, play, from_start)
        except (OSError, ApiError) as exc:
            log.warning("Plex: could not open %s: %s", key, exc)

    @staticmethod
    def _wake_gui() -> None:
        """Close the screensaver (the dashboard) before PM4K opens an item.

        The screensaver is a modal: while it is up Kodi refuses PM4K's player
        window ("active modal dialogs") and the item never plays. Any
        JSON-RPC input wakes the screensaver and is swallowed by it.
        """
        flags = ["System.ScreenSaverActive", "System.HasActiveModalDialog"]
        state = kodi_rpc("XBMC.GetInfoBooleans", {"booleans": flags}, timeout=2) or {}
        if not state.get("System.ScreenSaverActive"):
            return
        kodi_rpc("Input.ExecuteAction", {"action": "noop"}, timeout=2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            time.sleep(0.2)
            state = kodi_rpc("XBMC.GetInfoBooleans", {"booleans": flags}, timeout=2) or {}
            if not any(state.get(flag) for flag in flags):
                return
        log.warning("Plex: the screensaver did not close; opening anyway")

    @classmethod
    def _notify_open(cls, key: str, play: bool, from_start: bool) -> None:
        cls._wake_gui()
        kodi_rpc("JSONRPC.NotifyAll", {
            "sender": "couchos", "message": "open",
            "data": {"key": key, "play": bool(play), "from_start": bool(from_start)},
        })

    @staticmethod
    def _pm4k_running() -> bool:
        label = "Window(10000).Property(script.plex.running)"
        values = kodi_rpc("XBMC.GetInfoLabels", {"labels": [label]}, timeout=3) or {}
        return bool(values.get(label))


class Dashboard:
    """The TV dashboard (dashd's data, drawn by screensaver.couchos.dashboard).

    It is Kodi's screensaver, so Kodi shows it whenever the TV is idle, never
    over a video, and the first key returns to Plex. On/off is which
    screensaver Kodi uses; "show" also wakes the TV and starts it right away.
    """

    ADDON = "screensaver.couchos.dashboard"
    OFF = "screensaver.xbmc.builtin.black"

    def status(self) -> dict:
        mode = kodi_rpc("Settings.GetSettingValue", {"setting": "screensaver.mode"}, timeout=3)["value"]
        return {"enabled": mode == self.ADDON, "screensaver": mode}

    def set(self, enabled: bool | None, show: bool) -> dict:
        if show:
            # Only video: audio (PM4K's theme music while browsing) may play on.
            players = kodi_rpc("Player.GetActivePlayers", timeout=2) or []
            if any(p.get("type") == "video" for p in players):
                raise ApiError(HTTPStatus.CONFLICT, "A video is playing on the TV. Stop it first.")
            enabled = True
        if enabled is not None:
            kodi_rpc("Settings.SetSettingValue", {"setting": "screensaver.mode", "value": self.ADDON if enabled else self.OFF})
        if show:
            # Wakes the TV over CEC, waits, then starts the screensaver (see
            # service.couchos). Asynchronous: returns at once.
            kodi_command("dashboard")
        return self.status()


def now_playing() -> dict | None:
    try:
        players = kodi_rpc("Player.GetActivePlayers", timeout=2)
        if not players:
            return None
        player = players[0]
        item = kodi_rpc(
            "Player.GetItem",
            {"playerid": player["playerid"], "properties": ["title", "showtitle", "season", "episode", "artist"]},
            timeout=2,
        )["item"]
        props = kodi_rpc(
            "Player.GetProperties",
            {"playerid": player["playerid"], "properties": ["speed", "percentage"]},
            timeout=2,
        )
        return {
            "type": player.get("type"),
            "title": item.get("title") or item.get("label"),
            "show": item.get("showtitle") or None,
            "season": item.get("season") if item.get("season", -1) >= 0 else None,
            "episode": item.get("episode") if item.get("episode", -1) >= 0 else None,
            "artist": ", ".join(item.get("artist") or []) or None,
            "paused": props.get("speed") == 0,
            "progress": round(props.get("percentage", 0), 1),
        }
    except (OSError, ApiError, KeyError, TypeError):
        return None


# --- HTTP API --------------------------------------------------------------------


class DisplayWatchdog:
    """Restart Kodi if it runs without holding the display.

    Kodi claims DRM master only once, at startup. If anything else had the
    display open at that moment, Kodi keeps running but draws nothing and the
    TV shows the kernel console (seen 2026-11-21). Kodi can't recover alone.
    """

    INTERVAL = 15

    def __init__(self, services: Services):
        self.services = services

    def start(self) -> None:
        threading.Thread(target=self._watch, name="display", daemon=True).start()

    def _watch(self) -> None:
        misses = 0
        while True:
            time.sleep(self.INTERVAL)
            try:
                if not unit_active("kodi.service"):
                    misses = 0
                    continue
                misses = 0 if self.kodi_is_master() is not False else misses + 1
                if misses >= 2:
                    log.warning("Kodi is running without the display; restarting it")
                    self.services.expect("kodi.service", "display watchdog: Kodi had no picture")
                    unit_action("restart", "kodi.service")
                    misses = 0
            except Exception:
                log.exception("display watchdog failed")

    @staticmethod
    def kodi_is_master() -> bool | None:
        out = run("sudo", "-n", "/usr/bin/cat", "/sys/kernel/debug/dri/0/clients")
        if out.returncode != 0:
            return None  # can't tell; never restart on missing information
        for line in out.stdout.splitlines():
            fields = line.split()
            # command tgid dev master a uid ...
            if len(fields) >= 4 and fields[0] == "kodi.bin" and fields[2] == "0":
                return fields[3] == "y"
        return None


class TvSchedule:
    """Wake the TV, or put it to standby, when the dashboard's time of day
    changes (dashd's `mode` widget, the one clock: see dashd/README.md).

    COUCHD_WAKE_MODES (e.g. "morning"): entering one of these modes wakes
    the TV over CEC and shows the dashboard (only CEC when the dashboard is
    turned off: a scheduled wake never turns it back on; nothing when a
    video plays). COUCHD_STANDBY_MODES: entering one puts the TV to standby,
    unless something plays. Only a change seen while running counts: a
    restart of couchd or dashd in the middle of the morning wakes nothing,
    and nothing runs late for a time the Pi was off.
    """

    INTERVAL = 60  # mode changes are minute-exact at best; a minute late is fine

    def __init__(self, cec: Cec, dashboard: Dashboard):
        self.cec = cec
        self.dashboard = dashboard
        self.wake_on = self._modes("COUCHD_WAKE_MODES")
        self.standby_on = self._modes("COUCHD_STANDBY_MODES")
        self.url = os.environ.get("COUCHD_DASHD_URL", "http://127.0.0.1:8766").rstrip("/") + "/v1/widgets/mode"
        self.mode: str | None = None
        self.last: dict | None = None  # the last action, for GET /api/tv/schedule

    @staticmethod
    def _modes(name: str) -> list[str]:
        return [m.strip() for m in os.environ.get(name, "").split(",") if m.strip()]

    def start(self) -> None:
        if self.wake_on or self.standby_on:
            log.info("TV schedule: wake on %s, standby on %s", self.wake_on or "-", self.standby_on or "-")
            threading.Thread(target=self._watch, name="tv-schedule", daemon=True).start()

    def status(self) -> dict:
        return {"wake_on": self.wake_on, "standby_on": self.standby_on, "mode": self.mode, "last": self.last}

    def _current(self) -> str | None:
        try:
            with urllib.request.urlopen(self.url, timeout=5) as resp:
                return json.loads(resp.read())["data"]["mode"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.debug("TV schedule: no mode from dashd (%s)", exc)
            return None

    def _watch(self) -> None:
        while True:
            try:
                mode, before = self._current(), self.mode
                if mode is not None:
                    self.mode = mode  # first: a failed action is not retried every minute
                    if before is not None and mode != before:
                        self._entered(mode)
            except Exception:
                log.exception("TV schedule failed")
            time.sleep(self.INTERVAL)

    def _entered(self, mode: str) -> None:
        if mode in self.wake_on:
            self._act("wake", mode, self._wake)
        elif mode in self.standby_on:
            self._act("standby", mode, self._standby)

    def _act(self, action: str, mode: str, do: Callable[[], str]) -> None:
        try:
            result = do()
        except ApiError as exc:
            result = f"skipped: {exc}"
        except Exception as exc:  # Kodi down (OSError), an odd answer: log, keep watching
            result = f"failed: {exc!r}"
        log.info("TV schedule: %s on entering %s: %s", action, mode, result)
        self.last = {"action": action, "mode": mode, "at": int(time.time()), "result": result}

    def _wake(self) -> str:
        if not self.dashboard.status()["enabled"]:
            return f"TV on via {self.cec.tv('on')} (dashboard is off)"
        self.dashboard.set(None, show=True)  # 409 while a video plays
        return "dashboard shown"

    def _standby(self) -> str:
        if now_playing() is not None:
            return "skipped: something is playing"
        return f"standby via {self.cec.tv('standby')}"


def addresses() -> list[str]:
    out = run("ip", "-4", "-o", "addr", "show", "scope", "global").stdout
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)


class EventBus:
    """Fan-out of status snapshots to Server-Sent Events subscribers."""

    MAX_SUBSCRIBERS = 16

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            if len(self._subscribers) >= self.MAX_SUBSCRIBERS:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "too many event subscribers")
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:  # slow client: drop rather than block couchd
                pass


class App:
    # Seconds between change checks while someone listens; heartbeat interval.
    CHECK_INTERVAL = 3.0
    HEARTBEAT = 15.0

    def __init__(self) -> None:
        self.cec = Cec()
        self.airplay = AirPlay(self.cec)
        self.bt = Bluetooth()
        # The catalog must not take the rest down (CEC, AirPlay, Bluetooth):
        # if its database can't open (no FTS5, disk full), only Plex browsing
        # is off, and says why.
        self.catalog: Catalog | None = None
        self.catalog_error: str | None = None
        try:
            self.catalog = Catalog(PlexClient(), on_change=self.changed)
        except Exception as exc:
            log.exception("Plex catalog unavailable")
            self.catalog_error = f"Plex catalog unavailable: {exc}"
        self.plex = PlexOnTV(self.airplay, self.catalog)
        self.dashboard = Dashboard()
        self.schedule = TvSchedule(self.cec, self.dashboard)
        self.events = EventBus()
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.metrics = Metrics(state / "couchd" / "metrics.json", metrics_disks())
        self.services = Services(state / "couchd" / "events.json")
        self._status: tuple[float, dict] | None = None
        self._status_lock = threading.Lock()
        self._changed = threading.Event()
        self.airplay.on_change = self.changed
        self.airplay.on_restart = self.services.expect

    def changed(self) -> None:
        """Something happened: drop the cached status and push a fresh one."""
        with self._status_lock:
            self._status = None
        self._changed.set()

    @staticmethod
    def _signature(status: dict) -> tuple:
        # What clients display; uptime, load, progress and memory are left out
        # so they don't trigger a push every few seconds (the heartbeat has them).
        playing = status["kodi"]["playing"] or {}
        catalog = status["catalog"]
        done, total = catalog.get("done") or 0, catalog.get("total") or 0
        return (
            status["kodi"]["running"],
            playing.get("title"), playing.get("show"), playing.get("paused"),
            status["airplay"]["running"], status["airplay"]["active"], status["airplay"]["kind"],
            status["system"].get("throttled"),
            # A sync's progress in 5 % steps.
            catalog["state"], catalog.get("label"), (20 * done // total) if total else None,
        )

    def publish_loop(self) -> None:
        last_sig: tuple | None = None
        last_push = 0.0
        while True:
            forced = self._changed.wait(self.CHECK_INTERVAL)
            self._changed.clear()
            if not self.events.count:
                last_sig = None
                continue
            try:
                status = self.status()
            except Exception:
                log.exception("status for events failed")
                continue
            sig = self._signature(status)
            now = time.monotonic()
            if forced or sig != last_sig or now - last_push >= self.HEARTBEAT:
                self.events.publish(status)
                last_sig, last_push = sig, now

    def start(self) -> None:
        threading.Thread(target=self.publish_loop, name="events", daemon=True).start()

    def status(self) -> dict:
        # Each status runs a few subprocesses; several clients polling must
        # not multiply that on a Pi 3.
        with self._status_lock:
            if self._status and time.monotonic() - self._status[0] < 1.5:
                return self._status[1]
            kodi = unit_active("kodi.service")
            status = {
                "version": VERSION,
                "kodi": {"running": kodi, "playing": now_playing() if kodi else None},
                "airplay": self.airplay.status(),
                "catalog": self.catalog.summary() if self.catalog
                else {"state": "error", "label": self.catalog_error, "done": None, "total": None},
                "system": system_status(),
            }
            self._status = (time.monotonic(), status)
            return status

    def info(self) -> dict:
        return {
            "version": VERSION,
            "hostname": os.uname().nodename,
            "addresses": addresses(),
            "airplay_name": OSD_NAME,
            "app_url": os.environ.get("COUCHD_APP_URL", ""),
            "plex_url": os.environ.get("COUCHD_PLEX_URL", ""),
            "ports": {"api": PORT, "kodi_jsonrpc_tcp": 9090, "kodi_http": 8080},
        }

    def route(self, method: str, path: str, body: dict, query: dict[str, str] | None = None) -> object:
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        query = query or {}
        catalog = self.catalog
        if catalog is None and parts[:1] in (["plex"], ["catalog"]) and parts != ["plex", "play"]:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, self.catalog_error or "Plex catalog unavailable")
        match (method, parts):
            # -- Plex catalog (served from the index on the Pi)
            case ("GET", ["plex"]):
                return {**catalog.client.server(), "catalog": catalog.summary()}
            case ("GET", ["plex", "hubs"]):
                return {"hubs": catalog.hubs()}
            case ("GET", ["plex", "sections"]):
                return {"sections": catalog.sections()}
            case ("GET", ["plex", "sections", section]):
                return catalog.section(
                    section,
                    start=int(query.get("start", 0)),
                    size=int(query.get("size", 60)),
                    sort=query.get("sort", "title"),
                    unwatched=query.get("unwatched") in ("1", "true"),
                )
            case ("GET", ["plex", "sections", section, "letters"]):
                return catalog.letters(section, unwatched=query.get("unwatched") in ("1", "true"))
            case ("GET", ["plex", "search"]):
                return catalog.search(query.get("q", ""), query.get("type"), limit=int(query.get("limit", 30)))
            case ("GET", ["plex", "items", key]):
                return catalog.item(key, fresh=query.get("fresh") in ("1", "true"))
            case ("GET", ["plex", "art"]):
                data = catalog.art(query.get("path", ""), int(query.get("w", 300)), int(query.get("h", 450)))
                return Raw(data, "image/jpeg")
            # -- the index and the artwork cache
            case ("GET", ["catalog"]):
                return catalog.status()
            case ("POST", ["catalog", "sync"]):
                section = str(body["section"]) if body.get("section") else None
                kind = "full" if body.get("full") else "quick"
                return {"queued": catalog.enqueue(kind, section)}
            case ("POST", ["catalog", "sections", key]):
                return catalog.set_section(
                    key,
                    enabled=bool(body["enabled"]) if "enabled" in body else None,
                    prefetch_art=bool(body["prefetch_art"]) if "prefetch_art" in body else None,
                )
            case ("POST", ["catalog", ("items" | "sections") as scope, key, "art"]):
                action = str(body.get("action", ""))
                if action not in ("fetch", "drop") or not key.isdigit():
                    raise ApiError(HTTPStatus.BAD_REQUEST, 'action must be "fetch" or "drop", key a number')
                suffix = "_section" if scope == "sections" else ""
                return {"queued": catalog.enqueue(f"art_{action}{suffix}", key)}
            case ("POST", ["catalog", "items", key, "refresh"]):
                if not key.isdigit():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "key must be a number")
                return {"queued": catalog.enqueue("refresh", key)}
            case ("POST", ["catalog", "art", "clear"]):
                return {"queued": catalog.enqueue("art_clear")}
            case ("POST", ["catalog", "reset"]):
                return catalog.reset()
            # -- Plex on the TV
            case ("POST", ["plex", "play"]):
                result = self.plex.play(str(body.get("key", "")), bool(body.get("play", True)),
                                        bool(body.get("from_start", False)), bool(body.get("replace", False)))
                self.changed()
                return result
            # -- the TV dashboard (Kodi's screensaver; data from dashd)
            case ("GET" | "POST", ["dashboard"]):
                try:
                    if method == "GET":
                        return self.dashboard.status()
                    enabled = bool(body["enabled"]) if "enabled" in body else None
                    return self.dashboard.set(enabled, bool(body.get("show", False)))
                except OSError as exc:
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Kodi is not reachable: {exc}") from exc
            case ("GET", ["health"]):
                return {"ok": True}
            case ("GET", ["status"]):
                return self.status()
            case ("GET", ["info"]):
                return self.info()
            case ("GET", ["metrics"]):
                return self.metrics.snapshot()
            case ("GET", ["services"]):
                return self.services.snapshot()
            case ("POST", ["airplay", "stop"]):
                self.airplay.disconnect()
                self.changed()
                return {"ok": True}
            case ("POST", ["tv"]):
                return {"via": self.cec.tv(str(body.get("power", "")))}
            case ("GET", ["tv", "schedule"]):
                return self.schedule.status()
            case ("POST", ["kodi", "input"]):
                action = str(body.get("action", ""))
                if action not in KODI_INPUTS:
                    raise ApiError(HTTPStatus.BAD_REQUEST, f"action must be one of {sorted(KODI_INPUTS)}")
                rpc_method, params = KODI_INPUTS[action]
                try:
                    kodi_rpc(rpc_method, params)
                except OSError as exc:
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Kodi is not reachable: {exc}") from exc
                return {"ok": True}
            case ("POST", ["kodi", "text"]):
                # Fills the on-screen keyboard (search, server address, ...).
                text = str(body.get("text", ""))[:500]
                try:
                    kodi_rpc("Input.SendText", {"text": text, "done": bool(body.get("done", True))})
                except OSError as exc:
                    raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Kodi is not reachable: {exc}") from exc
                return {"ok": True}
            case ("POST", ["kodi", "restart"]):
                self.services.expect("kodi.service", "restarted from the web page")
                unit_action("restart", "kodi.service")
                self.changed()
                return {"ok": True}
            case ("GET", ["audio"]):
                return audio_status()
            case ("POST", ["audio"]):
                return audio_set(body)
            case ("GET", ["bluetooth"]):
                return {"scanning": self.bt.scanning, "devices": self.bt.devices()}
            case ("POST", ["bluetooth", "scan"]):
                self.bt.scan()
                return {"scanning": True}
            case ("POST", ["bluetooth", action]):
                return {"output": self.bt.action(action, str(body.get("mac", "")))}
            case ("POST", ["system", ("reboot" | "poweroff") as action]):
                subprocess.Popen(["sudo", "-n", "/usr/bin/systemctl", action])
                return {"ok": True}
        raise ApiError(HTTPStatus.NOT_FOUND, f"no route for {method} {path}")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"couchd/{VERSION}"

        def log_message(self, fmt: str, *args: object) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: HTTPStatus, payload: object) -> None:
            if isinstance(payload, Raw):
                data, content_type = payload.data, payload.content_type
            else:
                data, content_type = json.dumps(payload).encode(), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if isinstance(payload, Raw):
                self.send_header("Cache-Control", "max-age=86400")  # artwork paths change when the art does
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _events(self) -> None:
            """Server-Sent Events: a status snapshot now, then on every change."""
            try:
                q = app.events.subscribe()
            except ApiError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._event(app.status())
                while True:
                    try:
                        self._event(q.get(timeout=20))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keep proxies and clients alive
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                app.events.unsubscribe(q)

        def _event(self, payload: dict) -> None:
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

        def _handle(self, method: str) -> None:
            path, _, raw_query = self.path.partition("?")
            query = dict(urllib.parse.parse_qsl(raw_query))
            if method == "GET" and path == "/api/events":
                self._events()
                return
            if not path.startswith("/api/"):
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
                    if not isinstance(body, dict):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
                self._send(HTTPStatus.OK, app.route(method, path, body, query))
            except (ApiError, PlexError) as exc:
                self._send(exc.status, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except subprocess.TimeoutExpired as exc:
                self._send(HTTPStatus.GATEWAY_TIMEOUT, {"error": f"timed out: {exc.cmd}"})
            except Exception as exc:  # report, keep serving
                log.exception("request failed")
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(HTTPStatus.NO_CONTENT, {})

    return Handler


def main() -> None:
    logging.basicConfig(level=os.environ.get("COUCHD_LOG", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    app = App()
    app.airplay.start()
    if app.catalog:
        app.catalog.start()
    app.services.start()

    def every_minute() -> None:
        app.services.check()
        app.services.save()  # only when something happened: rare writes

    app.metrics.start(every_minute)
    app.start()
    DisplayWatchdog(app.services).start()
    app.schedule.start()
    server = ThreadingHTTPServer((BIND, PORT), make_handler(app))
    server.daemon_threads = True
    # systemd stops us with SIGTERM: exit cleanly so the metrics get saved.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("couchd %s listening on %s:%d", VERSION, BIND, PORT)
    try:
        server.serve_forever()
    finally:
        app.metrics.save()
        app.services.save()


if __name__ == "__main__":
    main()

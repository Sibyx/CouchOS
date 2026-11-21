#!/usr/bin/env python3
"""couchd - CouchOS control daemon.

Runs as a systemd --user service of the media user, next to Kodi and UxPlay.

* AirPlay inside Kodi: Kodi (and Plex in it) never stops. UxPlay only
  receives and forwards RTP; airplay-bridge muxes it into MPEG-TS on
  udp://127.0.0.1:5010. UxPlay writes its -dacp file only while a client is
  connected: couchd then makes Kodi play the stream (hardware decode) and
  wakes the TV, and stops it when the client leaves, so Plex is back at once.
* HDMI-CEC: through Kodi while it runs (it owns /dev/cec0), else cec-ctl.
* Bluetooth speakers (bluetoothctl) and volume (wpctl).
* JSON API on the LAN (trusted network, no auth) for the web UI and future
  phone apps.

Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0"

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
AIRPLAY_URL = os.environ.get("COUCHD_AIRPLAY_URL", "udp://127.0.0.1:5010/airplay.ts")

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
                kodi_rpc("Addons.ExecuteAddon", {"addonid": "service.couchos", "params": [command]})
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


class AirPlay:
    """Plays the AirPlay stream in Kodi while an iPhone/Mac is connected.

    There are no modes: Plex is always the TV's home, AirPlay is a temporary
    full-screen video on top of it.
    """

    def __init__(self, cec: Cec):
        self.cec = cec
        self.lock = threading.Lock()
        self.active = False
        self.since: float | None = None
        self._gone_since: float | None = None
        self._saved_refresh: int | None = None

    def start(self) -> None:
        threading.Thread(target=self._watch, name="airplay", daemon=True).start()

    def _watch(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:  # keep watching whatever happens
                log.exception("airplay tick failed")
            time.sleep(0.25)

    def _tick(self) -> None:
        connected = DACP_FILE.exists()
        with self.lock:
            if connected and not self.active:
                self.active = True
                self.since = time.time()
                self._gone_since = None
                log.info("AirPlay client connected")
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
        try:
            self.cec.tv("on")
        except (OSError, ApiError, subprocess.TimeoutExpired) as exc:
            log.warning("CEC wake failed: %s", exc)

    def _end(self) -> None:
        self.active = False
        self.since = None
        self._gone_since = None
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
        """Kick the connected device: restarting UxPlay drops the session."""
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


def system_status() -> dict:
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = int(value.split()[0])
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


def addresses() -> list[str]:
    out = run("ip", "-4", "-o", "addr", "show", "scope", "global").stdout
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)


class App:
    def __init__(self) -> None:
        self.cec = Cec()
        self.airplay = AirPlay(self.cec)
        self.bt = Bluetooth()
        self._status: tuple[float, dict] | None = None
        self._status_lock = threading.Lock()

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
                "airplay": {
                    "running": unit_active("uxplay.service"),
                    "active": self.airplay.active,
                    "since": self.airplay.since,
                },
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

    def route(self, method: str, path: str, body: dict) -> object:
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        match (method, parts):
            case ("GET", ["health"]):
                return {"ok": True}
            case ("GET", ["status"]):
                return self.status()
            case ("GET", ["info"]):
                return self.info()
            case ("POST", ["airplay", "stop"]):
                self.airplay.disconnect()
                with self._status_lock:
                    self._status = None
                return {"ok": True}
            case ("POST", ["tv"]):
                return {"via": self.cec.tv(str(body.get("power", "")))}
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
                unit_action("restart", "kodi.service")
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
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
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
                self._send(HTTPStatus.OK, app.route(method, path, body))
            except ApiError as exc:
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
    server = ThreadingHTTPServer((BIND, PORT), make_handler(app))
    server.daemon_threads = True
    log.info("couchd %s listening on %s:%d", VERSION, BIND, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()

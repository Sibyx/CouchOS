"""couchd's catalog API, from Kodi on the same Pi (no Kodi imports here)."""

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"


class CouchdError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def base_url(config_path):
    """couchd's address, from the file Ansible writes next to the add-on."""
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)["url"].rstrip("/")
    except (OSError, ValueError, KeyError, AttributeError):
        return DEFAULT_URL


class Couchd:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _call(self, method, path, params=None, body=None, timeout=8):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error")
            except (ValueError, AttributeError, OSError):
                message = None
            raise CouchdError(exc.code, message or "couchd answered %d" % exc.code) from exc
        except (OSError, ValueError) as exc:
            raise CouchdError(0, "couchd is not reachable (%s)" % exc) from exc

    # -- reads (the index on the Pi: milliseconds)

    def hubs(self):
        return self._call("GET", "/api/plex/hubs").get("hubs") or []

    def sections(self):
        return self._call("GET", "/api/plex/sections").get("sections") or []

    def section(self, key, start, size, sort, unwatched):
        return self._call("GET", "/api/plex/sections/%s" % key, {
            "start": start, "size": size, "sort": sort, "unwatched": 1 if unwatched else None})

    def letters(self, key, unwatched):
        return self._call("GET", "/api/plex/sections/%s/letters" % key,
                          {"unwatched": 1 if unwatched else None}).get("letters") or []

    def search(self, text, kind=None, limit=None):
        return self._call("GET", "/api/plex/search", {"q": text, "type": kind, "limit": limit}).get("groups") or []

    def item(self, key, fresh=False):
        return self._call("GET", "/api/plex/items/%s" % key, {"fresh": 1 if fresh else None},
                          timeout=10 if fresh else 8)

    def art(self, path, width, height):
        """The URL Kodi loads a picture from (couchd's cache on the Pi)."""
        if not path:
            return ""
        return "%s/api/plex/art?%s" % (self.base, urllib.parse.urlencode({"path": path, "w": width, "h": height}))

    # -- actions

    def play(self, key, from_start=False, replace=False):
        return self._call("POST", "/api/plex/play", body={
            "key": str(key), "play": True, "from_start": bool(from_start), "replace": bool(replace)})

    def refresh(self, key):
        return self._call("POST", "/api/catalog/items/%s/refresh" % key, body={})

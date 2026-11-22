"""The dashboard on the TV, as a Kodi screensaver.

Kodi draws everything; this script only copies dashd's display strings
(/v1/views/tv) into window properties, which the skin XML binds to. It reads
them when the screensaver opens and again only when dashd announces a change
(JSONRPC.NotifyAll from "dashd"), so an idle dashboard costs nothing beyond
Kodi redrawing the clock once a minute.

Static content all day on a TV risks burn-in: the layout drifts a few pixels
every few minutes.
"""

import json
import urllib.error
import urllib.request

import xbmc
import xbmcgui

DASHD_CONFIG = "/opt/couchos/dashd/dashd.json"  # written by Ansible (dashd role)
ASSETS = "/v1/assets/"
SKIN_XML = "screensaver-couchos-dashboard.xml"
LAYOUT = 100  # the group holding everything (see the skin XML)
SHIFT_SECONDS = 240
# Small drift around the design position, in pixels.
SHIFTS = [(0, 0), (6, 4), (-4, 8), (8, -4), (-6, -6), (2, 10)]


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("screensaver.couchos.dashboard: %s" % msg, level)


def dashd_url():
    """dashd on this machine, at the port its own config binds (one source)."""
    try:
        with open(DASHD_CONFIG, encoding="utf-8") as f:
            port = json.load(f)["bind"].rsplit(":", 1)[1]
    except (OSError, ValueError, KeyError, IndexError):
        port = "8766"
    return "http://127.0.0.1:%s" % port


DASHD = dashd_url()


class Dashboard(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._etag = None
        self._keys = set()
        self._shift = 0
        self.closed = False

    def load(self):
        """Copy dashd's view into window properties (skipped when unchanged)."""
        request = urllib.request.Request(DASHD + "/v1/views/tv")
        if self._etag:
            request.add_header("If-None-Match", self._etag)
        try:
            # dashd is local and answers from memory: a slow answer means
            # trouble, and this runs before the first frame, so give up fast.
            with urllib.request.urlopen(request, timeout=1) as response:
                self._etag = response.headers.get("ETag")
                view = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 304:
                log("dashd answered %s" % exc.code, xbmc.LOGWARNING)
            return
        except (OSError, ValueError) as exc:
            # dashd down: keep what is on screen; the clock still runs.
            self.setProperty("offline", "true")
            log("dashd unreachable: %s" % exc, xbmc.LOGWARNING)
            return
        properties = view.get("properties", {})
        for key in self._keys - properties.keys():
            self.clearProperty(key)
        for key, value in properties.items():
            if value.startswith(ASSETS):
                value = DASHD + value
            self.setProperty(key, value)
        self._keys = set(properties)
        self.clearProperty("offline")

    def drift(self):
        self._shift = (self._shift + 1) % len(SHIFTS)
        try:
            self.getControl(LAYOUT).setPosition(*SHIFTS[self._shift])
        except RuntimeError:
            pass  # the window is closing

    def onAction(self, action):
        # Kodi ends the screensaver on input itself; this only makes the
        # window go at once instead of on the next loop turn.
        self.closed = True
        self.close()


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.done = False
        self.changed = False

    def onScreensaverDeactivated(self):
        self.done = True

    def onNotification(self, sender, method, data):
        if sender == "dashd" and method == "Other.changed":
            self.changed = True


def run(addon_path):
    monitor = Monitor()
    window = Dashboard(SKIN_XML, addon_path, "default", "1080i")
    window.load()  # before show: the first frame already has the data
    window.show()
    ticks = 0
    # One wake-up a second (a sleep in Kodi's interpreter thread): checks
    # the flags the monitor sets from Kodi's callbacks.
    while not (monitor.done or window.closed) and not monitor.waitForAbort(1):
        ticks += 1
        if monitor.changed:
            monitor.changed = False
            window.load()
        if ticks % SHIFT_SECONDS == 0:
            window.drift()
    window.close()
    del window

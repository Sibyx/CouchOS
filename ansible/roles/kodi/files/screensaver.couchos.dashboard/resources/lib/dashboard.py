"""The dashboard on the TV.

Kodi draws everything; this only copies dashd's display strings
(/v1/views/tv) into window properties, which the skin XML binds to.

It is Kodi's screensaver (screensaver.couchos.dashboard), so Kodi decides
when it shows: on idle, never over video, gone on the first key. But the
window lives in service.couchos (Resident): that process is always running,
keeps dashd's last view in memory and opens the window the moment Kodi
announces the screensaver, with no Python interpreter to start (seconds on
a Pi 3) and no request to wait for. The screensaver script itself then has
nothing to do. Only when service.couchos isn't running does the script show
the window on its own (run()).

dashd is read again only when it announces a change (JSONRPC.NotifyAll from
"dashd"), so an idle dashboard costs nothing beyond Kodi redrawing the clock
once a minute.

Static content all day on a TV risks burn-in: the layout drifts a few pixels
every few minutes.

The bottom row shows only the cards with something in them; the last slot
is a carousel when more want it (Window.Property(slide)), the next slide
every SLIDE_SECONDS, skipping slides without data.
"""

import json
import threading
import urllib.error
import urllib.request

import xbmc
import xbmcgui

DASHD_CONFIG = "/opt/couchos/dashd/dashd.json"  # written by Ansible (dashd role)
ASSETS = "/v1/assets/"
SKIN_XML = "screensaver-couchos-dashboard.xml"
LAYOUT = 100  # the group holding everything (see the skin XML)
CLOCK_LAYOUT = 101  # the night clock alone (mode.layout "clock")
SHIFT_SECONDS = 240
# Small drift around the design position, in pixels.
SHIFTS = [(0, 0), (6, 4), (-4, 8), (8, -4), (-6, -6), (2, 10)]
# The night clock wanders the screen instead: the time sits in a 600 x 150
# box at (660, 465); these keep it inside 1920 x 1080 with a margin.
CLOCK_SHIFTS = [(0, 0), (-480, -300), (420, 260), (-300, 320), (480, -280), (120, -360), (-120, 360)]
SLIDE_SECONDS = 20
# (slide, view keys of which one has to be set for it to show), in order.
# The "app" slot shows the movies to watch instead of the app while there are any.
SLIDES = [("days", ("weather.days",)), ("app", ("app.image", "watchlist.n"))]
# The movies to watch card features one film at a time, the next this often.
WATCH_SECONDS = 15
WATCH_FIELDS = ("title", "line", "ratings", "poster")
# Bottom-row cards shown only when these view keys are set (the TO DO list).
BOARD_CARDS = ("board.items",)
HOST = "couchos.host"  # Window(Home) property: service.couchos is up


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


class View:
    """dashd's TV view, kept between showings."""

    def __init__(self):
        self.etag = None
        self.properties = {}
        self.offline = False

    def fetch(self):
        """True when it changed."""
        request = urllib.request.Request(DASHD + "/v1/views/tv")
        if self.etag:
            request.add_header("If-None-Match", self.etag)
        try:
            # dashd is local and answers from memory: a slow answer means
            # trouble, so give up fast.
            with urllib.request.urlopen(request, timeout=1) as response:
                self.etag = response.headers.get("ETag")
                view = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 304:
                log("dashd answered %s" % exc.code, xbmc.LOGWARNING)
            changed, self.offline = self.offline, False
            return changed
        except (OSError, ValueError) as exc:
            # dashd down: keep what is on screen; the clock still runs.
            log("dashd unreachable: %s" % exc, xbmc.LOGWARNING)
            changed, self.offline = not self.offline, True
            return changed
        properties = {}
        for key, value in view.get("properties", {}).items():
            properties[key] = DASHD + value if value.startswith(ASSETS) else value
        self.properties, self.offline = properties, False
        return True


class Dashboard(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shown = {}
        self._shift = 0
        self._slide = ""
        self._movie = 0  # which film the watch list card features
        self._featured = None
        self.closed = False

    def apply(self, view):
        """Copy the view into window properties (only what changed)."""
        properties = view.properties
        for key in self._shown.keys() - properties.keys():
            self.clearProperty(key)
        for key, value in properties.items():
            if self._shown.get(key) != value:
                self.setProperty(key, value)
        self._shown = dict(properties)
        # How many of the bottom row's three slots the TO DO list takes; the
        # skin lays the rest out around it (the message is a banner on top).
        self.setProperty("board.cards", str(sum(1 for key in BOARD_CARDS if properties.get(key))))
        if view.offline:
            self.setProperty("offline", "true")
        else:
            self.clearProperty("offline")
        if self._slide not in self.slides():
            self.next_slide()
        self.show_movie()

    def slides(self):
        return [name for name, keys in SLIDES if any(self._shown.get(k) for k in keys)]

    def show_movie(self):
        """Copy the featured film's strings (watchlist.item<N>.*) to the
        card's own properties (watchlist.*), only when they change."""
        try:
            count = int(self._shown.get("watchlist.n") or 0)
        except ValueError:
            count = 0
        if count:
            self._movie %= count
            k = self._movie + 1
            featured = tuple(self._shown.get("watchlist.item%d.%s" % (k, f), "") for f in WATCH_FIELDS)
        else:
            featured = ("",) * len(WATCH_FIELDS)
        if featured != self._featured:
            self._featured = featured
            for field, value in zip(WATCH_FIELDS, featured):
                self.setProperty("watchlist." + field, value)

    def next_movie(self):
        self._movie += 1
        self.show_movie()

    def next_slide(self):
        """Show the slide after the current one (the first one at start)."""
        slides = self.slides()
        if self._slide in slides:
            slide = slides[(slides.index(self._slide) + 1) % len(slides)]
        else:
            slide = slides[0] if slides else ""
        if slide != self._slide:  # one slide only: nothing to redraw
            self._slide = slide
            self.setProperty("slide", slide)

    def drift(self):
        self._shift += 1
        try:
            self.getControl(LAYOUT).setPosition(*SHIFTS[self._shift % len(SHIFTS)])
            self.getControl(CLOCK_LAYOUT).setPosition(*CLOCK_SHIFTS[self._shift % len(CLOCK_SHIFTS)])
        except RuntimeError:
            pass  # the window is closing

    def onAction(self, action):
        # Kodi ends the screensaver on input itself; this only makes the
        # window go at once.
        self.closed = True
        self.close()


class Resident:
    """The dashboard kept ready in service.couchos. Call everything from
    the thread that created it (Kodi runs window callbacks there)."""

    def __init__(self, addon_path):
        self.path = addon_path
        self.view = View()
        self.window = None
        self.changed = True  # read dashd before the first showing
        self.ticks = 0
        self.lock = threading.Lock()

    @property
    def showing(self):
        return self.window is not None and not self.window.closed

    def refresh_behind(self):
        """Read dashd on a worker (never on the thread that draws)."""
        if not self.lock.acquire(blocking=False):
            return

        def work():
            try:
                if self.view.fetch():
                    self.changed = True
            finally:
                self.lock.release()

        threading.Thread(target=work, name="dashboard-view", daemon=True).start()

    def show(self):
        if self.showing:
            return
        window = Dashboard(SKIN_XML, self.path, "default", "1080i")
        window.setProperty("board.cards", "0")
        window.apply(self.view)  # the last view at once, then a fresh one behind
        window.show()
        self.window = window
        self.ticks = 0
        self.refresh_behind()

    def hide(self):
        window, self.window = self.window, None
        if window is not None:
            window.close()
            del window

    def tick(self, seconds):
        """Every loop turn; `seconds` since the last. Cheap when hidden."""
        if self.window is not None and self.window.closed:
            self.hide()
        if self.changed and self.showing:
            self.changed = False
            self.window.apply(self.view)
        if not self.showing:
            return
        before, self.ticks = self.ticks, self.ticks + seconds
        if int(self.ticks) // SHIFT_SECONDS != int(before) // SHIFT_SECONDS:
            self.window.drift()
        if int(self.ticks) // SLIDE_SECONDS != int(before) // SLIDE_SECONDS:
            self.window.next_slide()
        if int(self.ticks) // WATCH_SECONDS != int(before) // WATCH_SECONDS:
            self.window.next_movie()


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
    """The screensaver on its own (service.couchos is not running)."""
    if xbmcgui.Window(10000).getProperty(HOST):
        return  # service.couchos shows it (Resident)
    monitor = Monitor()
    view = View()
    view.fetch()
    window = Dashboard(SKIN_XML, addon_path, "default", "1080i")
    window.setProperty("board.cards", "0")
    window.apply(view)  # before show: the first frame already has the data
    window.show()
    ticks = 0
    while not (monitor.done or window.closed) and not monitor.waitForAbort(1):
        ticks += 1
        if monitor.changed:
            monitor.changed = False
            if view.fetch():
                window.apply(view)
        if ticks % SHIFT_SECONDS == 0:
            window.drift()
        if ticks % SLIDE_SECONDS == 0:
            window.next_slide()
        if ticks % WATCH_SECONDS == 0:
            window.next_movie()
    window.close()
    del window

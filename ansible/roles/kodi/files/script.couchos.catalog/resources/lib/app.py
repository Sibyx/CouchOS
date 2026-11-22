"""The catalog's screens: search (with the home rows), a movie or a show
(seasons and episodes on one screen), and a library grid.

Screens are Kodi windows opened inside each other (doModal), so Back is
instant: the screen below is still there. A screen ends with a Result:
  back    the screen below takes over again
  goto    the screen below opens another one in its place
  play    every screen closes, then the host asks couchd to play
  search  every screen closes down to search (the blue key)
  exit    every screen closes

It runs inside service.couchos (resident: no interpreter to start, the home
rows already built), or on its own from default.py when that isn't running.

Responsiveness rules, because the Pi 3 is slow and every key waits for
the callback before it: nothing slow runs on the window's callback thread.
Searches run one at a time on a worker, only the newest query is sent, and
results are built off the callback thread and swapped in at once. Kodi moves
focus itself wherever it can (between the rows, in the grid); Python only
scrolls the rows at their top and bottom edges.

List items carry their text in properties, not in their labels: Kodi jumps
through a list by label on the number keys, which threw the focus around
while typing.

All data comes from couchd's index on the Pi, artwork from couchd's cache in
the sizes it keeps.
"""

import json
import os
import threading
import time

import xbmc
import xbmcgui

from . import keys, present
from .couchd import Couchd, CouchdError, base_url

ADDON_ID = "script.couchos.catalog"
SKIN = ("default", "1080i")
MAIN_XML = "couchos-catalog-main.xml"
ITEM_XML = "couchos-catalog-item.xml"
LIBRARY_XML = "couchos-catalog-library.xml"
POSTER = (320, 480)  # couchd's list sizes: already on disk when prefetched
STILL = (320, 180)
FANART = (1280, 720)
ROW_ITEMS = 15  # per row: enough to scroll, cheap to build on a Pi 3
SEARCH_PAUSE = 0.12  # seconds a typed query rests before it is sent
HOME_TTL = 120  # seconds the home rows count as current


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("%s: %s" % (ADDON_ID, msg), level)


def notify(message, ms=4000):
    xbmcgui.Dialog().notification("CouchOS", message, xbmcgui.NOTIFICATION_INFO, ms, False)


def api_for(path):
    return Couchd(base_url(os.path.join(path, "resources", "couchd.json")))


class Result:
    def __init__(self, kind, **fields):
        self.kind = kind
        self.__dict__.update(fields)


def screen_for(item):
    """Where an entry opens: episodes and seasons open their show, on that
    season (and episode), so the whole show is one screen."""
    kind = item.get("type")
    if kind == "episode" and item.get("grandparent"):
        return ("item", {"key": item["grandparent"], "season": item.get("parent"),
                         "episode": item["key"], "focus_episodes": True})
    if kind == "season" and item.get("parent"):
        return ("item", {"key": item["parent"], "season": item["key"]})
    return ("item", {"key": item["key"]})


def entry(label="", label2=""):
    """A list item whose text is in properties (see the module docstring)."""
    li = xbmcgui.ListItem("", offscreen=True)
    li.setProperty("label", label)
    if label2:
        li.setProperty("label2", label2)
    return li


def set_label(li, label):
    li.setProperty("label", label)


def set_progress(li, item):
    """Resume point, so the skin's ListItem.PercentPlayed draws the bar."""
    if not present.in_progress(item) or not item.get("duration_ms"):
        return
    try:
        li.getVideoInfoTag().setResumePoint(item["offset_ms"] / 1000.0, item["duration_ms"] / 1000.0)
    except (AttributeError, RuntimeError, TypeError):
        pass


def card(api, item):
    """A poster in a row or the grid."""
    li = entry(*present.card_labels(item))
    poster = api.art(item.get("poster") or item.get("thumb"), *POSTER)
    li.setArt({"poster": poster, "thumb": poster})
    li.setProperty("title", present.focus_title(item))
    li.setProperty("kicker", present.kicker(item))
    li.setProperty("meta", present.meta(item))
    li.setProperty("summary", item.get("summary") or "")
    li.setProperty("unwatched", "1" if present.unwatched(item) else "")
    set_progress(li, item)
    return li


def episode_card(api, item):
    li = entry(present.episode_label(item), present.meta(item))
    li.setArt({"thumb": api.art(item.get("thumb") or item.get("art"), *STILL)})
    li.setProperty("kicker", present.kicker(item))
    li.setProperty("summary", item.get("summary") or "")
    li.setProperty("unwatched", "1" if present.unwatched(item) else "")
    set_progress(li, item)
    return li


def build_rows(api, rows):
    """[(title, items)] -> rows ready to show: items cut to ROW_ITEMS, cards built."""
    out = []
    for title, items in rows:
        items = items[:ROW_ITEMS]
        out.append({"title": title, "items": items, "cards": [card(api, i) for i in items], "pos": 0})
    return out


# --- session --------------------------------------------------------------------


class Monitor(xbmc.Monitor):
    """Only when the catalog runs on its own (default.py); in service.couchos
    the host's monitor routes the same notifications."""

    def __init__(self, session):
        super().__init__()
        self.session = session

    def onNotification(self, sender, method, data):
        if sender not in ("couchos", "couchos.catalog"):
            return
        if method.endswith("catalog.close"):
            if not self.session.on_top():
                self.session.close_all()
        elif method.endswith("catalog") or method.endswith("search"):
            xbmcgui.Window(10000).clearProperty("couchos.catalog.ping")  # default.py: "I'm here"
            self.session.to_search()


class Session:
    """What lives as long as the catalog's process: couchd's address, the
    open screens, and (resident) the home rows and libraries ready to show."""

    def __init__(self, path, own_monitor=True):
        self.path = path
        self.api = api_for(path)
        self.stack = []
        self.monitor = Monitor(self) if own_monitor else None
        self.lock = threading.Lock()
        self.home = None  # (time, rows) built cards for an empty query
        self.sections = None
        self.window_ids = set()  # Kodi's ids of the catalog's own windows

    @property
    def active(self):
        return bool(self.stack)

    def on_top(self):
        """Is one of the catalog's windows what the TV shows? Something
        else (Plex opened from the phone, Kodi's photos) can open over it
        without closing it; dialogs (keyboard, menus) don't count."""
        return xbmcgui.getCurrentWindowId() in self.window_ids

    def window(self, cls, xml):
        return cls(xml, self.path, *SKIN)

    def run_window(self, window):
        self.stack.append(window)
        try:
            window.doModal()
        finally:
            self.stack.pop()
        return window.result

    def open(self, screen):
        """Run a screen on top of the current one. None when it went back;
        else the Result the screen below has to act on."""
        while screen is not None:
            kind, args = screen
            if kind == "item":
                window = self.window(ItemWindow, ITEM_XML)
            else:
                window = self.window(LibraryWindow, LIBRARY_XML)
            window.setup(self, **args)
            result = self.run_window(window)
            del window
            if result.kind == "goto":
                screen = result.screen
                continue
            return None if result.kind == "back" else result
        return None

    def to_search(self):
        if not self.stack:
            return
        top = self.stack[-1]
        if isinstance(top, MainWindow):
            top.focus_search()
        else:
            top.finish(Result("search"))

    def close_all(self):
        """Something else takes the screen (the dashboard, a video, a window
        opened over the catalog)."""
        for window in list(self.stack):
            window.finish(Result("exit"))

    # -- kept ready (resident)

    def prefetch(self):
        """Home rows and libraries, built before anyone asks. Safe from any
        thread; costs one small request each to couchd on the Pi."""
        try:
            rows = build_rows(self.api, present.hub_rows(self.api.hubs()))
            sections = self.api.sections()
        except CouchdError as exc:
            log("prefetch: %s" % exc, xbmc.LOGDEBUG)
            return
        with self.lock:
            self.home = (time.monotonic(), rows)
            self.sections = sections

    def home_rows(self):
        with self.lock:
            return self.home[1] if self.home else None

    def home_fresh(self):
        with self.lock:
            return bool(self.home) and time.monotonic() - self.home[0] < HOME_TTL


def run(path, argv=(), session=None):
    """The catalog until it closes; returns its Result (default.py or the
    host plays it afterwards, with every window closed)."""
    session = session or Session(path)
    main = session.window(MainWindow, MAIN_XML)
    main.setup(session)
    result = session.run_window(main)
    if not main.inited:
        # doModal returned without the window ever showing: Kodi refused it
        # (a modal dialog was open), which it only logs at info level.
        log("the catalog window did not open (dialog %d is up)" % xbmcgui.getCurrentWindowDialogId(),
            xbmc.LOGWARNING)
    del main
    return result


def play(result):
    """After the catalog closed: Plex on the TV plays it."""
    api = api_for(result.path)
    try:
        api.play(result.key, from_start=result.from_start)
    except CouchdError as exc:
        if exc.status == 409 and xbmcgui.Dialog().yesno(
                "CouchOS", "The TV is showing AirPlay.[CR]Stop it and play %s?" % (result.title or "this")):
            try:
                api.play(result.key, from_start=result.from_start, replace=True)
            except CouchdError as exc2:
                notify(str(exc2))
        elif exc.status != 409:
            notify(str(exc))


# --- screens --------------------------------------------------------------------


class Screen(xbmcgui.WindowXML):
    def setup(self, session):
        self.session = session
        self.api = session.api
        self.result = Result("back")
        self.started = False
        self.inited = False  # onInit ran: the window really opened

    def remember_id(self):
        """Called first in onInit, while this window is the current one."""
        self.inited = True
        try:
            self.session.window_ids.add(xbmcgui.getCurrentWindowId())
        except RuntimeError:
            pass

    def finish(self, result):
        self.result = result
        self.close()

    def go(self, screen):
        result = self.session.open(screen)
        if result is None:
            return
        if result.kind == "search" and isinstance(self, MainWindow):
            self.focus_search()
            return
        self.finish(result)

    def play(self, item, from_start=False):
        if item.get("type") in ("show", "season"):
            try:
                item = (self.api.item(item["key"]).get("up_next") or [None])[0]
            except CouchdError as exc:
                notify(str(exc))
                return
            if not item:
                notify("Everything here is watched")
                return
        title = present.focus_title(item)
        self.finish(Result("play", key=item["key"], from_start=from_start, title=title, path=self.session.path))

    def menu(self, item):
        """Info / menu key on an entry: everything else that can be done with it."""
        kind = item.get("type")
        options = []
        if kind in ("movie", "episode"):
            options.append(("play", present.play_label(item)))
            if present.in_progress(item):
                options.append(("start", "Play from the start"))
        else:
            options.append(("play", "Play next episode"))
        if not isinstance(self, ItemWindow) or kind != "episode":
            options.append(("open", {"episode": "Open the show", "season": "Open the show"}.get(kind, "Open")))
        options.append(("refresh", "Refresh from Plex"))
        choice = xbmcgui.Dialog().contextmenu([label for _, label in options])
        if choice < 0:
            return
        action = options[choice][0]
        if action == "play":
            self.play(item)
        elif action == "start":
            self.play(item, from_start=True)
        elif action == "open":
            self.go(screen_for(item))
        elif action == "refresh":
            self.refresh(item)

    def refresh(self, item):
        def work():
            try:
                self.api.refresh(item["key"])
                notify("Refreshing %s from Plex" % (present.focus_title(item) or "it"), 2500)
            except CouchdError as exc:
                notify(str(exc))

        threading.Thread(target=work, daemon=True).start()

    def common_action(self, aid):
        """Keys that mean the same on every screen. True when handled."""
        if aid in keys.BACK:
            self.finish(Result("back"))
        elif aid in keys.STOP:
            self.finish(Result("exit"))
        else:
            return False
        return True

    @staticmethod
    def log_action(action):
        xbmc.log("%s: action %d button %d" % (ADDON_ID, action.getId(), action.getButtonCode()), xbmc.LOGDEBUG)


class MainWindow(Screen):
    """Search, typed on a letter grid (or the number keys, or the phone),
    with live results in rows. With nothing typed, the rows are Plex's
    home rows (Continue Watching, ...)."""

    KEYS, ACTIONS, LIBRARIES = 200, 201, 210
    SLOTS = (301, 302, 303)
    # Focusable but invisible, above and below the rows: reaching one means
    # "scroll the rows" (Kodi moves between the three rows on its own).
    ABOVE, BELOW = 300, 304
    KEYBOARD = list("abcdefghijklmnopqrstuvwxyz1234567890")
    ACTION_KEYS = ["space", "delete", "clear", "kind", "keyboard"]

    def setup(self, session):
        super().setup(session)
        self.query = ""
        self.kind = 0
        self.rows = []
        self.top = 0
        self.lock = threading.RLock()
        self.tap = present.MultiTap()
        self.sections = []
        # The search worker: `wanted` is the newest (query, kind), `shown`
        # what is on screen. One request at a time, never a queue of them.
        self.wanted = None
        self.shown = None
        self.wake = threading.Condition()
        self.closing = False

    def onInit(self):
        self.remember_id()
        if self.getControl(self.KEYS).size():
            return  # back from a screen above: everything is still here
        self.getControl(self.KEYS).addItems([entry(c.upper()) for c in self.KEYBOARD])
        labels = {"space": "Space", "delete": "Delete", "clear": "Clear",
                  "kind": present.SEARCH_KINDS[self.kind][1], "keyboard": "Keyboard"}
        self.getControl(self.ACTIONS).addItems([entry(labels[k]) for k in self.ACTION_KEYS])
        self.show_query()
        self.setFocusId(self.KEYS)
        # Kept ready by the host: on screen at once, refreshed behind.
        home = self.session.home_rows()
        if home:
            self.show_rows(home, ("", None), "")
            if self.session.home_fresh():
                self.shown = ("", None)  # nothing to fetch
        self.show_sections(self.session.sections)
        threading.Thread(target=self.worker, name="catalog-search", daemon=True).start()
        self.request()
        if self.session.sections is None:
            threading.Thread(target=self.load_sections, daemon=True).start()

    def close(self):
        with self.wake:
            self.closing = True
            self.wake.notify()
        super().close()

    # -- the query

    def show_query(self):
        self.setProperty("query", self.query)
        self.setProperty("kind", present.SEARCH_KINDS[self.kind][1])

    def set_query(self, text, tapped=False):
        if not tapped:
            self.tap.reset()
        text = text.lstrip()[:60]
        if text == self.query:
            return
        self.query = text
        self.show_query()  # the only thing done before the key returns
        self.request()

    def cycle_kind(self):
        self.kind = (self.kind + 1) % len(present.SEARCH_KINDS)
        set_label(self.getControl(self.ACTIONS).getListItem(self.ACTION_KEYS.index("kind")),
                  present.SEARCH_KINDS[self.kind][1])
        self.show_query()
        if self.query.strip():
            self.request()

    def focus_search(self):
        self.setFocusId(self.KEYS)

    def phone_keyboard(self):
        """Kodi's own keyboard: the phone's keyboard sheet types into it."""
        text = xbmcgui.Dialog().input("Search Plex", self.query)
        if text:
            self.set_query(text)

    # -- the search worker

    def request(self):
        query = self.query.strip()
        with self.wake:
            self.wanted = (query, present.SEARCH_KINDS[self.kind][0] if query else None)
            self.wake.notify()

    def worker(self):
        covered = 0
        while True:
            with self.wake:
                if not self.closing and (self.wanted is None or self.wanted == self.shown):
                    self.wake.wait(1.0)
                if self.closing:
                    return
                idle = self.wanted is None or self.wanted == self.shown
                wanted = self.wanted
            if idle:
                # Once a second: something opened over the catalog without
                # closing it (Plex from the phone, Kodi's photos)? Twice in a
                # row: ask for it to be closed, on the thread that owns it,
                # so Kodi doesn't come back to it later.
                try:
                    covered = 0 if self.session.on_top() else covered + 1
                except RuntimeError:
                    return
                if covered == 2:
                    log("another window is over the catalog: closing it")
                    xbmc.executeJSONRPC(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
                        "params": {"sender": "couchos", "message": "catalog.close"}}))
                continue
            covered = 0
            if wanted[0]:
                time.sleep(SEARCH_PAUSE)  # typing on: wait for the next key
                with self.wake:
                    if self.wanted != wanted:
                        continue
            self.setProperty("busy", "1")
            error = ""
            try:
                if wanted[0]:
                    rows = build_rows(self.api, present.search_rows(
                        self.api.search(wanted[0], wanted[1], limit=ROW_ITEMS)))
                else:
                    self.session.prefetch()
                    rows = self.session.home_rows() or []
            except CouchdError as exc:
                rows, error = [], str(exc)
            except RuntimeError:  # the window went away
                return
            with self.wake:
                stale = self.wanted != wanted
                self.shown = wanted
            if not stale:
                try:
                    self.show_rows(rows, wanted, error)
                except RuntimeError:
                    return
            self.clearProperty("busy")

    def show_rows(self, rows, wanted, error):
        query = wanted[0]
        with self.lock:
            self.rows = [dict(r, pos=0) for r in rows]
            self.top = 0
            self.fill()
        if error:
            self.setProperty("empty", error)
        elif query and not rows:
            self.setProperty("empty", "Nothing found for “%s”" % query)
        else:
            self.clearProperty("empty")

    def fill(self):
        for i, cid in enumerate(self.SLOTS):
            control = self.getControl(cid)
            control.reset()
            r = self.top + i
            if r < len(self.rows):
                row = self.rows[r]
                control.addItems(row["cards"])
                control.selectItem(min(row["pos"], len(row["cards"]) - 1))
                self.setProperty("row%d" % (i + 1), row["title"])
            else:
                self.clearProperty("row%d" % (i + 1))
        self.setProperty("above", "1" if self.top > 0 else "")
        self.setProperty("below", "1" if self.top + len(self.SLOTS) < len(self.rows) else "")
        focus = self.getFocusId()
        if focus in self.SLOTS and self.top + self.SLOTS.index(focus) >= len(self.rows):
            self.setFocusId(self.KEYS)

    def scroll(self, delta, focus_slot):
        """Move the rows by `delta` (they keep their positions) and focus a slot."""
        with self.lock:
            top = max(0, min(max(0, len(self.rows) - len(self.SLOTS)), self.top + delta))
            if top != self.top:
                for i, cid in enumerate(self.SLOTS):
                    if self.top + i < len(self.rows):
                        self.rows[self.top + i]["pos"] = self.getControl(cid).getSelectedPosition()
                self.top = top
                self.fill()
            slot = min(focus_slot, max(0, len(self.rows) - self.top - 1))
            if self.top + slot < len(self.rows):
                self.setFocusId(self.SLOTS[slot])

    def onFocus(self, cid):
        if cid == self.ABOVE:
            self.scroll(-1, 0)
        elif cid == self.BELOW:
            self.scroll(1, len(self.SLOTS) - 1)

    def focused_item(self, cid):
        if cid not in self.SLOTS:
            return None
        with self.lock:
            r = self.top + self.SLOTS.index(cid)
            if r >= len(self.rows):
                return None
            pos = self.getControl(cid).getSelectedPosition()
            items = self.rows[r]["items"]
            return items[pos] if 0 <= pos < len(items) else None

    # -- libraries

    def load_sections(self):
        try:
            sections = self.api.sections()
        except CouchdError as exc:
            log("libraries: %s" % exc, xbmc.LOGWARNING)
            return
        self.session.sections = sections
        try:
            self.show_sections(sections)
        except RuntimeError:
            pass

    def show_sections(self, sections):
        if not sections or self.sections:
            return
        self.sections = sections
        self.getControl(self.LIBRARIES).addItems(
            [entry(s.get("title") or "", str(s.get("count") or "")) for s in sections])

    # -- input

    def onClick(self, cid):
        if cid == self.KEYS:
            pos = self.getControl(self.KEYS).getSelectedPosition()
            if 0 <= pos < len(self.KEYBOARD):
                self.set_query(self.query + self.KEYBOARD[pos])
        elif cid == self.ACTIONS:
            key = self.ACTION_KEYS[self.getControl(self.ACTIONS).getSelectedPosition()]
            if key == "space":
                self.set_query(self.query + " ")
            elif key == "delete":
                self.set_query(self.query[:-1])
            elif key == "clear":
                self.set_query("")
            elif key == "kind":
                self.cycle_kind()
            elif key == "keyboard":
                self.phone_keyboard()
        elif cid == self.LIBRARIES:
            pos = self.getControl(self.LIBRARIES).getSelectedPosition()
            if 0 <= pos < len(self.sections):
                self.go(("library", {"section": self.sections[pos]}))
        else:
            item = self.focused_item(cid)
            if item:
                self.go(screen_for(item))

    def onAction(self, action):
        self.log_action(action)
        aid = action.getId()
        if self.common_action(aid):  # Return closes the catalog, from anywhere
            return
        focus = self.getFocusId()
        digit = keys.digit(aid)
        if digit is not None:
            self.set_query(self.tap.press(self.query, digit), tapped=True)
        elif aid in keys.RED or aid in keys.BACKSPACE:
            self.set_query(self.query[:-1])
        elif aid in keys.GREEN:
            self.cycle_kind()
        elif aid in keys.YELLOW:
            self.setFocusId(self.LIBRARIES)
        elif aid in keys.BLUE:
            self.focus_search()
        elif focus in self.SLOTS:
            slot = self.SLOTS.index(focus)
            if aid in keys.PAGE_UP:
                self.scroll(-len(self.SLOTS), slot)
            elif aid in keys.PAGE_DOWN:
                self.scroll(len(self.SLOTS), slot)
            elif aid in keys.PLAY:
                item = self.focused_item(focus)
                if item:
                    self.play(item)
            elif aid in keys.MENU:
                item = self.focused_item(focus)
                if item:
                    self.menu(item)


class ItemWindow(Screen):
    """A movie, or a show with its seasons and episodes on one screen."""

    BUTTONS, SEASONS, EPISODES = 400, 410, 420

    def setup(self, session, key, season=None, episode=None, focus_episodes=False):
        super().setup(session)
        self.key = key
        self.season_key = season
        self.episode_key = episode
        self.focus_episodes = focus_episodes
        self.item = {}
        self.seasons = []
        self.season_pos = -1
        self.episodes = []
        self.up_next = None
        self.fallback = None
        self.buttons = []
        self.started_buttons = False
        self.gen = 0
        self.timer = None
        self.lock = threading.RLock()

    def onInit(self):
        self.remember_id()
        if self.started:
            return
        self.started = True
        try:
            self.item = self.api.item(self.key)
        except CouchdError as exc:
            notify(str(exc))
            self.finish(Result("back"))
            return
        self.render()
        if self.is_show():
            self.seasons = self.item.get("children") or []
            self.up_next = (self.item.get("up_next") or [None])[0]
            seasons = []
            for s in self.seasons:
                left = present.left_count(s)
                seasons.append(entry(present.season_name(s), ("%d to watch" % left) if left else "watched"))
            self.getControl(self.SEASONS).addItems(seasons)
            if self.seasons:
                pos = present.pick_season(self.seasons, self.season_key, self.up_next)
                self.getControl(self.SEASONS).selectItem(pos)
                self.load_season(pos)
        self.render_buttons()
        if self.focus_episodes and self.episodes:
            self.setFocusId(self.EPISODES)
        else:
            self.setFocusId(self.BUTTONS)
        # The copy may be minutes old: ask the server (watched, resume).
        threading.Thread(target=self.freshen, daemon=True).start()

    def is_show(self):
        return self.item.get("type") == "show"

    def render(self):
        item = self.item
        self.setProperty("kind", item.get("type") or "")
        self.setProperty("kicker", present.kicker(item))
        self.setProperty("title", present.focus_title(item))
        self.setProperty("meta", present.meta(item))
        self.setProperty("genres", present.genres(item))
        self.setProperty("summary", item.get("summary") or "")
        self.setProperty("people", present.people(item))
        self.setProperty("poster", self.api.art(item.get("poster") or item.get("thumb"), *POSTER))
        self.setProperty("fanart", self.api.art(item.get("art"), *FANART))
        self.setProperty("has_seasons", "1" if self.is_show() else "")

    def play_target(self):
        """What the main button plays: the movie, else the show's next episode
        (or, all watched, the one selected when its season opened)."""
        if not self.is_show():
            return self.item
        return self.up_next or self.fallback

    def render_buttons(self):
        target = self.play_target()
        buttons = []
        if target:
            label = present.play_label(target)
            if self.is_show():
                label = "%s %s" % (label, present.episode_code(target))
            buttons.append(("play", label.strip()))
            if present.in_progress(target):
                buttons.append(("start", "From the start"))
        buttons.append(("refresh", "Refresh"))
        self.buttons = buttons
        self.started_buttons = True
        control = self.getControl(self.BUTTONS)
        control.reset()
        control.addItems([entry(label) for _, label in buttons])

    def freshen(self):
        try:
            fresh = self.api.item(self.key, fresh=True)
        except CouchdError:
            return
        if fresh.get("index", {}).get("stale"):
            return
        keep = ("offset_ms", "watched", "viewed_leaf_count", "leaf_count")
        if all(fresh.get(k) == self.item.get(k) for k in keep):
            return
        try:
            with self.lock:
                self.item = fresh
                self.up_next = (fresh.get("up_next") or [None])[0]
                self.render()
                focus = self.getFocusId()
                self.render_buttons()
                if focus == self.BUTTONS:
                    self.setFocusId(self.BUTTONS)
        except RuntimeError:
            pass  # closed meanwhile

    # -- seasons

    def load_season(self, pos):
        with self.lock:
            if not 0 <= pos < len(self.seasons):
                return
            self.season_pos = pos
            control = self.getControl(self.SEASONS)
            for i in range(control.size()):
                control.getListItem(i).setProperty("current", "1" if i == pos else "")
            try:
                episodes = self.api.item(self.seasons[pos]["key"]).get("children") or []
            except CouchdError as exc:
                notify(str(exc))
                episodes = []
            self.episodes = episodes
            control = self.getControl(self.EPISODES)
            control.reset()
            control.addItems([episode_card(self.api, e) for e in episodes])
            if episodes:
                selected = present.pick_episode(episodes, self.episode_key, self.up_next)
                control.selectItem(selected)
                self.fallback = episodes[selected]
            self.episode_key = None  # only the first season opened is focused on it
            if not self.up_next and self.started_buttons:
                self.render_buttons()

    def load_season_later(self, pos):
        """Scrolling through the season strip: load once it rests."""
        with self.lock:
            self.gen += 1
            gen = self.gen
            if self.timer:
                self.timer.cancel()

            def work():
                if gen == self.gen and pos != self.season_pos:
                    try:
                        self.load_season(pos)
                    except RuntimeError:
                        pass

            self.timer = threading.Timer(0.3, work)
            self.timer.daemon = True
            self.timer.start()

    def step_season(self, delta):
        if not self.seasons:
            return
        pos = max(0, min(len(self.seasons) - 1, self.season_pos + delta))
        if pos != self.season_pos:
            self.getControl(self.SEASONS).selectItem(pos)
            self.load_season(pos)

    def jump_season(self, number):
        pos = present.season_by_number(self.seasons, number)
        if pos is None:
            notify("No season %d" % number if number else "No specials", 2000)
            return
        self.getControl(self.SEASONS).selectItem(pos)
        self.load_season(pos)
        self.setFocusId(self.EPISODES)

    def focused_episode(self):
        pos = self.getControl(self.EPISODES).getSelectedPosition()
        return self.episodes[pos] if 0 <= pos < len(self.episodes) else None

    # -- input

    def onClick(self, cid):
        if cid == self.BUTTONS:
            pos = self.getControl(self.BUTTONS).getSelectedPosition()
            action = self.buttons[pos][0] if 0 <= pos < len(self.buttons) else None
            target = self.play_target()
            if action == "play" and target:
                self.play(target)
            elif action == "start" and target:
                self.play(target, from_start=True)
            elif action == "refresh":
                self.refresh(self.item)
        elif cid == self.SEASONS:
            self.setFocusId(self.EPISODES)
        elif cid == self.EPISODES:
            episode = self.focused_episode()
            if episode:
                self.play(episode)

    def onAction(self, action):
        self.log_action(action)
        aid = action.getId()
        if self.common_action(aid):
            return
        focus = self.getFocusId()
        digit = keys.digit(aid)
        if self.is_show() and digit is not None:
            self.jump_season(int(digit))
        elif self.is_show() and aid in keys.PAGE_UP:
            self.step_season(-1)
        elif self.is_show() and aid in keys.PAGE_DOWN:
            self.step_season(1)
        elif focus == self.SEASONS and aid in (keys.LEFT, keys.RIGHT):
            self.load_season_later(self.getControl(self.SEASONS).getSelectedPosition())
        elif aid in keys.PLAY:
            target = self.focused_episode() if focus == self.EPISODES else self.play_target()
            if target:
                self.play(target)
        elif aid in keys.MENU:
            target = self.focused_episode() if focus == self.EPISODES else self.item
            if target:
                self.menu(target)


class LibraryWindow(Screen):
    """One library as a poster grid: sort, unwatched only, jump to a letter."""

    GRID, BUTTONS = 500, 510
    PAGE = 120
    BUTTON_KEYS = ["sort", "unwatched", "letters"]

    def setup(self, session, section):
        super().setup(session)
        self.section = section
        self.sort = 0
        self.unwatched = False
        self.items = []
        self.total = 0
        self.gen = 0
        self.groups = None
        self.lock = threading.RLock()
        self.loading = threading.Lock()
        self.tap = present.MultiTap()
        self.letter = ""
        self.jump_timer = None

    def onInit(self):
        self.remember_id()
        if self.started:
            # Back from a title. Should Kodi have rebuilt the window's
            # controls meanwhile, put the posters back.
            control = self.getControl(self.GRID)
            if self.items and not control.size():
                control.addItems([card(self.api, item) for item in self.items])
            return
        self.started = True
        self.setProperty("title", self.section.get("title") or "")
        self.getControl(self.BUTTONS).addItems([entry(label) for label in self.button_labels()])
        self.reload()
        self.setFocusId(self.GRID if self.items else self.BUTTONS)

    def button_labels(self):
        return ["Sort: %s" % present.SORTS[self.sort][1],
                "Unwatched only" if self.unwatched else "All titles",
                "Jump to letter"]

    def update_buttons(self):
        control = self.getControl(self.BUTTONS)
        for i, label in enumerate(self.button_labels()):
            set_label(control.getListItem(i), label)
        self.setProperty("sort", present.SORTS[self.sort][1])

    def reload(self):
        with self.lock:
            self.gen += 1
            self.items = []
            self.total = 0
            self.groups = None
            self.getControl(self.GRID).reset()
        self.update_buttons()
        self.load_more(self.gen, 0)

    def load_more(self, gen, until):
        """Pages up to and including position `until` (at least one page)."""
        with self.loading:
            first = True
            while gen == self.gen and (first or len(self.items) <= until) and \
                    (first or len(self.items) < self.total):
                first = False
                self.setProperty("busy", "1")
                try:
                    page = self.api.section(self.section["key"], len(self.items), self.PAGE,
                                            present.SORTS[self.sort][0], self.unwatched)
                except CouchdError as exc:
                    notify(str(exc))
                    break
                finally:
                    self.clearProperty("busy")
                new = page.get("items") or []
                cards = [card(self.api, item) for item in new]  # built outside the lock
                with self.lock:
                    if gen != self.gen:
                        return
                    self.total = int(page.get("total") or 0)
                    self.items.extend(new)
                    self.getControl(self.GRID).addItems(cards)
                    self.setProperty("count", "%d" % self.total)
                if not new:
                    break
            if gen == self.gen and not self.items:
                self.setProperty("empty", "Nothing here" if not self.unwatched else "Everything here is watched")
            else:
                self.clearProperty("empty")

    def maybe_more(self):
        pos = self.getControl(self.GRID).getSelectedPosition()
        if len(self.items) < self.total and pos > len(self.items) - 40 and not self.loading.locked():
            def work(gen=self.gen):
                try:
                    self.load_more(gen, 0)
                except RuntimeError:
                    pass

            threading.Thread(target=work, daemon=True).start()

    def focused_item(self):
        pos = self.getControl(self.GRID).getSelectedPosition()
        return self.items[pos] if 0 <= pos < len(self.items) else None

    # -- letters

    def letter_groups(self):
        if self.groups is None:
            try:
                self.groups = present.letter_groups(self.api.letters(self.section["key"], self.unwatched))
            except CouchdError as exc:
                notify(str(exc))
                return []
        return self.groups

    def ensure_title_sort(self):
        if present.SORTS[self.sort][0] != "title":
            self.sort = 0
            self.reload()

    def jump(self, letter):
        self.ensure_title_sort()
        groups = self.letter_groups()
        target = present.jump_target(groups, letter)
        if target is None:
            return
        self.load_more(self.gen, target)
        control = self.getControl(self.GRID)
        control.selectItem(min(target, control.size() - 1))
        self.setFocusId(self.GRID)
        self.setProperty("jump", letter.upper() if letter.strip() else "#")
        if self.jump_timer:
            self.jump_timer.cancel()
        self.jump_timer = threading.Timer(1.2, lambda: self.clearProperty("jump"))
        self.jump_timer.daemon = True
        self.jump_timer.start()

    def pick_letter(self):
        self.ensure_title_sort()
        groups = self.letter_groups()
        if not groups:
            notify("Letters come once the library is in the index", 2500)
            return
        choice = xbmcgui.Dialog().select("Jump to", [label for label, _ in groups])
        if choice >= 0:
            self.jump(groups[choice][0])

    # -- input

    def onClick(self, cid):
        if cid == self.GRID:
            item = self.focused_item()
            if item:
                self.go(screen_for(item))
        elif cid == self.BUTTONS:
            key = self.BUTTON_KEYS[self.getControl(self.BUTTONS).getSelectedPosition()]
            if key == "sort":
                self.sort = (self.sort + 1) % len(present.SORTS)
                self.reload()
            elif key == "unwatched":
                self.unwatched = not self.unwatched
                self.reload()
            elif key == "letters":
                self.pick_letter()

    def onAction(self, action):
        self.log_action(action)
        aid = action.getId()
        if self.common_action(aid):
            return
        focus = self.getFocusId()
        digit = keys.digit(aid)
        if digit is not None:
            self.letter = self.tap.press(self.letter[-1:] if self.letter else "", digit)
            self.jump(self.letter[-1:])
        elif aid in keys.GREEN:
            self.sort = (self.sort + 1) % len(present.SORTS)
            self.reload()
        elif aid in keys.YELLOW:
            self.unwatched = not self.unwatched
            self.reload()
        elif aid in keys.RED:
            self.pick_letter()
        elif focus == self.GRID:
            if aid in (keys.DOWN, keys.RIGHT) or aid in keys.PAGE_DOWN:
                self.maybe_more()
            elif aid in keys.PLAY:
                item = self.focused_item()
                if item:
                    self.play(item)
            elif aid in keys.MENU:
                item = self.focused_item()
                if item:
                    self.menu(item)

"""CouchOS's resident part in Kodi: one Python process that is always
running, so the TV's own screens open at once.

Starting a Python add-on in Kodi starts a Python interpreter: seconds on a
Pi 3 before anything shows. So the dashboard and the catalog live here,
loaded once, with their data kept current, and open on a notification:

  the dashboard  when Kodi announces its screensaver (screensaver.couchos.
                 dashboard is still Kodi's screensaver: Kodi decides when it
                 shows, never over video, gone on the first key; this
                 process only draws it, at once, from the view it keeps)
  the catalog    JSONRPC.NotifyAll / NotifyAll(couchos,catalog): the blue
                 key, the phone's "Open on TV", script.couchos.catalog.
                 Kodi refuses a window while a modal dialog is up ("Activate
                 of window refused because there are active modal dialogs",
                 2026-11-22: the phone's "CouchOS search" did nothing while
                 the dashboard was on). The dashboard is one: so the
                 screensaver is woken first, then the dashboard and any other
                 dialog (PM4K's spinner) are closed, then the catalog opens
  commands       NotifyAll "command" {"command": "cec_on"} from couchd (CEC,
                 bring video to the front, the dashboard now), run on a
                 worker: no interpreter per command either

Window(Home).Property(couchos.host) is set while this runs; couchd, the
catalog's launcher and the screensaver script check it and fall back to
starting a script when it isn't.

Kodi runs a window's and a monitor's callbacks on the thread that made them,
while it waits in Kodi (waitForAbort, doModal). So everything with a window
is made and used on this module's main thread; the loop wakes every 0.1 s
for the flags the callbacks set. A view that fails is logged and left out;
the rest keeps running.

Also here:

Stuck notifications. With <smartredraw> on (advancedsettings.xml: it saves
20-40 % CPU on a still screen) Kodi can leave a toast up for good: the CEC
adapter's "Pulse-Eight CEC adapter" from startup stayed in the top right
corner for hours, over Plex, video and the dashboard alike (2026-11-22). A
toast still up after longer than any real one is closed. Real ones stay:
the AirPlay pairing PIN is up for 30 s.

Music on the dashboard. When AirPlay starts playing audio only (Spotify and
the like), the dashboard opens; it shows what is playing beside everything
else. Only when the dashboard is the screensaver, and never over video.

Long pauses. A video paused for 30 min (dashboard_paused_minutes in
Ansible, read from couchos.json at start) with no key pressed is stopped
(Plex keeps the resume point) and the dashboard opens: Kodi's dim over a
paused video would otherwise stay all night. Not AirPlay, and only when the
dashboard is the screensaver.

Finding the toast takes xbmc.getCondVisibility: getCurrentWindowDialogId()
reports only the topmost dialog, which is the dashboard itself (a dialog) or
a PM4K one, so it never saw the toast (2026-11-22). getCondVisibility
deadlocks against PAPlayer's fade-out on stop (see inputstream.airplay's
service.py), so it is skipped while Kodi plays audio itself (Plex music);
AirPlay audio plays in VideoPlayer and is safe.
"""

import importlib
import importlib.util
import json
import os
import sys
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

import couchos_commands

HOME = xbmcgui.Window(10000)
HOST = "couchos.host"
TICK = 0.1  # seconds between loop turns: how soon a notification is acted on
TOAST_MAX_SECONDS = 45
CHECK_SECONDS = 5  # stuck toasts
AIRPLAY = "airplay://"
DASHBOARD = "screensaver.couchos.dashboard"
CATALOG = "script.couchos.catalog"
# Let the sender settle (a video handoff can follow an audio start).
MUSIC_DELAY_SECONDS = 3
# A video paused this long, with no key pressed for as long, is stopped and
# the dashboard opens (Plex keeps the resume point). couchos.json in this
# add-on (Ansible: dashboard_paused_minutes) replaces it; 0 = never.
PAUSED_SECONDS = 30 * 60
SETTINGS = "couchos.json"
# Between ending Kodi's dim and starting the screensaver again.
REDIM_SECONDS = 1.0
# The "dashboard" command wakes the TV and shows it at once. Kodi ends the
# screensaver on the CEC wake: twice within 0.4 s with the TV on, and once
# more when a TV that was off reports it switched to us. Within this long, a
# screensaver ended with no key pressed (Kodi's idle time >= 1 s) is CEC:
# show it again, up to DASHBOARD_RESHOWS times.
DASHBOARD_HOLD = 10.0
DASHBOARD_RESHOWS = 3
PREFETCH_SECONDS = 90  # the catalog's home rows, rebuilt while it is closed
WAKE_SECONDS = 2.0  # the catalog waits this long at most for the screensaver to end
CLEAR_SECONDS = 1.0  # ... and this long for closed dialogs to go


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("service.couchos: %s" % msg, level)


def setting(name):
    answer = xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
        "params": {"setting": name}}))
    try:
        return json.loads(answer)["result"]["value"]
    except (ValueError, KeyError, TypeError):
        return None


def wake_screensaver():
    """Kodi's JSON-RPC Input.* methods end the screensaver (and are used up
    doing it), as a key from the phone does."""
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "Input.ExecuteAction", "params": {"action": "noop"}}))


def load_settings():
    """couchos.json beside this add-on, written by Ansible. Missing or
    broken: the defaults, and the rest runs as before."""
    global PAUSED_SECONDS
    try:
        path = os.path.join(xbmcaddon.Addon("service.couchos").getAddonInfo("path"), SETTINGS)
        with open(path) as f:
            settings = json.load(f)
        PAUSED_SECONDS = max(0, int(settings.get("paused_minutes", PAUSED_SECONDS // 60))) * 60
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - a bad file must not stop the service
        log("%s unreadable, using defaults: %r" % (SETTINGS, exc), xbmc.LOGWARNING)


def load_lib(alias, addon_id):
    """An add-on's resources/lib as package `alias` (both views' add-ons
    call their package "resources", so neither can simply be imported).
    Returns the add-on's path, or None if it isn't installed."""
    try:
        path = xbmcaddon.Addon(addon_id).getAddonInfo("path")
    except RuntimeError:
        log("%s is not installed" % addon_id, xbmc.LOGWARNING)
        return None
    lib = os.path.join(path, "resources", "lib")
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(lib, "__init__.py"), submodule_search_locations=[lib])
    package = importlib.util.module_from_spec(spec)
    sys.modules[alias] = package
    spec.loader.exec_module(package)
    return path


class Host:
    def __init__(self):
        self.dashboard = None
        self.catalog = None  # the catalog's app module
        self.session = None  # its Session (kept: home rows ready)
        self.want_catalog = 0.0  # when the catalog was asked for, 0 = not
        self.want_dashboard = False
        self.screensaver = False  # Kodi's, as its callbacks report it
        self.hold_until = 0.0  # see DASHBOARD_HOLD (monotonic), 0 = none
        self.reshows_left = 0
        self.reshow = False  # the loop starts the screensaver again
        self.prefetched = 0.0
        try:
            path = load_lib("couchos_dashboard", DASHBOARD)
            if path:
                self.dashboard = importlib.import_module("couchos_dashboard.dashboard").Resident(path)
        except Exception as exc:  # noqa: BLE001 - keep the rest running
            log("dashboard not available: %r" % exc, xbmc.LOGERROR)
        try:
            path = load_lib("couchos_catalog", CATALOG)
            if path:
                self.catalog = importlib.import_module("couchos_catalog.app")
                self.session = self.catalog.Session(path, own_monitor=False)
        except Exception as exc:  # noqa: BLE001
            log("catalog not available: %r" % exc, xbmc.LOGERROR)

    @property
    def catalog_open(self):
        return self.session is not None and self.session.active

    # -- from the monitor's and player's callbacks (this thread)

    def screensaver_on(self):
        self.screensaver = True
        if self.dashboard is None:
            return
        # Kodi's dim over a paused video fires this too: never over video.
        if xbmc.Player().isPlayingVideo() or setting("screensaver.mode") != DASHBOARD:
            return
        if self.catalog_open:
            self.session.close_all()  # left open: the dashboard takes over
        self.want_dashboard = True

    def screensaver_off(self):
        self.screensaver = False
        self.want_dashboard = False
        if self.dashboard is not None:
            self.dashboard.hide()
        if self.hold_until and time.monotonic() < self.hold_until and self.reshows_left:
            # A key (TV remote or phone) resets Kodi's idle time: then it was
            # meant, and the hold ends. Else it is the TV waking over CEC.
            if xbmc.getGlobalIdleTime() >= 1 and not xbmc.Player().isPlayingVideo():
                self.reshows_left -= 1
                log("screensaver ended by the TV waking up: showing the dashboard again")
                self.reshow = True
            else:
                self.hold_until = 0.0

    def hold_dashboard(self):
        """The "dashboard" command just ran: see DASHBOARD_HOLD."""
        self.hold_until = time.monotonic() + DASHBOARD_HOLD
        self.reshows_left = DASHBOARD_RESHOWS

    def open_catalog(self):
        self.hold_until = 0.0  # the catalog takes over: don't bring the dashboard back
        if self.catalog is None:
            return
        if self.catalog_open and self.session.on_top():
            self.session.to_search()
            return
        if self.catalog_open:
            # Open but under another window: close it, open it fresh on top.
            self.session.close_all()
        log("catalog asked for (window %d, dialog %d, screensaver %s)" % (
            xbmcgui.getCurrentWindowId(), xbmcgui.getCurrentWindowDialogId(), self.screensaver))
        self.want_catalog = time.monotonic()
        if self.screensaver:
            # Ended first: else the first key on the catalog is used up
            # ending it, and PM4K, waking, can come back over the catalog.
            wake_screensaver()

    def close_covered_catalog(self):
        if self.catalog_open and not self.session.on_top():
            self.session.close_all()

    def video_started(self):
        if self.catalog_open:
            self.session.close_all()  # Kodi would come back to it after the video

    # -- the loop

    def clear_the_way(self):
        """No modal dialog may be open when the catalog's window opens."""
        self.want_dashboard = False
        if self.dashboard is not None:
            self.dashboard.hide()
        player = xbmc.Player()
        # Over a video, PM4K's player and seek bar are dialogs: closing them
        # could stop or confuse the playback. Then a refusal is only logged.
        if not player.isPlayingVideo():
            xbmc.executebuiltin("Dialog.Close(all,true)", True)
        if not cond_safe(player):
            xbmc.sleep(300)  # can't ask Kodi safely: give the close a moment
            return
        # A closing dialog (its fade) no longer counts; one still up does.
        deadline = time.monotonic() + CLEAR_SECONDS
        while xbmc.getCondVisibility("System.HasActiveModalDialog") and time.monotonic() < deadline:
            xbmc.sleep(50)

    def run_catalog(self):
        self.want_catalog = 0.0
        self.clear_the_way()
        HOME.setProperty("couchos.catalog.running", "1")
        try:
            result = self.catalog.run(self.session.path, session=self.session)
        except Exception as exc:  # noqa: BLE001
            log("catalog failed: %r" % exc, xbmc.LOGERROR)
            result = None
        finally:
            HOME.clearProperty("couchos.catalog.running")
        if result is not None and result.kind == "play":
            self.catalog.play(result)
        self.prefetched = 0.0  # watched state changes after playing

    def tick(self, seconds):
        if self.reshow:
            self.reshow = False
            xbmc.executebuiltin("ActivateScreensaver")
        # Once the screensaver has ended (or it didn't within WAKE_SECONDS).
        if self.want_catalog and (not self.screensaver or
                                  time.monotonic() - self.want_catalog > WAKE_SECONDS):
            self.run_catalog()
        if self.dashboard is not None:
            if self.want_dashboard and not self.catalog_open:
                self.want_dashboard = False
                try:
                    self.dashboard.show()
                except Exception as exc:  # noqa: BLE001
                    log("dashboard failed: %r" % exc, xbmc.LOGERROR)
            self.dashboard.tick(seconds)
        if self.session is not None and not self.catalog_open and \
                time.monotonic() - self.prefetched > PREFETCH_SECONDS:
            self.prefetched = time.monotonic()
            threading.Thread(target=self.session.prefetch, name="catalog-prefetch", daemon=True).start()


class Monitor(xbmc.Monitor):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.screensaver = False

    def onScreensaverActivated(self):
        self.screensaver = True
        self.host.screensaver_on()

    def onScreensaverDeactivated(self):
        self.screensaver = False
        self.host.screensaver_off()

    def onNotification(self, sender, method, data):
        if sender == "dashd" and method.endswith("changed"):
            if self.host.dashboard is not None:
                self.host.dashboard.refresh_behind()
        elif sender in ("couchos", "couchos.catalog"):
            if method.endswith("catalog.close"):
                self.host.close_covered_catalog()
            elif method.endswith("catalog") or method.endswith("search"):
                HOME.clearProperty("couchos.catalog.ping")  # a launcher waits for this
                self.host.open_catalog()
            elif method.endswith("command"):
                try:
                    args = json.loads(data or "{}")
                    command, ack = args.get("command", ""), str(args.get("id", ""))
                except (ValueError, AttributeError):
                    command, ack = "", ""
                # couchd waits for this, else it starts default.py instead
                # (this callback may not run: a dialog is open, the service hung).
                if ack:
                    HOME.setProperty("couchos.ack", ack)
                if command == "dashboard":
                    self.host.hold_dashboard()
                # On a worker: builtins run waiting, and callbacks must not block.
                threading.Thread(target=couchos_commands.run, args=(command,), daemon=True).start()


class Player(xbmc.Player):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.music_at = 0.0  # when to open the dashboard, 0 = not due
        # Once per session: the AirPlay add-on can rebuild its player
        # mid-session (another AV start), and someone who left the dashboard
        # to browse Plex should not get it back.
        self.opened = False

    def onAVStarted(self):
        try:
            video = self.isPlayingVideo()
            airplay_audio = self.getPlayingFile().startswith(AIRPLAY) and not video
        except RuntimeError:
            return  # stopped already
        if video:
            self.host.video_started()
        if self.opened:
            return
        if airplay_audio:
            self.music_at = time.time() + MUSIC_DELAY_SECONDS

    def onPlayBackStopped(self):
        self.music_at = 0.0
        self.opened = False

    onPlayBackEnded = onPlayBackStopped


def open_dashboard_for_music(player, monitor):
    if not player.music_at or time.time() < player.music_at:
        return
    player.music_at = 0.0
    player.opened = True
    try:
        still_audio = player.isPlaying() and not player.isPlayingVideo()
    except RuntimeError:
        return
    if not still_audio or monitor.screensaver or setting("screensaver.mode") != DASHBOARD:
        return
    log("AirPlay music: opening the dashboard")
    xbmc.executebuiltin("ActivateScreensaver")


def dashboard_after_long_pause(player, monitor, state):
    """A paused video never makes way for the dashboard by itself: Kodi dims
    over it ("use dim if paused", reported as its screensaver, which
    screensaver_on leaves alone), for good. After PAUSED_SECONDS paused and
    as long without a key, stop it (PM4K saves the resume point on stop),
    end the dim, and start the screensaver again: with nothing playing,
    that is the dashboard. AirPlay is left alone (the phone holds it), and
    so is everything when the dashboard isn't the screensaver."""
    if not PAUSED_SECONDS:
        return  # turned off (dashboard_paused_minutes: 0)
    try:
        paused = (player.isPlayingVideo() and xbmc.getCondVisibility("Player.Paused")
                  and not player.getPlayingFile().startswith(AIRPLAY))
    except RuntimeError:
        paused = False  # stopped in between
    if not paused:
        state["since"] = 0.0
        return
    now = time.time()
    if not state["since"]:
        state["since"] = now
        return
    if now - state["since"] < PAUSED_SECONDS or xbmc.getGlobalIdleTime() < PAUSED_SECONDS:
        return
    state["since"] = 0.0
    if setting("screensaver.mode") != DASHBOARD:
        return
    log("video paused for %d min: stopping it for the dashboard" % (PAUSED_SECONDS // 60))
    player.stop()
    deadline = time.monotonic() + 10
    while player.isPlaying() and time.monotonic() < deadline and not monitor.abortRequested():
        time.sleep(0.2)
    if player.isPlaying():
        log("the paused video did not stop; leaving it", xbmc.LOGWARNING)
        return
    if monitor.screensaver:
        wake_screensaver()  # the dim is still Kodi's screensaver: end it first
        time.sleep(REDIM_SECONDS)
    xbmc.executebuiltin("ActivateScreensaver")


def cond_safe(player):
    """Can xbmc.getCondVisibility be called now? Not while PAPlayer plays
    (Plex music): it deadlocks against PAPlayer's fade-out on stop."""
    try:
        return not (player.isPlayingAudio() and not player.getPlayingFile().startswith(AIRPLAY))
    except RuntimeError:
        return False  # stopping right now: the riskiest moment


def toast_visible(player):
    """None when it cannot be checked safely (PAPlayer playing)."""
    if not cond_safe(player):
        return None
    return xbmc.getCondVisibility("Window.IsVisible(notification)")


def close_stuck_toast(player, state):
    visible = toast_visible(player)
    if visible is None:
        return
    if not visible:
        state["since"] = 0.0
    elif not state["since"]:
        state["since"] = time.time()
    elif time.time() - state["since"] > TOAST_MAX_SECONDS:
        log("closing a notification stuck on screen")
        xbmc.executebuiltin("Dialog.Close(notification,true)")
        state["since"] = 0.0


def chores(monitor, player):
    """Stuck toasts, AirPlay music and long pauses, on their own thread: they
    keep going while the catalog is open (the main thread is in its doModal),
    and a hang in any can't freeze the dashboard, the catalog or the commands.
    Only plain calls here: no windows, no callbacks."""
    toast = {"since": 0.0}
    pause = {"since": 0.0}
    seconds = 0
    while not monitor.abortRequested():
        time.sleep(1)
        seconds += 1
        try:
            open_dashboard_for_music(player, monitor)
            dashboard_after_long_pause(player, monitor, pause)
            if seconds % CHECK_SECONDS == 0:
                close_stuck_toast(player, toast)
        except Exception as exc:  # noqa: BLE001 - never end the loop
            log("chores: %r" % exc, xbmc.LOGERROR)


def main():
    load_settings()
    host = Host()
    monitor = Monitor(host)
    player = Player(host)
    HOME.setProperty(HOST, "1")
    log("resident: dashboard %s, catalog %s; watching for stuck notifications, AirPlay music and %s" % (
        "ready" if host.dashboard else "missing", "ready" if host.catalog else "missing",
        "pauses over %d min" % (PAUSED_SECONDS // 60) if PAUSED_SECONDS else "no pauses (off)"))
    threading.Thread(target=chores, args=(monitor, player), name="couchos-chores", daemon=True).start()
    last = time.monotonic()
    try:
        while not monitor.waitForAbort(TICK):
            now = time.monotonic()
            try:
                host.tick(now - last)
            except Exception as exc:  # noqa: BLE001 - never end the loop
                log("loop: %r" % exc, xbmc.LOGERROR)
            last = now
    finally:
        HOME.clearProperty(HOST)
        if host.dashboard is not None:
            host.dashboard.hide()


main()

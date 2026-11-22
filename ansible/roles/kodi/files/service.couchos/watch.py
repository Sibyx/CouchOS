"""CouchOS's resident part in Kodi (default.py runs one-off commands for couchd).

Stuck notifications. With <smartredraw> on (advancedsettings.xml: it saves
20-40 % CPU on a still screen) Kodi can leave a toast up for good: the CEC
adapter's "Pulse-Eight CEC adapter" from startup stayed in the top right
corner for hours, over Plex, video and the dashboard alike (2026-11-22). A
toast still up after longer than any real one is closed. Real ones stay:
the AirPlay pairing PIN is up for 30 s.

Music on the dashboard. When AirPlay starts playing audio only (Spotify and
the like), the dashboard opens; it shows what is playing beside everything
else. Only when the dashboard is the screensaver, and never over video.

No xbmc.getCondVisibility here: it deadlocks against PAPlayer's fade-out on
stop (see inputstream.airplay's service.py), and this runs during Plex music.
"""

import json
import time

import xbmc
import xbmcgui

TOAST = 10107  # WINDOW_DIALOG_KAI_TOAST
TOAST_MAX_SECONDS = 45
CHECK_SECONDS = 5
AIRPLAY = "airplay://"
DASHBOARD = "screensaver.couchos.dashboard"
# Let the sender settle (a video handoff can follow an audio start).
MUSIC_DELAY_SECONDS = 3


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


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.screensaver = False

    def onScreensaverActivated(self):
        self.screensaver = True

    def onScreensaverDeactivated(self):
        self.screensaver = False


class Player(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.music_at = 0.0  # when to open the dashboard, 0 = not due
        # Once per session: the AirPlay add-on can rebuild its player
        # mid-session (another AV start), and someone who left the dashboard
        # to browse Plex should not get it back.
        self.opened = False

    def onAVStarted(self):
        if self.opened:
            return
        try:
            airplay_audio = self.getPlayingFile().startswith(AIRPLAY) and not self.isPlayingVideo()
        except RuntimeError:
            return  # stopped already
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


def main():
    monitor = Monitor()
    player = Player()
    toast_since = 0.0
    while not monitor.waitForAbort(1 if player.music_at else CHECK_SECONDS):
        open_dashboard_for_music(player, monitor)
        if xbmcgui.getCurrentWindowDialogId() != TOAST:
            toast_since = 0.0
        elif not toast_since:
            toast_since = time.time()
        elif time.time() - toast_since > TOAST_MAX_SECONDS:
            log("closing a notification stuck on screen")
            xbmc.executebuiltin("Dialog.Close(%d,true)" % TOAST)
            toast_since = 0.0


main()

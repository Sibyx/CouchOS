# Kodi's screensaver entry point. service.couchos (always running) draws the
# dashboard the moment Kodi announces the screensaver, so this returns at
# once; it shows the dashboard itself only when that service isn't up.
import xbmcaddon

from resources.lib.dashboard import run

run(xbmcaddon.Addon().getAddonInfo("path"))

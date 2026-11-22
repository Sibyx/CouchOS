import xbmcaddon

from resources.lib.dashboard import run

run(xbmcaddon.Addon().getAddonInfo("path"))

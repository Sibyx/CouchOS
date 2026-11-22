"""CouchOS catalog on the TV, started as a script (Kodi's add-on menu, an
old keymap, Addons.ExecuteAddon).

Normally the catalog lives in service.couchos, ready in memory: this only
tells it to open (the blue key and the phone send that notification
directly, without starting this script at all). When service.couchos isn't
running, the catalog runs here on its own.

One at a time: a second start while it runs here tells it to go back to
search. Playing happens after every catalog window has closed: PM4K opens
the item in its own windows, and a catalog window left on top would hide
them.
"""

import json
import sys

import xbmc
import xbmcaddon
import xbmcgui

HOST = "couchos.host"  # service.couchos is up
RUNNING = "couchos.catalog.running"
# Set by a second start; the running catalog clears it when it hears the
# notification. Still set after a moment: the flag above is left over from a
# catalog Kodi stopped (no finally ran), so start afresh.
PING = "couchos.catalog.ping"
HOME = xbmcgui.Window(10000)


def notify_catalog():
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
        "params": {"sender": "couchos", "message": "catalog"}}))


def running_catalog_answers():
    HOME.setProperty(PING, "1")
    notify_catalog()
    for _ in range(10):
        xbmc.sleep(50)
        if not HOME.getProperty(PING):
            return True
    HOME.clearProperty(PING)
    xbmc.log("script.couchos.catalog: stale running flag, starting afresh", xbmc.LOGWARNING)
    return False


def main():
    if HOME.getProperty(HOST):
        notify_catalog()
        return
    if HOME.getProperty(RUNNING) and running_catalog_answers():
        return
    HOME.setProperty(RUNNING, "1")
    try:
        from resources.lib import app
        path = xbmcaddon.Addon().getAddonInfo("path")
        result = app.run(path, sys.argv[1:])
    finally:
        HOME.clearProperty(RUNNING)
    if result and result.kind == "play":
        app.play(result)


main()

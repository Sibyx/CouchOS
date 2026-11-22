"""Commands couchd runs in Kodi. Kodi owns /dev/cec0 while it runs, so CEC
must go through Kodi's builtins.

couchd sends them to the resident service (watch.py) as
  JSONRPC.NotifyAll {"sender": "couchos", "message": "command", "data": {"command": "cec_on"}}
which runs them at once; default.py runs the same ones when started on its
own (Addons.ExecuteAddon, the old way: a new interpreter, seconds on a Pi 3).
Each command is a list of Kodi builtins, run in order; an int is a pause (ms).
"""

import xbmc

COMMANDS = {
    "cec_on": ["CECActivateSource"],
    "cec_standby": ["CECStandby"],
    "cec_toggle": ["CECToggleState"],
    # AirPlay: Kodi refuses to show the video while a modal dialog is open
    # ("Activate of window refused because there are active modal dialogs"),
    # e.g. PM4K's busy spinner or its exit prompt. Close them, then go full screen.
    "show_video": ["Dialog.Close(all,true)", "ActivateWindow(fullscreenvideo)"],
    # The dashboard now (it is the screensaver; Kodi also starts it on idle).
    # Wake the TV first and wait: Kodi ends the screensaver when CEC reports it
    # became the active source, which would close the dashboard right away.
    "dashboard": ["CECActivateSource", 5000, "Dialog.Close(all,true)", "ActivateScreensaver"],
}


def run(command):
    """Run a command's builtins in order (blocks through its pauses)."""
    steps = COMMANDS.get(command)
    if steps is None:
        xbmc.log("service.couchos: unknown command %r" % command, xbmc.LOGWARNING)
        return
    for step in steps:
        if isinstance(step, int):
            xbmc.sleep(step)
        else:
            xbmc.executebuiltin(step, True)

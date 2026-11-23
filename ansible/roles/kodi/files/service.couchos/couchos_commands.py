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
    # The dashboard now (it is the screensaver; Kodi also starts it on idle),
    # at once. Waking the TV over CEC makes Kodi end the screensaver: at
    # once, again a moment later (seen 2026-11-23, TV already on), and when a
    # TV that was off reports it switched to us. watch.py (DASHBOARD_HOLD)
    # puts it back each time, unless a key was pressed. (It used to wait 5 s.)
    "dashboard": ["CECActivateSource", "Dialog.Close(all,true)", "ActivateScreensaver"],
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

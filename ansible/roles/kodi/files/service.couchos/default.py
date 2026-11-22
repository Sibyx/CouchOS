# Run by couchd through JSON-RPC:
#   Addons.ExecuteAddon {"addonid": "service.couchos", "params": ["cec_on"]}
# Kodi owns /dev/cec0 while it runs, so CEC must go through Kodi's builtins.
# Each command is a list of Kodi builtins, run in order; an int is a pause (ms).
import sys

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

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command in COMMANDS:
    for step in COMMANDS[command]:
        if isinstance(step, int):
            xbmc.sleep(step)
        else:
            xbmc.executebuiltin(step, True)
else:
    xbmc.log("service.couchos: unknown command %r" % command, xbmc.LOGWARNING)

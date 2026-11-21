# Run by couchd through JSON-RPC:
#   Addons.ExecuteAddon {"addonid": "service.couchos", "params": ["cec_on"]}
# Kodi owns /dev/cec0 while it runs, so CEC must go through Kodi's builtins.
import sys

import xbmc

COMMANDS = {
    "cec_on": "CECActivateSource",
    "cec_standby": "CECStandby",
    "cec_toggle": "CECToggleState",
}

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command in COMMANDS:
    xbmc.executebuiltin(COMMANDS[command])
else:
    xbmc.log("service.couchos: unknown command %r" % command, xbmc.LOGWARNING)

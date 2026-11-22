# Run by couchd through JSON-RPC when the resident service isn't up:
#   Addons.ExecuteAddon {"addonid": "service.couchos", "params": ["cec_on"]}
# (normally couchd sends the command to watch.py, which runs it at once).
import sys

from couchos_commands import run

run(sys.argv[1] if len(sys.argv) > 1 else "")

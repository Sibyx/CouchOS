"""Kodi action ids the catalog reacts to, by what they mean on the remote.

A Samsung remote reaches Kodi over HDMI-CEC; which of its keys arrive
depends on the TV model. Everything works with the arrows, OK, Return and
Play alone; colours, numbers and channel up/down are shortcuts on top.
Every action is logged (debug level) with its button code, so the real map
of a TV can be read from kodi.log.
"""

import xbmcgui


def _a(name, default):
    return getattr(xbmcgui, name, default)


LEFT = _a("ACTION_MOVE_LEFT", 1)
RIGHT = _a("ACTION_MOVE_RIGHT", 2)
UP = _a("ACTION_MOVE_UP", 3)
DOWN = _a("ACTION_MOVE_DOWN", 4)
SELECT = _a("ACTION_SELECT_ITEM", 7)

# Return on the remote is "back"; Escape on a keyboard is "previous menu".
BACK = {_a("ACTION_NAV_BACK", 92), _a("ACTION_PREVIOUS_MENU", 10)}
STOP = {_a("ACTION_STOP", 13)}
PLAY = {_a("ACTION_PLAYER_PLAY", 79), _a("ACTION_PLAYER_PLAYPAUSE", 229), _a("ACTION_PAUSE", 12)}
MENU = {_a("ACTION_CONTEXT_MENU", 117), _a("ACTION_SHOW_INFO", 11)}
# Channel up/down (and keyboard page up/down, next/previous)
PAGE_UP = {_a("ACTION_PAGE_UP", 5), _a("ACTION_CHANNEL_UP", 184), _a("ACTION_PREV_ITEM", 15)}
PAGE_DOWN = {_a("ACTION_PAGE_DOWN", 6), _a("ACTION_CHANNEL_DOWN", 185), _a("ACTION_NEXT_ITEM", 14)}
BACKSPACE = {_a("ACTION_BACKSPACE", 110)}

RED = {_a("ACTION_TELETEXT_RED", 215)}
GREEN = {_a("ACTION_TELETEXT_GREEN", 216)}
YELLOW = {_a("ACTION_TELETEXT_YELLOW", 217)}
BLUE = {_a("ACTION_TELETEXT_BLUE", 218)}

_REMOTE_0 = _a("ACTION_REMOTE_0", 58)
NUMBERS = {_REMOTE_0 + n: str(n) for n in range(10)}
# The keyboard's digits (development without the TV remote)
_JUMP_SMS = _a("ACTION_JUMP_SMS2", 142)
NUMBERS.update({_JUMP_SMS + n - 2: str(n) for n in range(2, 10)})


def digit(action_id):
    return NUMBERS.get(action_id)

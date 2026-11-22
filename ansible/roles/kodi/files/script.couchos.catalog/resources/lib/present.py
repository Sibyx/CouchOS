"""What the catalog shows, from couchd's entries. No Kodi imports, so it can
be tested anywhere."""

import time

# Multi-tap typing on the remote's number keys, as on a phone keypad: the
# same key again within MULTITAP_SECONDS changes the last character.
MULTITAP = {
    "1": "1", "2": "abc2", "3": "def3", "4": "ghi4", "5": "jkl5",
    "6": "mno6", "7": "pqrs7", "8": "tuv8", "9": "wxyz9", "0": " 0",
}
MULTITAP_SECONDS = 1.2

SEARCH_KINDS = [(None, "All"), ("movie", "Movies"), ("show", "Shows"), ("episode", "Episodes")]
SORTS = [("title", "Title"), ("added", "Recently added"), ("year", "Year"),
         ("rating", "Rating"), ("watched", "Last watched")]


class MultiTap:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.key = None
        self.index = 0
        self.at = 0.0

    def reset(self):
        self.key = None

    def press(self, text, digit):
        """The text after pressing `digit`."""
        letters = MULTITAP.get(digit)
        if not letters:
            return text
        now = self.clock()
        if digit == self.key and now - self.at < MULTITAP_SECONDS and text:
            self.index = (self.index + 1) % len(letters)
            text = text[:-1] + letters[self.index]
        else:
            self.index = 0
            text = text + letters[0]
        self.key, self.at = digit, now
        return text


def duration(ms):
    """5400000 -> "1 h 30 min"."""
    if not ms:
        return ""
    minutes = int(round(ms / 60000.0))
    if minutes < 60:
        return "%d min" % max(1, minutes)
    return "%d h %02d min" % (minutes // 60, minutes % 60) if minutes % 60 else "%d h" % (minutes // 60)


def clock(ms):
    """1390000 -> "23:10", 4000000 -> "1:06:40"."""
    seconds = int((ms or 0) / 1000)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def episode_code(item):
    season, episode = item.get("season"), item.get("episode")
    if season is None or episode is None:
        return ""
    return "S%02dE%02d" % (season, episode)


def season_name(season):
    index = season.get("season")
    if index == 0:
        return "Specials"
    return season.get("title") or ("Season %d" % index if index is not None else "Season")


def left_count(item):
    """Episodes not watched yet, for shows and seasons."""
    leaf = item.get("leaf_count") or 0
    return max(0, leaf - (item.get("viewed_leaf_count") or 0))


def in_progress(item):
    return bool(item.get("offset_ms")) and not item.get("watched")


def unwatched(item):
    if item.get("type") in ("show", "season"):
        return left_count(item) > 0
    return not item.get("watched")


def _join(parts):
    return "  ·  ".join(p for p in parts if p)


def rating(item):
    value = item.get("rating")
    # Plain text: Estuary's Noto Sans has no star, and Kodi has no font fallback.
    return "%.1f/10" % value if isinstance(value, (int, float)) and value else ""


def resolution(item):
    res = str(item.get("resolution") or "")
    return {"4k": "4K", "sd": "SD"}.get(res.lower(), res + "p" if res.isdigit() else res)


def kicker(item):
    """The small line above a title."""
    kind = item.get("type")
    if kind == "movie":
        return "MOVIE"
    if kind == "show":
        return "TV SHOW"
    if kind == "season":
        return ("%s  ·  %s" % (season_name(item), item.get("show") or "")).upper().strip(" ·")
    if kind == "episode":
        season, index = item.get("season"), item.get("episode")
        if index is None:
            return "EPISODE"
        name = "SPECIALS" if season == 0 else ("SEASON %s" % season if season is not None else "")
        return ("%s  ·  EPISODE %s" % (name, index)).strip(" ·")
    return ""


def meta(item):
    """The facts line under a title."""
    kind = item.get("type")
    if kind == "show":
        seasons = item.get("child_count")
        episodes = item.get("leaf_count")
        left = left_count(item)
        return _join([
            str(item.get("year") or ""),
            ("%d seasons" % seasons if seasons != 1 else "1 season") if seasons else "",
            "%d episodes" % episodes if episodes else "",
            ("%d to watch" % left) if left and left != episodes else ("watched" if episodes and not left else ""),
            item.get("content_rating"), rating(item),
        ])
    if kind == "season":
        episodes = item.get("leaf_count")
        left = left_count(item)
        return _join(["%d episodes" % episodes if episodes else "",
                      "%d to watch" % left if left else ("watched" if episodes else "")])
    progress = ("%s left" % duration((item.get("duration_ms") or 0) - item["offset_ms"])) if in_progress(item) else ""
    if kind == "episode":
        return _join([episode_code(item), duration(item.get("duration_ms")), progress,
                      "watched" if item.get("watched") else "", item.get("content_rating")])
    return _join([str(item.get("year") or ""), duration(item.get("duration_ms")), progress,
                  item.get("content_rating"), rating(item), resolution(item),
                  "watched" if item.get("watched") else ""])


def people(item):
    directors = item.get("directors") or []
    cast = item.get("cast") or []
    return _join([("Directed by " + ", ".join(directors[:2])) if directors else "",
                  ("Starring " + ", ".join(cast[:4])) if cast else ""])


def genres(item):
    return "  ·  ".join((item.get("genres") or [])[:4])


def card_labels(item):
    """(label, label2) for a poster in a row or grid."""
    kind = item.get("type")
    if kind == "episode":
        return item.get("show") or item.get("title", ""), _join([episode_code(item), item.get("title")])
    if kind == "season":
        return item.get("show") or item.get("title", ""), season_name(item)
    if kind == "show":
        left = left_count(item)
        return item.get("title", ""), _join([str(item.get("year") or ""), "%d to watch" % left if left else ""])
    return item.get("title", ""), _join([str(item.get("year") or ""), duration(item.get("duration_ms"))])


def focus_title(item):
    """The big title over the rows for the focused card."""
    if item.get("type") == "episode":
        return "%s  ·  %s" % (item.get("show") or "", item.get("title") or "")
    return item.get("title", "")


def episode_label(item):
    index = item.get("episode")
    return ("%s. %s" % (index, item.get("title", ""))) if index is not None else item.get("title", "")


def play_label(item):
    """The main button for a movie or an episode: resume where it stopped."""
    if in_progress(item):
        return "Resume %s" % clock(item["offset_ms"])
    return "Play"


def pick_season(seasons, season_key=None, up_next=None):
    """Which season a show opens on: the one asked for, else the one with the
    next episode, else the first that isn't Specials."""
    keys = [s.get("key") for s in seasons]
    for wanted in (season_key, (up_next or {}).get("parent")):
        if wanted and wanted in keys:
            return keys.index(wanted)
    for i, s in enumerate(seasons):
        if s.get("season") != 0:
            return i
    return 0


def pick_episode(episodes, episode_key=None, up_next=None):
    """Which episode is selected: the one asked for, else up next, else the
    first not watched yet, else the first."""
    keys = [e.get("key") for e in episodes]
    for wanted in (episode_key, (up_next or {}).get("key")):
        if wanted and wanted in keys:
            return keys.index(wanted)
    for i, e in enumerate(episodes):
        if not e.get("watched"):
            return i
    return 0


def season_by_number(seasons, number):
    """The position of season `number` (0: Specials), or None."""
    for i, s in enumerate(seasons):
        if s.get("season") == number:
            return i
    return None


def search_rows(groups):
    """couchd's search groups -> [(title, items)], best group first."""
    return [(g.get("title") or "", g.get("items") or []) for g in groups if g.get("items")]


def hub_rows(hubs):
    return [(h.get("title") or "", h.get("items") or []) for h in hubs if h.get("items")]


def letter_groups(letters):
    """couchd's letters -> [(label, start)]: A-Z as they are, everything else
    (digits, accents, punctuation) under "#" at its first position."""
    out = []
    other = None
    for g in letters:
        c = g.get("letter") or ""
        if len(c) == 1 and "A" <= c <= "Z":
            out.append((c, g.get("start", 0)))
        elif other is None or g.get("start", 0) < other:
            other = g.get("start", 0)
    if other is not None:
        out.insert(0, ("#", other))
        out.sort(key=lambda pair: pair[1])
    return out


def jump_target(groups, letter):
    """The start of `letter`, else of the next letter there is, else the last."""
    letter = (letter or "").upper()
    if not groups:
        return None
    if not ("A" <= letter <= "Z"):
        return dict(groups).get("#", groups[0][1])
    for label, start in groups:
        if label != "#" and label >= letter:
            return start
    return groups[-1][1]

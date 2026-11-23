"""The movies to watch: a shared Reminders list on the phone, matched to the
Plex index here, and shown on the TV's dashboard (dashd's widget "watchlist")
when a film is in the library.

The phone sends the whole list of open reminders (POST /api/watchlist); it
replaces the one kept here. A reminder is one film:

  title  the film's name, the year optional      "The Matrix (1999)"
  notes  a link to it                            https://www.imdb.com/title/tt0133093/

Links (in the notes, the reminder's URL or the title) are matched first,
exactly: IMDb (tt0133093) and TMDB (themoviedb.org/movie/603) by Plex's own
IDs. A Letterboxd link (letterboxd.com/film/the-matrix/, or the app's
boxd.it short link, followed once) gives a title. Without an ID, the title
and year match the library's title or original title. Page titles as the
Share Sheet gives them ("The Matrix (1999) - IMDb", "‎Dune (2021) •
Letterboxd") are fine: the site's part is dropped.

The list is matched again whenever the index changes (a sync), so a film
added to Plex later shows up by itself. dashd gets only the films in the
library, in the list's order, and only when that changes. IMDb's rating is
only on an entry's own page in Plex: it is fetched once per film, after the
list is on the TV, and kept a week.

Standard library only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("couchd.watchlist")

MAX_ENTRIES = 200
MAX_TEXT = 1000
TICK = 60  # seconds between checks for a changed index or a failed push
IMDB_MAX_AGE = 7 * 86400
IMDB_PER_TICK = 5  # IMDb ratings fetched per tick, so a long list doesn't burst Plex
POSTER = (200, 300)  # within Kodi's 540 px texture limit

IMDB_ID = re.compile(r"\b(tt\d{7,9})\b")
TMDB_ID = re.compile(r"themoviedb\.org/movie/(\d+)", re.I)
LETTERBOXD = re.compile(r"letterboxd\.com/(?:[\w-]+/)?film/([\w-]+)", re.I)
BOXD = re.compile(r"https?://boxd\.it/\w+", re.I)
URL = re.compile(r"https?://\S+", re.I)
YEAR = re.compile(r"\((\d{4})\)")
# What follows the film's name in a page title: "- IMDb", "• Letterboxd",
# "— The Movie Database (TMDB)", IMDb's "⭐ 8.7 | Action, Sci-Fi",
# Letterboxd's "directed by ...". Only these: a title has dashes of its own
# ("Spider-Man", "Mission: Impossible - Dead Reckoning").
SITE_TAIL = re.compile(
    r"\s*(?:⭐.*|\s[-–—|•·]\s+(?:IMDb|Letterboxd|TMDB|The Movie Database|Rotten Tomatoes|Reviews)\b.*"
    r"|\s+directed by\s.*|\s+on Letterboxd\b.*)$", re.I)
INVISIBLE = dict.fromkeys(map(ord, "‎‏​⁠﻿"))


def fold(text: str) -> str:
    """A title for comparing: accents, case, punctuation and a leading
    article don't count. "Amélie" == "amelie", "The Thing" == "thing"."""
    text = unicodedata.normalize("NFKD", text.translate(INVISIBLE))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("&", " and ")
    words = re.findall(r"[^\W_]+", text)
    if len(words) > 1 and words[0] in ("the", "a", "an"):
        words = words[1:]
    return " ".join(words)


def parse(entry: dict) -> dict:
    """What a reminder says about its film: {"imdb", "tmdb", "letterboxd",
    "boxd", "title", "year"} (only what it has)."""
    title = str(entry.get("title") or "").translate(INVISIBLE).strip()
    text = " ".join(str(entry.get(k) or "") for k in ("notes", "url", "title"))
    out: dict = {}
    if m := IMDB_ID.search(text):
        out["imdb"] = m.group(1)
    if m := TMDB_ID.search(text):
        out["tmdb"] = m.group(1)
    if m := LETTERBOXD.search(text):
        out["letterboxd"] = m.group(1).lower()
    elif m := BOXD.search(text):
        out["boxd"] = m.group(0)
    if URL.fullmatch(title):
        title = ""  # the Share Sheet gave the link as the name
    year = YEAR.search(title)
    if year:
        out["year"] = int(year.group(1))
        name = title[:year.start()]
    else:
        name = SITE_TAIL.sub("", title)
        if m := re.search(r"\s(\d{4})$", name):  # "Dune 2021"
            out["year"], name = int(m.group(1)), name[:m.start()]
    name = name.strip(" -–—:")
    if name:
        out["title"] = name
    return out


def slug_title(slug: str) -> tuple[str, int | None]:
    """letterboxd.com/film/the-thing-1982/ -> ("the thing", 1982)."""
    m = re.fullmatch(r"(.+)-(\d{4})", slug)
    if m and 1880 <= int(m.group(2)) <= 2100:
        return m.group(1).replace("-", " "), int(m.group(2))
    return slug.replace("-", " "), None


def follow(url: str, timeout: float = 6) -> str | None:
    """Where a short link points (boxd.it -> letterboxd.com/film/...)."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "couchd"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as exc:  # letterboxd.com may refuse the HEAD; the redirect is known
        return exc.geturl() or None
    except (OSError, ValueError):
        return None


class Library:
    """The index's movies, looked up by ID and by title (one per film: a
    film in two libraries is the first one found)."""

    def __init__(self, movies: list[dict]):
        self.by_id: dict[tuple[str, str], dict] = {}
        self.by_title: dict[str, list[dict]] = {}
        for movie in movies:
            for scheme, value in (movie.get("ids") or {}).items():
                self.by_id.setdefault((scheme, value), movie)
            names = {fold(movie.get("title") or ""), fold(movie.get("original_title") or "")} - {""}
            for name in names:
                same = self.by_title.setdefault(name, [])
                if not any(_same_film(movie, other) for other in same):
                    same.append(movie)

    def find(self, want: dict) -> tuple[dict | None, str | None]:
        """The film a reminder means, and how it was found."""
        for scheme in ("imdb", "tmdb"):
            if want.get(scheme) and (movie := self.by_id.get((scheme, want[scheme]))):
                return movie, scheme
        if not want.get("title"):
            return None, None
        found = self.by_title.get(fold(want["title"]), [])
        year = want.get("year")
        if year:
            for slack in (0, 1):  # release years differ by one between databases
                near = [m for m in found if m.get("year") and abs(m["year"] - year) <= slack]
                if len(near) == 1:
                    return near[0], "title"
            return None, None
        return (found[0], "title") if len(found) == 1 else (None, None)


def _same_film(a: dict, b: dict) -> bool:
    ids_a, ids_b = a.get("ids") or {}, b.get("ids") or {}
    if any(ids_a.get(k) and ids_a.get(k) == ids_b.get(k) for k in ("imdb", "tmdb")):
        return True
    return fold(a.get("title") or "") == fold(b.get("title") or "") and a.get("year") == b.get("year")


def post_json(url: str, body: dict, timeout: float = 5) -> None:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        resp.read()


class Watchlist:
    def __init__(self, catalog, state_path: Path, dashd_url: str, art_base: str,
                 push: Callable[[str, dict], None] = post_json, follow_link: Callable[[str], str | None] = follow):
        self.catalog = catalog
        self.path = state_path
        self.widget_url = dashd_url.rstrip("/") + "/v1/widgets/watchlist"
        self.art_base = art_base.rstrip("/")
        self.push = push
        self.follow_link = follow_link
        self.lock = threading.Lock()  # the state below
        self.refreshing = threading.Lock()  # one refresh at a time (API thread and the loop)
        self.wake = threading.Event()
        state = self._load()
        self.entries: list[dict] = state.get("entries", [])
        self.imdb: dict[str, dict] = state.get("imdb", {})  # Plex key -> {"value", "at"}
        self.links: dict[str, str] = state.get("links", {})  # boxd.it -> letterboxd.com
        self.unreachable: set[str] = set()  # short links that led nowhere (this run)
        self.updated_at: float | None = state.get("updated_at")
        self.results: list[dict] = []
        self.pushed: list[dict] | None = None  # None: not yet since start (push once)
        self.indexed_at = -1.0

    # -- the list

    def set(self, entries: object) -> dict:
        if not isinstance(entries, list):
            raise ValueError("items must be a list")
        clean = []
        for e in entries[:MAX_ENTRIES]:
            if not isinstance(e, dict):
                continue
            item = {k: str(e.get(k) or "")[:MAX_TEXT] for k in ("id", "title", "notes", "url")}
            if item["title"] or item["notes"] or item["url"]:
                clean.append(item)
        with self.lock:
            self.entries = clean
            self.updated_at = time.time()
            self._save()
        # At once, from what is known; new short links are followed behind.
        self.refresh(follow=False)
        self.wake.set()
        return self.status()

    def status(self) -> dict:
        with self.lock:
            results = list(self.results)
        return {
            "items": [{k: r[k] for k in ("id", "title", "year", "available", "key", "via", "matched", "watched")
                       if r.get(k) is not None} for r in results],
            "shown": sum(1 for r in results if r["available"]),
            "updated_at": self.updated_at,
            "on_tv": self.pushed is not None,
        }

    # -- matching and the TV

    def start(self) -> None:
        threading.Thread(target=self._loop, name="watchlist", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                indexed = self.catalog.synced_at()
                if indexed != self.indexed_at or self.pushed is None or self._unfollowed():
                    self.indexed_at = indexed
                    self.refresh()
                if self._fetch_imdb():
                    self.refresh()
            except Exception:
                log.exception("watch list failed")
            self.wake.wait(TICK)
            self.wake.clear()

    def refresh(self, follow: bool = True) -> None:
        """Match the list to the index again, and update the TV if that changed.
        follow: look up new boxd.it short links first (a network call each)."""
        with self.refreshing:
            self._refresh(follow)

    def _refresh(self, follow: bool) -> None:
        library = Library(self.catalog.movies())
        with self.lock:
            entries = list(self.entries)
        if follow:
            self._follow_short_links(entries)
        results = []
        for entry in entries:
            want = parse(entry)
            if want.get("boxd") and (target := self.links.get(want["boxd"])):
                if m := LETTERBOXD.search(target):
                    want["letterboxd"] = m.group(1).lower()
            if want.get("letterboxd") and not want.get("title"):
                want["title"], year = slug_title(want["letterboxd"])
                if year and not want.get("year"):
                    want["year"] = year
            movie, via = library.find(want)
            results.append({
                "id": entry["id"], "title": want.get("title") or entry["title"], "year": want.get("year"),
                "available": movie is not None, "via": via, "movie": movie,
                "key": movie["key"] if movie else None,
                "matched": {"title": movie.get("title"), "year": movie.get("year")} if movie else None,
                "watched": bool(movie.get("watched")) if movie else None,
            })
        with self.lock:
            self.results = results
        self._push()

    def tv_items(self) -> list[dict]:
        """What dashd shows: the films in the library, in the list's order, once each."""
        items, seen = [], set()
        with self.lock:
            results = list(self.results)
        for r in results:
            movie = r["movie"]
            if not movie or movie["key"] in seen:
                continue
            seen.add(movie["key"])
            ratings = dict(movie.get("ratings") or {})
            if (imdb := self.imdb.get(movie["key"])) and imdb.get("value") is not None:
                ratings["imdb"] = imdb["value"]
            item = {"key": movie["key"], "title": movie.get("title") or "", "year": movie.get("year"),
                    "directors": (movie.get("directors") or [])[:2], "ratings": ratings,
                    "watched": bool(movie.get("watched"))}
            if movie.get("poster"):
                query = urllib.parse.urlencode({"path": movie["poster"], "w": POSTER[0], "h": POSTER[1]})
                item["poster"] = f"{self.art_base}/api/plex/art?{query}"
            items.append(item)
        return items

    def _push(self) -> None:
        items = self.tv_items()
        if items == self.pushed:
            return
        try:
            self.push(self.widget_url, {"op": "set", "items": items})
        except (OSError, ValueError) as exc:
            log.info("watch list: dashd not reachable yet (%s); trying again", exc)
            self.pushed = None
            return
        if self.pushed is None or len(items) != len(self.pushed):
            log.info("watch list: %d of %d films on the TV", len(items), len(self.results))
        self.pushed = items

    def _fetch_imdb(self) -> bool:
        """IMDb ratings for films on the TV that have none (or an old one)."""
        now, fetched = time.time(), 0
        for item in self.pushed or []:
            key = item["key"]
            known = self.imdb.get(key)
            if known and now - known.get("at", 0) < IMDB_MAX_AGE:
                continue
            if fetched == IMDB_PER_TICK:
                break
            fetched += 1
            try:
                ratings = self.catalog.plex_ratings(key)
            except Exception as exc:  # Plex down: try again next tick
                log.debug("watch list: no ratings for %s (%s)", key, exc)
                break
            value = next((r.get("value") for r in ratings if str(r.get("image", "")).startswith("imdb://")), None)
            with self.lock:
                self.imdb[key] = {"value": value, "at": now}
                self._save()
        return fetched > 0

    def _short_links(self, entries: list[dict]) -> set[str]:
        """boxd.it links in the list not looked up yet."""
        found = {m.group(0) for e in entries if (m := BOXD.search(" ".join(e.values())))}
        return found - self.links.keys()

    def _unfollowed(self) -> bool:
        with self.lock:
            entries = list(self.entries)
        return bool(self._short_links(entries) - self.unreachable)

    def _follow_short_links(self, entries: list[dict]) -> None:
        for link in self._short_links(entries) - self.unreachable:
            target = self.follow_link(link)
            if target and LETTERBOXD.search(target):
                with self.lock:
                    self.links[link] = target
                    self._save()
            else:
                self.unreachable.add(link)  # not again until couchd restarts

    # -- kept across restarts

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        """Called with self.lock held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": self.entries, "imdb": self.imdb, "links": self.links,
                                   "updated_at": self.updated_at}))
        tmp.replace(self.path)

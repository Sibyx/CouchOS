"""Plex catalog for couchd: a local index of the Plex libraries, and artwork.

couchd browses the Plex server that PM4K (Plex on the TV) is signed in to.
PM4K's settings hold the account, every known server with its connections and
that server's own access token, and the selected server, so the phone needs
no login and the token never leaves the Pi.

Asking the server for every screen made the phone wait on it (and time out),
and Plex's own search matches whole words only. So a background worker copies
the libraries into SQLite on the Pi, with an FTS5 index for search: prefixes,
accents ignored ("zlt" finds "Žltý"), ranked by title, then show, then cast,
genres and year. The phone reads the copy. Posters are cached on disk in a few
fixed sizes, with a size limit (least recently used go first).

Keeping the copy current, in two lanes so a long library sync never holds up
what the phone asked for:

* sync lane
  - full: every movie, show, season and episode of a library, in pages; what
    the server no longer has is dropped. At start when the index is empty or
    older than FULL_INTERVAL, and then every FULL_INTERVAL.
  - quick: per library, the most recently updated and most recently watched
    entries, newest first, until they are older than the last sync. New
    episodes and watched state show up within QUICK_INTERVAL.
* item lane
  - refresh one entry and everything under it (a show's seasons and episodes),
    on request; the phone also asks for a fresh copy of what it opens.
  - fetch or drop the artwork of an entry, a show, or a whole library.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path

log = logging.getLogger("couchd.plex")

_STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "couchd"
_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "couchd"
PM4K_SETTINGS = Path(os.environ.get(
    "COUCHD_PM4K_SETTINGS", str(Path.home() / ".kodi/userdata/addon_data/script.plexmod/settings.xml")))
CATALOG_DB = Path(os.environ.get("COUCHD_CATALOG_DB", str(_STATE / "catalog.sqlite")))
ART_DIR = Path(os.environ.get("COUCHD_ART_DIR", str(_CACHE / "art")))
ART_LIMIT = int(float(os.environ.get("COUCHD_ART_CACHE_MB", "400")) * 1024 * 1024)
# Whatever the limit, the disk keeps this much free: below it the oldest
# artwork goes (and no new artwork is saved) until it's back above.
DISK_RESERVE = int(float(os.environ.get("COUCHD_DISK_RESERVE_MB", "2048")) * 1024 * 1024)
DISK_CHECK_EVERY = 30  # seconds between free-space checks while artwork is saved
QUICK_INTERVAL = float(os.environ.get("COUCHD_CATALOG_QUICK", "300"))
FULL_INTERVAL = float(os.environ.get("COUCHD_CATALOG_FULL", str(12 * 3600)))
PLEX_TIMEOUT = float(os.environ.get("COUCHD_PLEX_TIMEOUT", "8"))
SYNC_TIMEOUT = 60  # one page of a library listing (200 entries) can take a while
FRESH_TIMEOUT = 4  # the phone opened an entry: wait this long for the server, else show the copy
RETRY_AFTER = 300  # a failed sync is retried after this long
PAGE = 200
HUBS_TTL = 60
SCHEMA = "1"

TYPES = ("movie", "show", "season", "episode")
PLEX_TYPE = {"movie": 1, "show": 2, "season": 3, "episode": 4}
# Library type -> what its listing holds, top level first.
SECTION_TYPES = {"movie": ("movie",), "show": ("show", "season", "episode")}

# Artwork is cached in these sizes only, so the phone's requests (which ask for
# whatever width a view needs) land on a few files. The smallest size at least
# as wide as asked; the largest when nothing is.
POSTER_SIZES = ((160, 240), (320, 480), (640, 960))
WIDE_SIZES = ((320, 180), (640, 360), (1280, 720))
LIST_POSTER = (320, 480)  # what lists and grids show; what "download artwork" fetches
LIST_STILL = (320, 180)
ART_PATH = re.compile(r"/library/[\w/]+")
# Requests to the server at once for artwork (a grid asks for dozens together).
UPSTREAM = threading.BoundedSemaphore(4)


class PlexError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


# --- server ----------------------------------------------------------------------


class PlexClient:
    """The server PM4K uses, through a connection that answers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (settings mtime, base url, token, server name, server uuid)
        self._conn: tuple[float, str, str, str, str] | None = None
        self.client_id = f"couchd-{os.uname().nodename}"

    @staticmethod
    def _settings() -> dict[str, str]:
        try:
            root = ET.parse(PM4K_SETTINGS).getroot()
        except (OSError, ET.ParseError) as exc:
            raise PlexError(HTTPStatus.SERVICE_UNAVAILABLE, f"Plex is not set up on the TV yet ({exc})") from exc
        return {s.get("id", ""): s.text or "" for s in root.iter("setting")}

    def _candidates(self) -> list[tuple[str, str, str, str]]:
        """(base url, token, server name, uuid): the selected server first, LAN first."""
        settings = self._settings()
        try:
            account = json.loads(settings.get("myplex.MyPlexAccount") or "{}")
            manager = json.loads(settings.get("None.PlexServerManager") or settings.get("PlexServerManager") or "{}")
        except ValueError as exc:
            raise PlexError(HTTPStatus.SERVICE_UNAVAILABLE, f"cannot read PM4K's Plex sign-in: {exc}") from exc
        selected = settings.get(f"lastServerId.{account.get('ID')}")
        servers = sorted(manager.get("servers") or [], key=lambda s: (s.get("uuid") != selected, not s.get("owned")))
        out = []
        for server in servers:
            for conn in sorted(server.get("connections") or [], key=lambda c: not c.get("isLocal")):
                address = conn.get("address") or ""
                token = conn.get("token") or account.get("authToken") or ""
                if address.startswith("http") and not address.endswith(":None") and token:
                    out.append((address.rstrip("/"), token, server.get("name") or "Plex", server.get("uuid") or ""))
        if not out:
            raise PlexError(HTTPStatus.SERVICE_UNAVAILABLE, "PM4K has no Plex server yet: sign in on the TV first")
        return out

    def connection(self, fresh: bool = False) -> tuple[str, str, str, str]:
        try:
            mtime = PM4K_SETTINGS.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._lock:
            if self._conn and not fresh and self._conn[0] == mtime:
                return self._conn[1:]
            candidates = self._candidates()
            if self._conn and not fresh and self._conn[1:] in candidates:
                # Kodi rewrote PM4K's settings, but the connection in use is
                # still one of them: keep it.
                self._conn = (mtime, *self._conn[1:])
                return self._conn[1:]
            # All at once: PM4K lists LAN addresses that may not answer from
            # here, and trying them in turn (3 s each) held couchd up ~30 s.
            results: list[str | None] = [None] * len(candidates)

            def probe(i: int, base: str, token: str) -> None:
                try:
                    self._request(base, token, "/identity", {}, timeout=3)
                    results[i] = "ok"
                except (OSError, PlexError) as exc:
                    results[i] = f"{urllib.parse.urlsplit(base).netloc}: {exc}"

            threads = [threading.Thread(target=probe, args=(i, c[0], c[1]), daemon=True) for i, c in enumerate(candidates)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(4)
            for result, (base, token, name, uuid) in zip(results, candidates, strict=True):
                if result == "ok":  # the most preferred one that answered
                    log.info("Plex: using %s (%s)", name, urllib.parse.urlsplit(base).netloc)
                    self._conn = (mtime, base, token, name, uuid)
                    return base, token, name, uuid
            errors = [r or "no answer" for r in results]
            raise PlexError(HTTPStatus.BAD_GATEWAY, "no Plex server connection answers: " + "; ".join(errors))

    def server(self) -> dict:
        _, _, name, uuid = self.connection()
        return {"name": name, "uuid": uuid}

    def _request(self, base: str, token: str, path: str, params: dict, timeout: float,
                 accept: str = "application/json") -> tuple[bytes, str]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{base}{path}" + (f"?{query}" if query else ""), headers={
            "Accept": accept,
            "X-Plex-Token": token,
            "X-Plex-Product": "CouchOS",
            "X-Plex-Client-Identifier": self.client_id,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status = HTTPStatus.NOT_FOUND if exc.code == 404 else HTTPStatus.BAD_GATEWAY
            raise PlexError(status, f"Plex {path}: HTTP {exc.code}") from exc

    def request(self, path: str, params: dict | None = None, timeout: float = PLEX_TIMEOUT,
                accept: str = "application/json", retry: bool = True) -> tuple[bytes, str]:
        base, token, _, _ = self.connection()
        try:
            return self._request(base, token, path, params or {}, timeout, accept)
        except OSError as exc:
            if not retry:
                raise PlexError(HTTPStatus.GATEWAY_TIMEOUT, f"Plex {path}: {exc}") from exc
            # The connection went away (left the LAN, server restarted): find another.
            base, token, _, _ = self.connection(fresh=True)
            try:
                return self._request(base, token, path, params or {}, timeout, accept)
            except OSError as exc2:
                raise PlexError(HTTPStatus.GATEWAY_TIMEOUT, f"Plex {path}: {exc2}") from exc2

    def get(self, path: str, params: dict | None = None, timeout: float = PLEX_TIMEOUT, retry: bool = True) -> dict:
        data, _ = self.request(path, params, timeout=timeout, retry=retry)
        try:
            # "replace": Plex passes file names through as they are on disk,
            # and one stored in Latin-1 ("L\xe1ska na vl\xe1sku") makes the
            # whole page invalid UTF-8. Only those paths lose a character.
            return json.loads(data.decode("utf-8", "replace")).get("MediaContainer") or {}
        except ValueError as exc:
            raise PlexError(HTTPStatus.BAD_GATEWAY, f"Plex {path}: not JSON") from exc


# --- entries -----------------------------------------------------------------------


def _tags(m: dict, name: str) -> list[str]:
    return [t["tag"] for t in m.get(name) or [] if t.get("tag")]


def _guids(kind: str) -> int:
    """includeGuids for a library listing: movies only (the watch list needs
    them). For all ~58,000 episodes it made pages slow enough that a full
    sync of the TV shows timed out (2026-11-23)."""
    return 1 if kind == "movie" else 0


def _ids(m: dict) -> dict:
    """External IDs from Plex's Guid list (asked for with includeGuids=1):
    {"imdb": "tt0133093", "tmdb": "603"}. The watch list matches on them."""
    ids = {}
    for g in m.get("Guid") or []:
        scheme, _, value = str(g.get("id") or "").partition("://")
        if scheme in ("imdb", "tmdb") and value:
            ids[scheme] = value
    return ids


def _source(image: str | None) -> str | None:
    """"rottentomatoes://image.rating.ripe" -> "rottentomatoes"."""
    return image.split("://", 1)[0] if image and "://" in image else None


def _ratings(m: dict) -> dict:
    """Both of Plex's scores (0-10) with where each comes from: the critic
    one is Rotten Tomatoes' for most movies, the audience one Rotten
    Tomatoes', IMDb's or TMDB's, depending on the agent."""
    out = {}
    for kind, value, image in (("critic", m.get("rating"), m.get("ratingImage")),
                               ("audience", m.get("audienceRating"), m.get("audienceRatingImage"))):
        if value is not None:
            out[kind] = value
            if _source(image):
                out[f"{kind}_source"] = _source(image)
    return out


def normalize(m: dict) -> dict:
    """The fields the phone shows (and the index needs), from one Plex entry."""
    kind = m.get("type")
    media = (m.get("Media") or [{}])[0]
    leaf, viewed_leaf = m.get("leafCount"), m.get("viewedLeafCount")
    if kind in ("movie", "episode"):
        watched = (m.get("viewCount") or 0) > 0
    else:
        watched = bool(leaf) and viewed_leaf is not None and viewed_leaf >= leaf
    item = {
        "key": str(m.get("ratingKey") or ""),
        "type": kind,
        "title": m.get("title") or "",
        "original_title": m.get("originalTitle"),
        "sort_title": m.get("titleSort"),
        "year": m.get("year"),
        "summary": m.get("summary"),
        # An episode's own thumb is a 16:9 still; its show's is a poster.
        "thumb": m.get("thumb"),
        "poster": (m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb")) if kind == "episode"
        else (m.get("thumb") or m.get("parentThumb")),
        "art": m.get("art") or m.get("grandparentArt") or m.get("parentArt"),
        "duration_ms": m.get("duration"),
        "offset_ms": m.get("viewOffset"),
        "watched": watched,
        "show": m.get("grandparentTitle") if kind == "episode" else m.get("parentTitle") if kind == "season" else None,
        "season": m.get("parentIndex") if kind == "episode" else m.get("index") if kind == "season" else None,
        "episode": m.get("index") if kind == "episode" else None,
        "parent": str(m["parentRatingKey"]) if m.get("parentRatingKey") else None,
        "grandparent": str(m["grandparentRatingKey"]) if m.get("grandparentRatingKey") else None,
        "leaf_count": leaf,
        "viewed_leaf_count": viewed_leaf,
        "child_count": m.get("childCount"),
        "content_rating": m.get("contentRating"),
        "rating": m.get("audienceRating") or m.get("rating"),
        "ratings": _ratings(m),
        "ids": _ids(m),
        "genres": _tags(m, "Genre"),
        "directors": _tags(m, "Director")[:3],
        "cast": _tags(m, "Role")[:8],
        "studio": m.get("studio"),
        "video_codec": media.get("videoCodec"),
        "resolution": media.get("videoResolution"),
        "added_at": m.get("addedAt"),
        "updated_at": m.get("updatedAt"),
        "viewed_at": m.get("lastViewedAt"),
    }
    return {k: v for k, v in item.items() if v not in (None, "", [], {})}


def _unwatched(item: dict) -> int:
    if item["type"] in ("movie", "episode"):
        return 0 if item.get("watched") else 1
    leaf = item.get("leaf_count") or 0
    return 1 if leaf - (item.get("viewed_leaf_count") or 0) > 0 else 0


def _fold(text: str) -> str:
    """Lowercase without accents, as the search index sees it."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_GROUP_WEIGHT = {"movie": 0, "show": 0, "episode": 1}


def _relevance(item: dict, wanted: str, score: float) -> tuple:
    """Sort key, best first:
    0 the title is the query          "the office" -> The Office
    1 it starts with the query words  "office" -> Office Space
    2 it holds the query as words     "office" -> The Office
    3 it starts with it, mid-word     "office" -> Officer Down
    4 it contains it anywhere
    5 the match is elsewhere (cast, genre, show)
    then movies and shows before episodes, the index's own score (bm25:
    lower is better) and shorter titles."""
    title = _fold(item.get("title", ""))
    words = " ".join(re.findall(r"\w+", title))
    # "office" is The Office: a leading article doesn't count.
    bare = re.sub(r"^(the|a|an) ", "", words)
    if wanted in (words, bare):
        place = 0
    elif f"{words} ".startswith(f"{wanted} ") or f"{bare} ".startswith(f"{wanted} "):
        place = 1
    elif f" {wanted} " in f" {words} ":
        place = 2
    elif words.startswith(wanted):
        place = 3
    elif wanted in words:
        place = 4
    else:
        place = 5
    return place, _GROUP_WEIGHT.get(item.get("type"), 0), round(score, 1), len(title)


def _search_text(item: dict) -> str:
    parts = [item.get("original_title"), item.get("year"), item.get("studio"),
             *item.get("genres", []), *item.get("cast", []), *item.get("directors", [])]
    return " ".join(str(p) for p in parts if p)


# --- catalog -----------------------------------------------------------------------


class Catalog:
    """The index, the artwork cache, the workers that keep them current, and
    the queries the phone makes."""

    SORTS = {
        "title": "sort_title COLLATE NOCASE",
        "added": "added_at DESC",
        "year": "year DESC, sort_title COLLATE NOCASE",
        "rating": "rating DESC, sort_title COLLATE NOCASE",
        "watched": "viewed_at DESC",
    }

    def __init__(self, client: PlexClient, on_change: Callable[[], None] = lambda: None) -> None:
        self.client = client
        self.on_change = on_change
        CATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
        ART_DIR.mkdir(parents=True, exist_ok=True)
        self._db_lock = threading.RLock()
        self.db = sqlite3.connect(CATALOG_DB, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._schema()
        # Reads use their own connections: in WAL mode they don't wait for a
        # sync writing a page (which held searches up for seconds), nor for
        # each other (one shared reader made every phone and TV request
        # queue behind the slowest search). A few, reused.
        self._readers: queue.SimpleQueue = queue.SimpleQueue()
        self._lanes = {"sync": queue.Queue(), "items": queue.Queue()}
        self._queued: set[tuple] = set()
        self._queued_lock = threading.Lock()
        # What each lane is doing now: {"kind", "label", "done", "total"}
        self.jobs: dict[str, dict | None] = {"sync": None, "items": None}
        self.error: str | None = None
        self._failed_at = 0.0
        self._hubs: tuple[float, list] | None = None
        self._hubs_refreshing = False
        self._art_lock = threading.Lock()
        self._inflight: dict[Path, threading.Event] = {}
        # Running totals of the artwork cache (None until the first scan),
        # kept on every write and removal so the limit holds without rescans.
        self._art_files: int | None = None
        self._art_bytes: int | None = None
        self._evicting = threading.Lock()
        self._disk_checked = float("-inf")
        self._disk_low = False

    # -- database

    def _schema(self) -> None:
        with self._db_lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            row = self.db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if row and row[0] != SCHEMA:
                for table in ("items_fts", "items", "sections"):
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.execute("DELETE FROM meta")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sections(
                    key TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL, position INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1, prefetch_art INTEGER NOT NULL DEFAULT 0,
                    synced_at REAL, full_synced_at REAL, status TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS items(
                    key TEXT PRIMARY KEY, section TEXT NOT NULL, parent TEXT, grandparent TEXT,
                    type TEXT NOT NULL, title TEXT NOT NULL, sort_title TEXT, show_title TEXT,
                    idx INTEGER, parent_idx INTEGER, year INTEGER, added_at INTEGER, updated_at INTEGER,
                    viewed_at INTEGER, rating REAL, unwatched INTEGER, extra TEXT,
                    data TEXT NOT NULL, synced_at REAL NOT NULL, gen INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS items_section ON items(section, type);
                CREATE INDEX IF NOT EXISTS items_parent ON items(parent, idx);
                CREATE INDEX IF NOT EXISTS items_grandparent ON items(grandparent, parent_idx, idx);
                -- Short searches ("s"): titles starting with it, per type.
                CREATE INDEX IF NOT EXISTS items_title ON items(type, sort_title COLLATE NOCASE);
            """)
            self._fts()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA,))

    FTS_SQL = """
        CREATE VIRTUAL TABLE items_fts USING fts5(
            title, show_title, extra, type, content='items', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2', prefix='2 3');
        CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
            INSERT INTO items_fts(rowid, title, show_title, extra, type)
            VALUES (new.rowid, new.title, new.show_title, new.extra, new.type);
        END;
        CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
            INSERT INTO items_fts(items_fts, rowid, title, show_title, extra, type)
            VALUES ('delete', old.rowid, old.title, old.show_title, old.extra, old.type);
        END;
        CREATE TRIGGER items_au AFTER UPDATE OF title, show_title, extra, type ON items
        WHEN old.title IS NOT new.title OR old.show_title IS NOT new.show_title OR old.extra IS NOT new.extra
            OR old.type IS NOT new.type
        BEGIN
            INSERT INTO items_fts(items_fts, rowid, title, show_title, extra, type)
            VALUES ('delete', old.rowid, old.title, old.show_title, old.extra, old.type);
            INSERT INTO items_fts(rowid, title, show_title, extra, type)
            VALUES (new.rowid, new.title, new.show_title, new.extra, new.type);
        END;
    """

    def _fts(self) -> None:
        """The search index. Each entry's type is in it too, so a search for
        movies only looks at movies inside the index: before, every group
        scored every match of every type and dropped the others afterwards
        (a one-letter search took ~3 s on the Pi 3). An index without the
        type column is rebuilt from the items in place; nothing is synced
        again from Plex."""
        row = self.db.execute("SELECT sql FROM sqlite_master WHERE name='items_fts'").fetchone()
        if row and "type," in row[0].replace(" ", ""):
            return
        started = time.monotonic()
        with self._db_lock:
            # One transaction: a failure leaves the old index as it was.
            try:
                self.db.executescript(
                    "BEGIN IMMEDIATE;"
                    "DROP TRIGGER IF EXISTS items_ai; DROP TRIGGER IF EXISTS items_ad; DROP TRIGGER IF EXISTS items_au;"
                    "DROP TABLE IF EXISTS items_fts;"
                    + self.FTS_SQL
                    + "INSERT INTO items_fts(items_fts) VALUES ('rebuild'); COMMIT;")
            except sqlite3.Error:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        if row:
            log.info("catalog: search index rebuilt with types in %.1f s", time.monotonic() - started)

    @contextmanager
    def _tx(self):
        with self._db_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        try:
            reader = self._readers.get_nowait()
        except queue.Empty:
            reader = sqlite3.connect(CATALOG_DB, check_same_thread=False, isolation_level=None, timeout=10)
            reader.row_factory = sqlite3.Row
        try:
            return reader.execute(sql, tuple(params)).fetchall()
        finally:
            self._readers.put(reader)

    def _meta(self, key: str) -> str | None:
        rows = self._query("SELECT v FROM meta WHERE k=?", (key,))
        return rows[0][0] if rows else None

    def _set_meta(self, key: str, value: object) -> None:
        with self._db_lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def _upsert(self, section: str, metas: Iterable[dict], gen: int, keep_gen: bool = False) -> int:
        """Write entries; returns how many. keep_gen: an entry that is already
        there keeps its sync generation (only a full sync moves it on), so an
        item refreshed in the middle of a full sync isn't swept away by it."""
        now = time.time()
        rows = []
        for m in metas:
            item = normalize(m)
            if not item.get("key") or item.get("type") not in TYPES:
                continue
            kind = item["type"]
            rows.append((
                item["key"], section, item.get("parent"), item.get("grandparent"), kind, item["title"],
                item.get("sort_title") or item["title"],
                item.get("show") if kind == "episode" else "",
                item.get("episode") if kind == "episode" else item.get("season") if kind == "season" else None,
                item.get("season") if kind == "episode" else None,
                item.get("year"), item.get("added_at"), item.get("updated_at"), item.get("viewed_at"),
                item.get("rating"), _unwatched(item), _search_text(item), json.dumps(item), now, gen,
            ))
        if not rows:
            return 0
        sql = """INSERT INTO items(key, section, parent, grandparent, type, title, sort_title, show_title,
                   idx, parent_idx, year, added_at, updated_at, viewed_at, rating, unwatched, extra, data,
                   synced_at, gen)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(key) DO UPDATE SET section=excluded.section, parent=excluded.parent,
                   grandparent=excluded.grandparent, type=excluded.type, title=excluded.title,
                   sort_title=excluded.sort_title, show_title=excluded.show_title, idx=excluded.idx,
                   parent_idx=excluded.parent_idx, year=excluded.year, added_at=excluded.added_at,
                   updated_at=excluded.updated_at, viewed_at=excluded.viewed_at, rating=excluded.rating,
                   unwatched=excluded.unwatched, extra=excluded.extra, data=excluded.data,
                   synced_at=excluded.synced_at"""
        if not keep_gen:
            sql += ", gen=excluded.gen"
        with self._tx() as db:
            db.executemany(sql, rows)
        return len(rows)

    # -- workers

    def start(self) -> None:
        for lane in self._lanes:
            threading.Thread(target=self._worker, args=(lane,), name=f"catalog-{lane}", daemon=True).start()
        threading.Thread(target=self._scheduler, name="catalog-schedule", daemon=True).start()
        threading.Thread(target=self._scan_art, name="catalog-art-scan", daemon=True).start()

    def enqueue(self, kind: str, arg: str | None = None) -> bool:
        """Queue a job once (a second identical request while it waits is a no-op)."""
        lane = "sync" if kind in ("full", "quick") else "items"
        job = (kind, arg)
        with self._queued_lock:
            if job in self._queued:
                return False
            self._queued.add(job)
        self._lanes[lane].put(job)
        self.on_change()
        return True

    def _scheduler(self) -> None:
        time.sleep(5)  # let couchd finish starting
        while True:
            try:
                now = time.time()
                # Nothing while a sync runs or waits: a full sync takes
                # minutes, and its timestamps only move when it's done.
                busy = self.jobs["sync"] is not None or not self._lanes["sync"].empty()
                if not busy and now - self._failed_at > RETRY_AFTER:
                    full = float(self._meta("full_synced_at") or 0)
                    quick = float(self._meta("synced_at") or 0)
                    if now - full > FULL_INTERVAL:
                        self.enqueue("full")
                    elif now - quick > QUICK_INTERVAL:
                        self.enqueue("quick")
                # Something else may fill the disk (logs, downloads): check
                # even when no artwork is being saved.
                self._check_disk(force=True)
            except Exception:
                log.exception("catalog scheduler")
            time.sleep(20)

    def _worker(self, lane: str) -> None:
        while True:
            kind, arg = self._lanes[lane].get()
            with self._queued_lock:
                self._queued.discard((kind, arg))
            self.jobs[lane] = {"kind": kind, "label": self._label(kind, arg), "done": 0, "total": 0}
            self.on_change()
            started = time.monotonic()
            try:
                getattr(self, f"_job_{kind}")(arg)
                if lane == "sync":
                    self.error = None
                log.log(logging.DEBUG if kind == "quick" else logging.INFO,
                        "catalog: %s %s done in %.1f s", kind, arg or "", time.monotonic() - started)
            except Exception as exc:  # report and keep going
                log.warning("catalog: %s %s failed: %s", kind, arg or "", exc)
                if lane == "sync":
                    self.error = str(exc)
                    self._failed_at = time.time()
            finally:
                self.jobs[lane] = None
                self.on_change()

    def _label(self, kind: str, arg: str | None) -> str:
        if kind in ("full", "quick"):
            name = self._section_title(arg) if arg else "all libraries"
            return f"{'Full sync' if kind == 'full' else 'Checking for changes'}: {name}"
        title = self._section_title(arg) if kind.endswith("section") else self._item_title(arg)
        return {
            "refresh": f"Refreshing {title}",
            "art_fetch": f"Downloading artwork: {title}",
            "art_drop": f"Removing artwork: {title}",
            "art_fetch_section": f"Downloading artwork: {title}",
            "art_drop_section": f"Removing artwork: {title}",
            "art_clear": "Clearing the artwork cache",
        }.get(kind, kind)

    def _progress(self, lane: str, done: int, total: int, label: str | None = None) -> None:
        job = self.jobs.get(lane)
        if job is None:
            return
        # No on_change here: couchd's status stream polls every few seconds
        # and pushes progress in 5 % steps; forcing a rebuild per page would
        # cost the Pi a status (subprocesses, Kodi calls) every page.
        job.update(done=done, total=total)
        if label:
            job["label"] = label

    # -- sync lane

    def _sync_sections(self) -> list[sqlite3.Row]:
        """Bring the library list up to date; returns the enabled libraries."""
        server = self.client.server()
        if self._meta("server") not in (None, server["uuid"]):
            log.info("catalog: Plex server changed to %s; starting over", server["name"])
            self._wipe()
        self._set_meta("server", server["uuid"])
        self._set_meta("server_name", server["name"])
        dirs = self.client.get("/library/sections").get("Directory") or []
        seen = []
        with self._tx() as db:
            for pos, d in enumerate(dirs):
                if d.get("type") not in SECTION_TYPES:
                    continue
                key = str(d.get("key"))
                seen.append(key)
                db.execute("""INSERT INTO sections(key, title, type, position) VALUES (?, ?, ?, ?)
                              ON CONFLICT(key) DO UPDATE SET title=excluded.title, type=excluded.type,
                              position=excluded.position""", (key, d.get("title") or key, d["type"], pos))
            marks = ",".join("?" * len(seen)) or "''"
            db.execute(f"DELETE FROM items WHERE section NOT IN ({marks})", seen)
            db.execute(f"DELETE FROM sections WHERE key NOT IN ({marks})", seen)
        return self._query("SELECT * FROM sections WHERE enabled=1 ORDER BY position")

    def _job_full(self, section: str | None) -> None:
        sections = [s for s in self._sync_sections() if section in (None, s["key"])]
        for s in sections:
            self._full_section(s)
        if section is None:
            now = time.time()
            self._set_meta("full_synced_at", now)
            self._set_meta("synced_at", now)
        self.invalidate_hubs()

    def _full_section(self, s: sqlite3.Row) -> None:
        gen = time.time_ns() // 1_000_000
        self._section_status(s["key"], "syncing")
        try:
            for kind in SECTION_TYPES[s["type"]]:
                start, total = 0, None
                while total is None or start < total:
                    container = self.client.get(f"/library/sections/{s['key']}/all", {
                        "type": PLEX_TYPE[kind], "includeGuids": _guids(kind),
                        "X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE,
                    }, timeout=SYNC_TIMEOUT)
                    metas = container.get("Metadata") or []
                    total = int(container.get("totalSize") or container.get("size") or 0)
                    self._upsert(s["key"], metas, gen)
                    start += len(metas)
                    self._progress("sync", start, total, f"Full sync: {s['title']} · {kind}s")
                    if not metas:
                        break
            now = time.time()
            with self._tx() as db:
                db.execute("DELETE FROM items WHERE section=? AND gen<?", (s["key"], gen))
                db.execute("UPDATE sections SET synced_at=?, full_synced_at=?, status='ok', error=NULL WHERE key=?",
                           (now, now, s["key"]))
        except Exception as exc:
            self._section_status(s["key"], "error", str(exc))
            raise
        if s["prefetch_art"]:
            self.enqueue("art_fetch_section", s["key"])

    def _job_quick(self, section: str | None) -> None:
        for s in self._sync_sections():
            if section not in (None, s["key"]):
                continue
            if not s["full_synced_at"]:
                self.enqueue("full", s["key"])
                continue
            since = (s["synced_at"] or 0) - 120
            gen = time.time_ns() // 1_000_000
            changed = 0
            kinds = SECTION_TYPES[s["type"]]
            for field, field_kinds in (("updatedAt", kinds), ("lastViewedAt", [k for k in kinds if k != "season"])):
                for kind in field_kinds:
                    start = 0
                    while start < 1000:
                        container = self.client.get(f"/library/sections/{s['key']}/all", {
                            "type": PLEX_TYPE[kind], "sort": f"{field}:desc", "includeGuids": _guids(kind),
                            "X-Plex-Container-Start": start, "X-Plex-Container-Size": 50,
                        })
                        metas = container.get("Metadata") or []
                        newer = [m for m in metas if (m.get(field) or 0) >= since]
                        changed += self._upsert(s["key"], newer, gen, keep_gen=True)
                        if len(newer) < len(metas) or not metas:
                            break
                        start += len(metas)
            with self._db_lock:
                self.db.execute("UPDATE sections SET synced_at=?, status='ok', error=NULL WHERE key=?",
                                (time.time(), s["key"]))
            if changed:
                # Counts the 2-minute overlap with the last sync too, so not "new".
                log.debug("catalog: %s: %d updated", s["title"], changed)
                if s["prefetch_art"]:
                    self.enqueue("art_fetch_section", s["key"])
        if section is None:
            self._set_meta("synced_at", time.time())
        self.invalidate_hubs()

    def _section_status(self, key: str, status: str, error: str | None = None) -> None:
        with self._db_lock:
            self.db.execute("UPDATE sections SET status=?, error=? WHERE key=?", (status, error, key))
        self.on_change()

    def _wipe(self) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM sections")
            db.execute("DELETE FROM meta WHERE k NOT IN ('schema')")

    # -- item lane

    def _job_refresh(self, key: str) -> None:
        self.refresh(key, subtree=True)

    def refresh(self, key: str, subtree: bool = False, timeout: float = PLEX_TIMEOUT, retry: bool = True) -> dict | None:
        """Fetch one entry (and, with subtree, everything under it) from the server."""
        # includeGuids on every fetch that is written to the index: a row
        # written without them would lose its IMDb/TMDB IDs (the watch list).
        container = self.client.get(f"/library/metadata/{key}", {"includeGuids": 1}, timeout=timeout, retry=retry)
        found = container.get("Metadata") or []
        if not found:
            with self._tx() as db:
                db.execute("DELETE FROM items WHERE key=? OR parent=? OR grandparent=?", (key, key, key))
            return None
        m = found[0]
        existing = self._query("SELECT section FROM items WHERE key=?", (key,))
        section = str(m.get("librarySectionID") or container.get("librarySectionID")
                      or (existing[0]["section"] if existing else ""))
        gen = time.time_ns() // 1_000_000
        self._upsert(section, [m], gen, keep_gen=True)
        kind = m.get("type")
        if subtree and kind in ("show", "season"):
            children = self.client.get(f"/library/metadata/{key}/children", timeout=SYNC_TIMEOUT).get("Metadata") or []
            leaves = []
            if kind == "show":
                leaves = self.client.get(f"/library/metadata/{key}/allLeaves", timeout=SYNC_TIMEOUT).get("Metadata") or []
            self._upsert(section, children + leaves, gen, keep_gen=True)
            seen = [str(c.get("ratingKey")) for c in children + leaves]
            marks = ",".join("?" * len(seen)) or "''"
            with self._tx() as db:
                db.execute(f"DELETE FROM items WHERE (parent=? OR grandparent=?) AND key NOT IN ({marks})",
                           (key, key, *seen))
        return self.item(key)

    def _section_of(self, key: str) -> str:
        rows = self._query("SELECT section FROM items WHERE key=?", (key,))
        return rows[0]["section"] if rows else ""

    def _subtree(self, key: str) -> list[dict]:
        rows = self._query("SELECT data FROM items WHERE key=? OR parent=? OR grandparent=?", (key, key, key))
        return [json.loads(r["data"]) for r in rows]

    def _section_items(self, section: str) -> list[dict]:
        return [json.loads(r["data"]) for r in self._query("SELECT data FROM items WHERE section=?", (section,))]

    def _job_art_fetch(self, key: str) -> None:
        self._fetch_art_for(self._subtree(key))

    def _job_art_fetch_section(self, section: str) -> None:
        self._fetch_art_for(self._section_items(section))

    def _job_art_drop(self, key: str) -> None:
        self._drop_art_for(self._subtree(key))

    def _job_art_drop_section(self, section: str) -> None:
        self._drop_art_for(self._section_items(section))

    def _job_art_clear(self, _: str | None) -> None:
        for path in list(ART_DIR.rglob("*.jpg")):
            path.unlink(missing_ok=True)
        with self._art_lock:
            self._art_files, self._art_bytes = 0, 0

    @staticmethod
    def _art_listing() -> list[tuple[float, int, Path]]:
        files = []
        for path in ART_DIR.rglob("*.jpg"):
            try:
                st = path.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        return files

    def _scan_art(self) -> None:
        files = self._art_listing()
        with self._art_lock:
            self._art_files, self._art_bytes = len(files), sum(f[1] for f in files)
        if self._art_bytes > ART_LIMIT:
            self._evict()

    @staticmethod
    def disk_short() -> int:
        """Bytes missing from the disk reserve (0 when there's enough free)."""
        try:
            ART_DIR.mkdir(parents=True, exist_ok=True)
            st = os.statvfs(ART_DIR)
        except OSError:
            return 0
        return max(0, DISK_RESERVE - st.f_bavail * st.f_frsize)

    def _check_disk(self, force: bool = False) -> bool:
        """True when the disk is short of its reserve; frees artwork if so."""
        now = time.monotonic()
        if not force and now - self._disk_checked < DISK_CHECK_EVERY:
            return self._disk_low
        self._disk_checked = now
        short = self.disk_short()
        if short and not self._disk_low:
            log.warning("disk below its %d MB reserve: dropping old artwork", DISK_RESERVE // 2**20)
        self._disk_low = bool(short)
        if short:
            self._evict(free=short + DISK_RESERVE // 4)  # a margin, so it doesn't trim per picture
        return self._disk_low

    def _evict(self, free: int = 0) -> None:
        """Drop the least recently used pictures down to 90 % of the limit,
        or further when the disk needs `free` bytes back.
        One at a time: a second caller while it runs has nothing to add."""
        if not self._evicting.acquire(blocking=False):
            return
        try:
            files = self._art_listing()
            total = sum(f[1] for f in files)
            count = len(files)
            target = min(ART_LIMIT * 0.9, total - free) if free else ART_LIMIT * 0.9
            for _, size, path in sorted(files):
                if total <= target:
                    break
                path.unlink(missing_ok=True)
                total -= size
                count -= 1
            with self._art_lock:
                self._art_files, self._art_bytes = count, total
            if count < len(files):
                log.info("artwork cache trimmed to %d files, %.1f MB", count, total / 1e6)
        finally:
            self._evicting.release()

    @staticmethod
    def _list_art(item: dict) -> tuple[str | None, tuple[int, int]]:
        """The picture lists and grids show for an entry, and its size."""
        if item.get("type") == "episode":
            return item.get("thumb"), LIST_STILL
        return item.get("poster"), LIST_POSTER

    def _fetch_art_for(self, items: list[dict]) -> None:
        wanted = [(p, size) for p, size in map(self._list_art, items) if p and not self._art_file(p, size).exists()]
        for n, (path, size) in enumerate(wanted, 1):
            if self._check_disk():
                log.warning("artwork download stopped: the disk is short of space")
                break
            try:
                self.art(path, *size)
            except PlexError as exc:
                log.debug("artwork %s: %s", path, exc)
            self._progress("items", n, len(wanted))

    def _drop_art_for(self, items: list[dict]) -> None:
        paths = {p for item in items for p in (item.get("thumb"), item.get("poster"), item.get("art")) if p}
        files = removed = 0
        for n, path in enumerate(paths, 1):
            for size in POSTER_SIZES + WIDE_SIZES:
                file = self._art_file(path, size)
                try:
                    removed += file.stat().st_size
                    file.unlink()
                    files += 1
                except OSError:
                    pass
            self._progress("items", n, len(paths))
        with self._art_lock:
            if self._art_bytes is not None:
                self._art_files = max(0, (self._art_files or 0) - files)
                self._art_bytes = max(0, self._art_bytes - removed)

    # -- artwork

    @staticmethod
    def _bucket(width: int, height: int) -> tuple[int, int]:
        sizes = WIDE_SIZES if width > height else POSTER_SIZES
        return next((s for s in sizes if s[0] >= width), sizes[-1])

    @staticmethod
    def _art_file(path: str, size: tuple[int, int]) -> Path:
        digest = hashlib.sha1(f"{path}@{size[0]}x{size[1]}".encode()).hexdigest()
        return ART_DIR / digest[:2] / f"{digest}.jpg"

    def art(self, path: str, width: int, height: int) -> bytes:
        """Artwork at the nearest cached size: from disk, else from the server
        (once, however many requests ask for the same picture at once)."""
        if not ART_PATH.fullmatch(path):
            raise PlexError(HTTPStatus.BAD_REQUEST, "path must be a /library/... artwork path")
        size = self._bucket(max(16, width), max(16, height))
        file = self._art_file(path, size)
        data = self._read_art(file)
        if data is not None:
            return data
        with self._art_lock:
            event = self._inflight.get(file)
            owner = event is None
            if owner:
                event = self._inflight[file] = threading.Event()
        if not owner:
            event.wait(PLEX_TIMEOUT + 2)
            data = self._read_art(file)
            if data is None:
                raise PlexError(HTTPStatus.GATEWAY_TIMEOUT, "artwork is not available")
            return data
        try:
            with UPSTREAM:
                data, _ = self.client.request("/photo/:/transcode", {
                    "url": path, "width": size[0], "height": size[1], "minSize": 1, "upscale": 1,
                }, accept="image/*")
            if self._check_disk():
                return data  # served, not saved
            tmp = file.with_suffix(".tmp")
            try:
                file.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                tmp.replace(file)
            except OSError as exc:  # the disk filled up between checks
                tmp.unlink(missing_ok=True)
                log.warning("artwork not saved: %s", exc)
                self._check_disk(force=True)
                return data
            self._art_added(len(data))
            return data
        finally:
            with self._art_lock:
                self._inflight.pop(file, None)
            event.set()

    @staticmethod
    def _read_art(file: Path) -> bytes | None:
        try:
            data = file.read_bytes()
        except FileNotFoundError:
            return None
        # mtime is the eviction order: refresh it on use, at most daily.
        try:
            if time.time() - file.stat().st_mtime > 86400:
                os.utime(file)
        except OSError:
            pass
        return data

    def _art_added(self, size: int) -> None:
        with self._art_lock:
            if self._art_bytes is None:  # the start-up scan is still counting
                return
            self._art_files = (self._art_files or 0) + 1
            self._art_bytes += size
            over = self._art_bytes > ART_LIMIT
        if not self._check_disk() and over:
            # Right here, not queued: a library's "keep artwork" fills the
            # cache from the same lane an eviction job would wait in.
            self._evict()

    def art_usage(self) -> tuple[int, int]:
        with self._art_lock:
            if self._art_bytes is not None:
                return self._art_files or 0, self._art_bytes
        files = self._art_listing()
        return len(files), sum(f[1] for f in files)

    def art_cached(self, item: dict) -> bool:
        path, size = self._list_art(item)
        return bool(path) and self._art_file(path, size).exists()

    # -- queries for the phone

    def indexed(self) -> bool:
        return bool(self._query("SELECT 1 FROM sections WHERE full_synced_at IS NOT NULL LIMIT 1"))

    def sections(self) -> list[dict]:
        rows = self._query("""SELECT s.key, s.title, s.type, COUNT(i.key) AS count FROM sections s
                              LEFT JOIN items i ON i.section = s.key AND i.type IN ('movie', 'show')
                              WHERE s.enabled=1 GROUP BY s.key ORDER BY s.position""")
        if rows:
            return [dict(r) for r in rows]
        # Not indexed yet (first start): ask the server.
        dirs = self.client.get("/library/sections").get("Directory") or []
        return [{"key": str(d.get("key")), "title": d.get("title"), "type": d.get("type")}
                for d in dirs if d.get("type") in SECTION_TYPES]

    def section(self, section: str, start: int, size: int, sort: str, unwatched: bool) -> dict:
        if not section.isdigit():
            raise PlexError(HTTPStatus.BAD_REQUEST, "section must be a number")
        info = self._query("SELECT type, full_synced_at FROM sections WHERE key=?", (section,))
        if not info or not info[0]["full_synced_at"]:
            return self._live_section(section, start, size, sort, unwatched)
        kind = SECTION_TYPES[info[0]["type"]][0]
        where = "section=? AND type=?" + (" AND unwatched=1" if unwatched else "")
        total = self._query(f"SELECT COUNT(*) FROM items WHERE {where}", (section, kind))[0][0]
        rows = self._query(f"SELECT data FROM items WHERE {where} ORDER BY {self.SORTS.get(sort, self.SORTS['title'])} "
                           "LIMIT ? OFFSET ?", (section, kind, min(max(1, size), 200), max(0, start)))
        return {"total": total, "items": [json.loads(r["data"]) for r in rows]}

    def letters(self, section: str, unwatched: bool) -> dict:
        """Where each first character starts in the title-sorted listing
        (section(sort="title") order), for the TV's A-Z jump. Empty until the
        library is indexed: the server's own listing has no such offsets."""
        if not section.isdigit():
            raise PlexError(HTTPStatus.BAD_REQUEST, "section must be a number")
        info = self._query("SELECT type, full_synced_at FROM sections WHERE key=?", (section,))
        if not info or not info[0]["full_synced_at"]:
            return {"letters": []}
        kind = SECTION_TYPES[info[0]["type"]][0]
        where = "section=? AND type=?" + (" AND unwatched=1" if unwatched else "")
        # upper() and NOCASE both fold ASCII only, so the groups come out in
        # the listing's own order (by first character, then the rest).
        rows = self._query(f"""SELECT upper(substr(sort_title, 1, 1)) AS c, COUNT(*) AS n FROM items
                               WHERE {where} GROUP BY c ORDER BY c COLLATE NOCASE""", (section, kind))
        out, start = [], 0
        for r in rows:
            out.append({"letter": r["c"], "start": start, "count": r["n"]})
            start += r["n"]
        return {"letters": out}

    def _live_section(self, section: str, start: int, size: int, sort: str, unwatched: bool) -> dict:
        sorts = {"title": "titleSort", "added": "addedAt:desc", "year": "year:desc",
                 "rating": "audienceRating:desc", "watched": "lastViewedAt:desc"}
        container = self.client.get(f"/library/sections/{section}/all", {
            "sort": sorts.get(sort, "titleSort"), "unwatched": 1 if unwatched else None,
            "X-Plex-Container-Start": max(0, start), "X-Plex-Container-Size": min(max(1, size), 200),
        })
        return {"total": container.get("totalSize", container.get("size", 0)),
                "items": [normalize(m) for m in container.get("Metadata") or []]}

    def search(self, text: str, kind: str | None = None, limit: int = 30) -> dict:
        """Grouped results: movies, shows, episodes. Each word matches as a
        prefix, accents ignored. Movies and shows match on title, show, cast,
        genres and year; episodes on their own title (a show's name would
        otherwise list every one of its episodes)."""
        words = [w for w in re.findall(r"\w+", text.lower()) if w][:8]
        groups = []
        limit = max(1, min(int(limit), 50))
        if not words:
            return {"groups": groups, "indexed": self.indexed()}
        if not self.indexed():
            return self._live_search(text)
        wanted = _fold(" ".join(words))
        # The words in the title, show and extra columns only (never the
        # type column: "movie" would match every movie).
        anywhere = "{title show_title extra} : (%s)" % " ".join(f'"{w}"*' for w in words)
        in_title = "title : (%s)" % " ".join(f'"{w}"*' for w in words)
        kinds = [(g, t, m) for g, t, m in (("movie", "Movies", anywhere), ("show", "Shows", anywhere),
                                           ("episode", "Episodes", in_title)) if kind in (None, "", "all", g)]
        found = {}
        if len(wanted) < 3:
            # One or two letters match most of the library: rank-scoring
            # every match is what took seconds. Titles starting with them
            # instead, straight from an index; episodes only from 3 letters.
            for group, _, _ in kinds:
                if group == "episode":
                    continue
                found[group] = self._query(f"""SELECT data, 0.0 AS score FROM items INDEXED BY items_title
                                               WHERE type=? AND sort_title >= ? COLLATE NOCASE
                                               AND sort_title < ? COLLATE NOCASE
                                               AND section IN (SELECT key FROM sections WHERE enabled=1)
                                               ORDER BY sort_title COLLATE NOCASE LIMIT ?""",
                                           (group, wanted, wanted + "\U0010ffff", limit))
        else:
            for group, _, match in kinds:
                found[group] = self._query("""SELECT items.data, bm25(items_fts, 10.0, 4.0, 1.0, 0.0) AS score
                                              FROM items_fts JOIN items ON items.rowid = items_fts.rowid
                                              WHERE items_fts MATCH ? AND items.section IN
                                                  (SELECT key FROM sections WHERE enabled=1)
                                              ORDER BY score LIMIT ?""",
                                           (f"type : {group} AND {match}", limit * 3))
        if not any(found.values()) and len(text.strip()) >= 4:
            # Last resort, for the middle of a word ("troville" in
            # "Zootroville"): a scan of movie and show titles, only when the
            # words found nothing (episodes are most of the titles: ~0.6 s
            # on a Pi 3, so not those).
            like = "%" + text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for group, _, _ in kinds:
                if group == "episode":
                    continue
                found[group] = self._query("""SELECT data, 0.0 AS score FROM items
                                              WHERE type=? AND title LIKE ? ESCAPE '\\'
                                              AND section IN (SELECT key FROM sections WHERE enabled=1)
                                              ORDER BY sort_title COLLATE NOCASE LIMIT ?""", (group, like, limit))
        for group, title, _ in kinds:
            rows = found.get(group)
            if not rows:
                continue
            items = [(json.loads(r["data"]), r["score"]) for r in rows]
            ranked = sorted(((_relevance(item, wanted, score), item) for item, score in items), key=lambda pair: pair[0])
            groups.append({"type": group, "title": title, "items": [item for _, item in ranked[:limit]],
                           "_best": ranked[0][0]})
        # The group with the best match first: "sever" puts the show Severance
        # above movies that only mention it.
        groups.sort(key=lambda g: g.pop("_best"))
        return {"groups": groups, "indexed": True}

    def _live_search(self, text: str) -> dict:
        hubs = self.client.get("/hubs/search", {"query": text.strip()[:100], "limit": 12}).get("Hub") or []
        groups = []
        for hub in hubs:
            items = [normalize(m) for m in hub.get("Metadata") or [] if m.get("type") in ("movie", "show", "episode")]
            if items:
                groups.append({"type": items[0]["type"], "title": hub.get("title") or "", "items": items})
        return {"groups": groups, "indexed": False}

    def item(self, key: str, fresh: bool = False) -> dict:
        """One entry with its children (a show's seasons, a season's
        episodes), what to play next, and how current the copy is.

        fresh: try the server first (briefly), so what was just watched on
        the TV shows as watched; fall back to the copy."""
        if not key.isdigit():
            raise PlexError(HTTPStatus.BAD_REQUEST, "key must be a number")
        stale = False
        if fresh:
            try:
                self.refresh(key, timeout=FRESH_TIMEOUT, retry=False)
            except PlexError as exc:
                if exc.status == HTTPStatus.NOT_FOUND:
                    raise
                stale = True
        rows = self._query("SELECT data, synced_at FROM items WHERE key=?", (key,))
        if not rows:
            if fresh and not stale:
                raise PlexError(HTTPStatus.NOT_FOUND, f"no Plex item {key}")
            if self.refresh(key) is None:
                raise PlexError(HTTPStatus.NOT_FOUND, f"no Plex item {key}")
            rows = self._query("SELECT data, synced_at FROM items WHERE key=?", (key,))
        item = json.loads(rows[0]["data"])
        if item["type"] in ("show", "season"):
            children = self._query("SELECT data FROM items WHERE parent=? ORDER BY idx", (key,))
            if not children and not stale:
                # Not indexed below this level yet (first sync still running):
                # its direct children now, everything under them behind.
                try:
                    found = self.client.get(f"/library/metadata/{key}/children").get("Metadata") or []
                    self._upsert(self._section_of(key), found, time.time_ns() // 1_000_000, keep_gen=True)
                except PlexError as exc:
                    log.info("item %s: children not available yet: %s", key, exc)
                self.enqueue("refresh", key)
                children = self._query("SELECT data FROM items WHERE parent=? ORDER BY idx", (key,))
            item["children"] = [json.loads(r["data"]) for r in children]
            nxt = self._next_episode(key, item["type"])
            if nxt:
                item["up_next"] = [nxt]
        item["index"] = {"synced_at": rows[0]["synced_at"], "art_cached": self.art_cached(item), "stale": stale}
        return item

    def _next_episode(self, key: str, kind: str) -> dict | None:
        """In progress first, else the first unwatched episode (specials last)."""
        column = "grandparent" if kind == "show" else "parent"
        rows = self._query(f"""SELECT data FROM items WHERE {column}=? AND type='episode' AND unwatched=1
                               ORDER BY (json_extract(data, '$.offset_ms') IS NULL), (parent_idx = 0), parent_idx, idx
                               LIMIT 1""", (key,))
        return json.loads(rows[0]["data"]) if rows else None

    def hubs(self) -> list[dict]:
        """Home rows (Continue Watching, ...). Live data, served from a copy
        for HUBS_TTL and refreshed behind the reply after that, so the phone
        never waits on the server for its first screen."""
        cached = self._hubs
        if cached and time.monotonic() - cached[0] < HUBS_TTL:
            return cached[1]
        if cached is None:
            stored = self._meta("hubs")
            if stored:
                cached = self._hubs = (0.0, json.loads(stored))
        if cached:
            self._refresh_hubs_behind()
            return cached[1]
        return self._load_hubs()

    def invalidate_hubs(self) -> None:
        if self._hubs:
            self._hubs = (0.0, self._hubs[1])

    def _refresh_hubs_behind(self) -> None:
        if self._hubs_refreshing:
            return
        self._hubs_refreshing = True

        def work() -> None:
            try:
                self._load_hubs()
            except Exception as exc:
                log.debug("hubs refresh: %s", exc)
            finally:
                self._hubs_refreshing = False

        threading.Thread(target=work, name="catalog-hubs", daemon=True).start()

    def _load_hubs(self) -> list[dict]:
        hubs = []
        for hub in self.client.get("/hubs", {"count": 16, "includeEmpty": 0}).get("Hub") or []:
            items = [normalize(m) for m in hub.get("Metadata") or [] if m.get("type") in TYPES]
            if items:
                hubs.append({"id": hub.get("hubIdentifier") or hub.get("title"), "title": hub.get("title") or "",
                             "items": items})
        self._hubs = (time.monotonic(), hubs)
        self._set_meta("hubs", json.dumps(hubs))
        return hubs

    # -- control

    def status(self) -> dict:
        counts = {r["type"]: r["n"] for r in self._query("SELECT type, COUNT(*) AS n FROM items GROUP BY type")}
        per_section: dict[str, dict[str, int]] = {}
        for r in self._query("SELECT section, type, COUNT(*) AS n FROM items GROUP BY section, type"):
            per_section.setdefault(r["section"], {})[r["type"]] = r["n"]
        sections = []
        for s in self._query("SELECT * FROM sections ORDER BY position"):
            section = {k: s[k] for k in s.keys()}
            section["counts"] = per_section.get(s["key"], {})
            section["count"] = sum(section["counts"].values())
            sections.append(section)
        files, size = self.art_usage()
        try:
            db_bytes = sum(p.stat().st_size for p in CATALOG_DB.parent.glob(CATALOG_DB.name + "*"))
        except OSError:
            db_bytes = 0
        jobs = [dict(job, lane=lane) for lane, job in self.jobs.items() if job]
        return {
            "state": "syncing" if jobs else "error" if self.error else "idle" if self.indexed() else "empty",
            "jobs": jobs,
            "queued": sum(q.qsize() for q in self._lanes.values()),
            "error": self.error,
            "server": self._meta("server_name"),
            "synced_at": float(self._meta("synced_at") or 0) or None,
            "full_synced_at": float(self._meta("full_synced_at") or 0) or None,
            "intervals": {"quick": QUICK_INTERVAL, "full": FULL_INTERVAL},
            "counts": {t: counts.get(t, 0) for t in TYPES},
            "db_bytes": db_bytes,
            "art": {"files": files, "bytes": size, "limit_bytes": ART_LIMIT,
                    "disk_reserve_bytes": DISK_RESERVE, "disk_low": self._disk_low},
            "sections": sections,
        }

    def summary(self) -> dict:
        """The short form for the status stream (pushed on every change)."""
        jobs = [job for job in self.jobs.values() if job]
        job = jobs[0] if jobs else None
        return {
            "state": "syncing" if jobs else "error" if self.error else "idle",
            "label": job["label"] if job else None,
            "done": job["done"] if job else None,
            "total": job["total"] if job else None,
        }

    def movies(self) -> list[dict]:
        """Every movie in the index, as stored (normalize()), for the watch list."""
        return [json.loads(r["data"]) for r in self._query("SELECT data FROM items WHERE type='movie'")]

    def synced_at(self) -> float:
        """When the index last changed from a sync (0 before the first)."""
        try:
            return float(self._meta("synced_at") or 0)
        except ValueError:
            return 0.0

    def plex_ratings(self, key: str) -> list[dict]:
        """One entry's full ratings list (IMDb, Rotten Tomatoes, TMDB), straight
        from the server: only its own page has the IMDb rating."""
        found = self.client.get(f"/library/metadata/{key}", timeout=PLEX_TIMEOUT).get("Metadata") or []
        return (found[0].get("Rating") or []) if found else []

    def set_section(self, key: str, enabled: bool | None = None, prefetch_art: bool | None = None) -> dict:
        rows = self._query("SELECT * FROM sections WHERE key=?", (key,))
        if not rows:
            raise PlexError(HTTPStatus.NOT_FOUND, f"no library {key}")
        with self._db_lock:
            if enabled is not None:
                self.db.execute("UPDATE sections SET enabled=? WHERE key=?", (int(enabled), key))
            if prefetch_art is not None:
                self.db.execute("UPDATE sections SET prefetch_art=? WHERE key=?", (int(prefetch_art), key))
        if enabled is False:
            # Out of the index: drop its entries (artwork stays until evicted).
            with self._tx() as db:
                db.execute("DELETE FROM items WHERE section=?", (key,))
                db.execute("UPDATE sections SET synced_at=NULL, full_synced_at=NULL, status=NULL WHERE key=?", (key,))
        elif enabled:
            self.enqueue("full", key)
        if prefetch_art:
            self.enqueue("art_fetch_section", key)
        self.on_change()
        return {"ok": True}

    def reset(self) -> dict:
        self._wipe()
        self._hubs = None
        self.enqueue("full")
        return {"ok": True}

    def _section_title(self, key: str | None) -> str:
        rows = self._query("SELECT title FROM sections WHERE key=?", (key,)) if key else []
        return rows[0]["title"] if rows else str(key)

    def _item_title(self, key: str | None) -> str:
        rows = self._query("SELECT title, type, show_title, parent_idx, idx FROM items WHERE key=?", (key,)) if key else []
        if not rows:
            return str(key)
        r = rows[0]
        if r["type"] == "episode":
            return f"{r['show_title']} S{r['parent_idx'] or 0:02d}E{r['idx'] or 0:02d}"
        return r["title"]

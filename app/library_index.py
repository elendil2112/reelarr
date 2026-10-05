"""
Library index — Reelarr's ground truth, scanned from the library itself.

A 20k-show curated library is a better metadata oracle than any external API:
  artists   — every canonical artist (folder names + ALBUMARTIST tags)
  host_map  — learned collab→host mappings from ARTIST/ALBUMARTIST tag pairs
              ("Phish w/ Billy Strings" → "Phish")
  venues    — venue → (city, state) gazetteer from album-tag triples

Scans read ONE audio file per show folder, so a full pass over the library is
minutes, not hours. Runs at startup (if empty), daily, after every filed show
(incrementally), and on demand from the GUI.
"""
import re
import sqlite3
import threading
import time
from pathlib import Path

from mutagen import File as MutagenFile

from . import database as db
from .metadata import parse_album_string, TAGGABLE_EXTS

from . import paths
DB_PATH = paths.config_dir() / "library_index.db"
_local = threading.local()
_scan_lock = threading.Lock()
_scan_state = {"running": False, "folders": 0}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # schema: migrations.py (INDEX_STEPS)
        _local.conn = c
    return _local.conn


# ── Song-title corpus: self-seeding per-artist known-song dictionary ─────────

def learn_songs(artist: str, titles, confirmed: bool = False) -> int:
    """Record track titles for an artist. confirmed=True marks them
    setlist.fm-authoritative (they win ties and seed the corpus strongly)."""
    if not artist or not titles:
        return 0
    an = _norm(artist)
    c = _conn()
    n = 0
    for t in titles:
        t = (t or "").strip()
        tn = _song_norm(t)
        if len(tn) < 2:
            continue
        c.execute(
            "INSERT INTO song_titles (artist_norm, title_norm, title, confirmed, seen) "
            "VALUES (?,?,?,?,1) ON CONFLICT(artist_norm, title_norm) DO UPDATE SET "
            "seen = seen + 1, confirmed = MAX(confirmed, excluded.confirmed), "
            "title = CASE WHEN excluded.confirmed=1 THEN excluded.title ELSE title END",
            (an, tn, t, 1 if confirmed else 0))
        n += 1
    c.commit()
    return n


def known_songs(artist: str) -> dict:
    """title_norm → {'title','confirmed','seen'} for one artist."""
    an = _norm(artist)
    return {r["title_norm"]: {"title": r["title"], "confirmed": r["confirmed"],
                              "seen": r["seen"]}
            for r in _conn().execute(
                "SELECT title_norm, title, confirmed, seen FROM song_titles "
                "WHERE artist_norm=?", (an,))}


def song_known(artist: str, title: str) -> int:
    """0 = unknown, 1 = seen locally, 2 = setlist.fm-confirmed."""
    tn = _song_norm(title)
    if len(tn) < 2:
        return 0
    r = _conn().execute(
        "SELECT confirmed FROM song_titles WHERE artist_norm=? AND title_norm=?",
        (_norm(artist), tn)).fetchone()
    if not r:
        return 0
    return 2 if r["confirmed"] else 1


def songs_count() -> int:
    return _conn().execute("SELECT COUNT(*) n FROM song_titles").fetchone()["n"]


def songs_count_for(artist: str) -> int:
    return _conn().execute(
        "SELECT COUNT(*) n FROM song_titles WHERE artist_norm=?",
        (_norm(artist),)).fetchone()["n"]


def _song_norm(s: str) -> str:
    """Looser than _norm: drop segue markers, punctuation, 'the', so
    'Dark Star >' and 'dark star' and 'The Dark Star' all match."""
    import re as _re
    s = _re.sub(r"[>→/].*$", "", (s or "").lower())      # keep pre-segue song
    s = _re.sub(r"\b(reprise|jam|tease|partial|cont\.?|continued)\b", "", s)
    s = _re.sub(r"^the\s+", "", s)
    return _re.sub(r"[^a-z0-9]", "", s)


# ── Lookups used by the pipeline ─────────────────────────────────────────────

def known_artists() -> dict:
    """norm → canonical name (from folders and ALBUMARTIST tags)."""
    return {r["norm"]: r["name"] for r in
            _conn().execute("SELECT norm, name FROM artists")}


def host_for(artist: str) -> str:
    r = _conn().execute("SELECT host_name FROM host_map WHERE artist_norm=?",
                        (_norm(artist),)).fetchone()
    return r["host_name"] if r else ""


def venue_lookup(venue: str):
    """Exact-normalized venue → (venue, city, state) or None."""
    r = _conn().execute("SELECT venue, city, state FROM venues WHERE norm=?",
                        (_norm(venue),)).fetchone()
    return (r["venue"], r["city"], r["state"]) if r else None


_city_cache = {"at": 0.0, "map": {}}


def city_states(city: str) -> dict:
    """How often each state/country appears with this city in your library's
    venues: {'OR': 41, 'ME': 2}. Cached for a few minutes."""
    import time as _t
    from .places import fold
    if _t.time() - _city_cache["at"] > 300:
        m = {}
        try:
            for r in _conn().execute("SELECT city, state FROM venues WHERE city != '' AND state != ''"):
                d = m.setdefault(fold(r["city"]), {})
                d[r["state"]] = d.get(r["state"], 0) + 1
        except sqlite3.Error:
            pass
        _city_cache.update(at=_t.time(), map=m)
    return dict(_city_cache["map"].get(fold(city), {}))


def venue_norms() -> set:
    return {r["norm"] for r in _conn().execute("SELECT norm FROM venues")}


def counts() -> dict:
    c = _conn()
    return {
        "artists": c.execute("SELECT COUNT(*) n FROM artists").fetchone()["n"],
        "hosts": c.execute("SELECT COUNT(*) n FROM host_map").fetchone()["n"],
        "venues": c.execute("SELECT COUNT(*) n FROM venues").fetchone()["n"],
        "last_scan": (c.execute("SELECT v FROM idx_meta WHERE k='last_scan'")
                      .fetchone() or {"v": None})["v"],
        "scanning": _scan_state["running"],
    }


def is_empty() -> bool:
    return counts()["artists"] == 0


# ── Corrections: your fixes become rules ───────────────────────────

def correction_set(kind: str, wrong: str, right):
    import json as _json
    if not wrong or not right:
        return
    c = _conn()
    c.execute("INSERT INTO corrections (kind, wrong_norm, right) VALUES (?,?,?) "
              "ON CONFLICT(kind, wrong_norm) DO UPDATE SET right=excluded.right",
              (kind, _norm(wrong), _json.dumps(right)))
    c.commit()


def correction_get(kind: str, wrong: str):
    import json as _json
    r = _conn().execute("SELECT right FROM corrections WHERE kind=? AND wrong_norm=?",
                        (kind, _norm(wrong))).fetchone()
    return _json.loads(r["right"]) if r else None


def corrections_list(kind: str = None):
    """All learned corrections, for review/management in the GUI."""
    import json as _json
    q = "SELECT kind, wrong_norm, right FROM corrections"
    args = ()
    if kind:
        q += " WHERE kind=?"
        args = (kind,)
    q += " ORDER BY kind, wrong_norm"
    out = []
    for r in _conn().execute(q, args).fetchall():
        try:
            right = _json.loads(r["right"])
        except Exception:
            right = r["right"]
        out.append({"kind": r["kind"], "wrong": r["wrong_norm"], "right": right})
    return out


def correction_delete(kind: str, wrong: str) -> bool:
    c = _conn()
    cur = c.execute("DELETE FROM corrections WHERE kind=? AND wrong_norm=?",
                    (kind, _norm(wrong)))
    c.commit()
    return cur.rowcount > 0


def corrections_count() -> int:
    return _conn().execute("SELECT COUNT(*) n FROM corrections").fetchone()["n"]


# ── Writes ───────────────────────────────────────────────────────────────────

def _upsert_artist(c, name: str):
    if name and len(name) > 1:
        c.execute("INSERT INTO artists (norm, name) VALUES (?,?) "
                  "ON CONFLICT(norm) DO NOTHING", (_norm(name), name.strip()))


def _upsert_host(c, artist: str, host: str):
    if artist and host and _norm(artist) != _norm(host):
        c.execute("INSERT INTO host_map (artist_norm, host_name) VALUES (?,?) "
                  "ON CONFLICT(artist_norm) DO UPDATE SET host_name=excluded.host_name",
                  (_norm(artist), host.strip()))


def _upsert_venue(c, venue: str, city: str, state: str):
    if not venue or len(venue) < 3 or not (city or state):
        return
    c.execute("""INSERT INTO venues (norm, venue, city, state) VALUES (?,?,?,?)
                 ON CONFLICT(norm) DO UPDATE SET
                   seen = seen + 1,
                   city  = CASE WHEN excluded.city  != '' THEN excluded.city  ELSE city  END,
                   state = CASE WHEN excluded.state != '' THEN excluded.state ELSE state END""",
              (_norm(venue), venue.strip(), (city or "").strip(), (state or "").strip()))


def add_show(meta):
    """Incremental learning when Reelarr files a show."""
    c = _conn()
    _upsert_artist(c, meta.album_artist)
    _upsert_artist(c, meta.artist) if meta.artist == meta.album_artist else None
    _upsert_host(c, meta.artist, meta.album_artist)
    _upsert_venue(c, meta.venue, meta.city, meta.state)
    c.commit()


# ── Full scan ────────────────────────────────────────────────────────────────

def _first_audio(show_dir: Path):
    try:
        for p in sorted(show_dir.iterdir()):
            if p.suffix.lower() in TAGGABLE_EXTS and p.is_file():
                return p
        for p in sorted(show_dir.rglob("*")):
            if p.suffix.lower() in TAGGABLE_EXTS and p.is_file():
                return p
    except OSError:
        pass
    return None


def scan(library_dir) -> dict:
    """Walk the library, reading one file per show folder."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "scan already running"}
    _scan_state["running"] = True
    started = time.time()
    folders = 0
    try:
        library_dir = Path(library_dir)
        if not library_dir.is_dir():
            return {"error": "library dir missing"}
        c = _conn()
        for artist_dir in sorted(library_dir.iterdir()):
            if not artist_dir.is_dir() or artist_dir.name.startswith((".", "_")):
                continue
            _upsert_artist(c, artist_dir.name)
            show_dirs = []
            for child in artist_dir.iterdir():
                if not child.is_dir():
                    continue
                # year subfolder (e.g. "trey2003") → scan the shows inside it
                if re.match(r"^.*?(?:19|20)\d{2}$", child.name) \
                        and not re.search(r"\d{4}-\d{2}-\d{2}", child.name) \
                        and any(s.is_dir() for s in child.iterdir()):
                    show_dirs.extend(s for s in child.iterdir() if s.is_dir())
                else:
                    show_dirs.append(child)
            for show in show_dirs:
                folders += 1
                _scan_state["folders"] = folders
                audio = _first_audio(show)
                if not audio:
                    continue
                try:
                    f = MutagenFile(audio, easy=True)
                    if not f:
                        continue
                    tag = lambda k, f=f: (f.get(k) or [""])[0].strip()
                    artist, albumartist = tag("artist"), tag("albumartist")
                    _upsert_artist(c, albumartist or artist_dir.name)
                    _upsert_host(c, artist, albumartist or artist_dir.name)
                    album_meta, _ = parse_album_string(tag("album"))
                    _upsert_venue(c, album_meta.venue, album_meta.city,
                                  album_meta.state)
                except Exception:
                    continue
                if folders % 500 == 0:
                    c.commit()
                    db.log("info", "index", f"library scan: {folders} shows so far…")
        c.execute("INSERT INTO idx_meta (k,v) VALUES ('last_scan',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(time.time()),))
        c.commit()
        result = counts()
        db.log("info", "index",
               f"library scan finished in {time.time()-started:.0f}s — "
               f"{folders} shows, {result['artists']} artists, "
               f"{result['venues']} venues, {result['hosts']} host mappings")
        return result
    finally:
        _scan_state["running"] = False
        _scan_lock.release()

"""The Live Music Archive — search archive.org and grab .torrent files.

Finds shows on the Internet Archive's Live Music Archive (mediatype:etree) and
drops their .torrent files into Reelarr's torrent folder. From there the normal
torrent flow takes over: the file goes to your download client with a label on
it, and when it finishes the show is copied into the watch folder and tagged.

Search → torrent → client → library, without leaving Reelarr.
The Indexer wrapper used by Discover and the Artists monitor is in
indexers/lma.py.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, database as db

ARCHIVE = "https://archive.org"
SEARCH_API = ARCHIVE + "/advancedsearch.php"
METADATA_API = ARCHIVE + "/metadata/{ident}"
from .version import __version__
UA = f"Reelarr/{__version__} (live-music archiving; +https://archive.org/details/etree)"
DELAY = 0.4          # be polite between requests

_FIELDS = ["identifier", "title", "creator", "date", "venue", "coverage",
           "downloads", "avg_rating", "source", "format", "addeddate"]


def _formats(raw) -> list:
    """archive.org format names → short tokens."""
    names = raw if isinstance(raw, list) else [raw] if raw else []
    out = []
    for n in names:
        n = str(n).lower()
        if "flac" in n:
            out.append("flac24" if "24bit" in n.replace(" ", "") else "flac")
        elif "shorten" in n:
            out.append("shn")
        elif "wave" in n or n == "wav":
            out.append("wav")
        elif "mp3" in n:
            out.append("mp3")
        elif "ogg" in n:
            out.append("ogg")
    return sorted(set(out))


class LmaError(Exception):
    pass


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        raise LmaError(f"archive.org: {e}") from None


def _get_json(url: str, timeout: int = 30):
    raw = _get(url, timeout)
    try:
        return json.loads(raw or b"{}")
    except Exception:
        raise LmaError("archive.org sent something that wasn't JSON") from None


# ── Search ──────────────────────────────────────────────────────────────────

def _search_url(q: str, rows: int, page: int, sort: str) -> str:
    params = [("q", q), ("rows", str(rows)), ("page", str(page)),
              ("output", "json"), ("sort[]", sort)]
    params += [("fl[]", f) for f in _FIELDS]
    return SEARCH_API + "?" + urllib.parse.urlencode(params)


def _clean(v):
    """Archive fields come back as a string or a list of strings."""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x)
    return "" if v is None else str(v)


def _row(doc: dict) -> dict:
    date = _clean(doc.get("date"))[:10]
    return {
        "identifier": doc.get("identifier", ""),
        "title": _clean(doc.get("title")),
        "artist": _clean(doc.get("creator")),
        "date": date,
        "venue": _clean(doc.get("venue")),
        "location": _clean(doc.get("coverage")),
        "source": _clean(doc.get("source"))[:80],
        "downloads": doc.get("downloads") or 0,
        "formats": _formats(doc.get("format")),
        "added": _clean(doc.get("addeddate"))[:19],
    }


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def mark_owned(rows: list) -> list:
    """Flag results whose date and artist already exist in the library, so a
    20,000-show collection doesn't keep re-downloading what it has."""
    for r in rows:
        r["owned"] = False
        date, artist = r.get("date", ""), _norm(r.get("artist", ""))
        if not date:
            continue
        try:
            for hit in db.shows_on_date(date):
                blob = _norm((hit.get("filed_path") or "") + (hit.get("meta") or ""))
                if artist and artist[:12] in blob:
                    r["owned"] = True
                    break
        except Exception:
            pass
    return rows


def search(query: str, rows: int = 25, page: int = 1,
           sort: str = "downloads desc") -> dict:
    """Free-text search of the Live Music Archive."""
    query = (query or "").strip()
    if not query:
        raise LmaError("nothing to search for")
    q = f"({query}) AND mediatype:etree"
    data = _get_json(_search_url(q, rows, page, sort))
    resp = data.get("response", {})
    return {"total": resp.get("numFound", 0), "page": page,
            "results": [_row(d) for d in resp.get("docs", [])]}


def collection(identifier: str, rows: int = 25, page: int = 1,
               sort: str = "date desc") -> dict:
    """Everything inside an archive.org collection, e.g. GratefulDead."""
    q = f"collection:{identifier} AND mediatype:etree"
    data = _get_json(_search_url(q, rows, page, sort))
    resp = data.get("response", {})
    return {"total": resp.get("numFound", 0), "page": page,
            "results": [_row(d) for d in resp.get("docs", [])]}


def item(identifier: str) -> dict:
    """One show, by identifier."""
    meta = _get_json(METADATA_API.format(ident=identifier)).get("metadata", {})
    if not meta:
        raise LmaError(f"archive.org has nothing called {identifier!r}")
    return {"total": 1, "page": 1, "results": [_row({**meta,
                                                     "identifier": identifier})]}


_DETAILS_RE = re.compile(r"archive\.org/(?:details|download)/([^/?#]+)")


def parse_input(text: str) -> tuple:
    """Work out what the user typed: an item URL, a collection URL, or a query.
    Returns (kind, value) where kind is 'item' | 'collection' | 'query'."""
    text = (text or "").strip()
    m = _DETAILS_RE.search(text)
    if not m:
        return ("query", text)
    ident = urllib.parse.unquote(m.group(1))
    # ask the archive what it is rather than guessing from the name
    try:
        meta = _get_json(METADATA_API.format(ident=ident), timeout=20)
        mtype = (meta.get("metadata", {}) or {}).get("mediatype", "")
        if mtype == "collection":
            return ("collection", ident)
        if mtype:
            return ("item", ident)
    except LmaError:
        pass
    # fall back to the old heuristic: recording ids almost always contain dots
    return ("item", ident) if "." in ident else ("collection", ident)


def lookup(text: str, rows: int = 25, page: int = 1) -> dict:
    kind, value = parse_input(text)
    if kind == "item":
        out = item(value)
    elif kind == "collection":
        out = collection(value, rows=rows, page=page)
    else:
        out = search(value, rows=rows, page=page)
    out["kind"] = kind
    out["query"] = value
    out["results"] = mark_owned(out.get("results", []))
    return out


# ── Torrent files ───────────────────────────────────────────────────────────

def torrent_url(identifier: str) -> str:
    """Find an item's .torrent. Asks the metadata API which file it is rather
    than assuming the usual name, then falls back to the convention."""
    try:
        meta = _get_json(METADATA_API.format(ident=identifier), timeout=20)
        for f in meta.get("files", []) or []:
            name = f.get("name", "")
            if f.get("format") == "Archive BitTorrent" or name.endswith(".torrent"):
                return f"{ARCHIVE}/download/{identifier}/{urllib.parse.quote(name)}"
    except LmaError:
        pass
    return f"{ARCHIVE}/download/{identifier}/{identifier}_archive.torrent"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def torrent_dir() -> Path:
    d = (config.load()["torrents"].get("watch_dir") or "").strip()
    if not d:
        raise LmaError("no .torrent folder set — choose one in Settings → Torrents "
                       "so grabbed torrents have somewhere to land")
    p = Path(d)
    if not p.is_dir():
        raise LmaError(f"the torrent folder {d} doesn't exist or isn't reachable")
    return p


def already_here(identifier: str, dest: Path) -> bool:
    """Has this torrent been grabbed before — still waiting, or already handed
    to the client?"""
    stem = _safe(identifier)
    for folder in (dest, dest / "_added", dest / "_failed"):
        if folder.is_dir():
            for p in folder.glob("*.torrent"):
                if p.stem.startswith(stem) or stem in p.stem:
                    return True
    return False


def grab(identifiers: list, delay: float = DELAY) -> dict:
    """Download .torrent files into the torrent folder, where Reelarr's normal
    torrent run picks them up."""
    dest = torrent_dir()
    got = skipped = failed = 0
    names = []
    for ident in [i for i in (identifiers or []) if i]:
        if already_here(ident, dest):
            skipped += 1
            continue
        try:
            url = torrent_url(ident)
            data = _get(url, timeout=45)
            if not data.startswith(b"d"):
                raise LmaError("that didn't come back as a torrent file")
            out = dest / _safe(url.split("/")[-1])
            tmp = dest / ("." + out.name + ".part")
            tmp.write_bytes(data)
            tmp.rename(out)          # atomic: the watcher never sees a partial
            got += 1
            names.append(out.name)
            db.log("info", "lma", f"grabbed torrent: {ident}")
        except Exception as e:
            failed += 1
            db.log("warn", "lma", f"couldn't grab {ident}: {e}")
        time.sleep(delay)
    if got or failed:
        db.log("info", "lma",
               f"Live Music Archive: {got} torrent(s) added to the torrent folder"
               + (f", {skipped} already had" if skipped else "")
               + (f", {failed} failed" if failed else ""))
    return {"grabbed": got, "skipped": skipped, "failed": failed, "names": names}

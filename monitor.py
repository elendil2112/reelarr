"""Monitored artists: the search loop that makes Reelarr an *arr.

Live recordings don't behave like TV episodes. There's no list of episodes
that "should" exist; tapes surface years after the show, in no order, often
several sources at once. So "wanted" here means:

    a NEW upload, by an artist you follow, that you don't already have
    (or that beats the copy you have, if you ask for upgrades),
    in a format and source you'd accept.

Every release an indexer shows us is judged once and remembered in the
`releases` table, with the reason, so you can see why something was or
wasn't picked up. Wanted releases wait for you in the Queue unless the
artist is set to grab automatically — and nothing is grabbed while
dry-run is on.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, database as db, fileops, indexers

SOURCE_RANK = {"SBD": 4, "SBD.FM": 3, "MTX": 3, "AUD.FOB": 2, "AUD": 1, "": 0}
_lock = threading.Lock()
_state = {"running": False, "current": "", "last": None}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _c():
    return db._conn()


def _row(r) -> dict:
    d = dict(r)
    d["sources"] = [x for x in (d.get("sources") or "").split(",") if x]
    return d


# ── Artists ──────────────────────────────────────────────────────────────────

FIELDS = ("monitored", "indexer", "collection", "sources", "require_lossless",
          "upgrades", "action")


def list_artists() -> list:
    rows = _c().execute(
        "SELECT a.*, "
        " (SELECT COUNT(*) FROM releases r WHERE r.artist_id=a.id AND r.status='wanted') wanted,"
        " (SELECT COUNT(*) FROM releases r WHERE r.artist_id=a.id AND r.status='grabbed') grabbed "
        "FROM monitored_artists a ORDER BY a.name COLLATE NOCASE").fetchall()
    return [_row(r) for r in rows]


def get_artist(artist_id: int):
    r = _c().execute("SELECT * FROM monitored_artists WHERE id=?", (artist_id,)).fetchone()
    return _row(r) if r else None


def _since(days) -> str:
    """lookback in days → the date to search from. Negative = everything."""
    if days is None or days == "":
        days = config.load().get("monitor", {}).get("lookback_days", 30)
    days = int(days)
    if days < 0:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _since_text(since: str) -> str:
    return f"uploads since {since}" if since else "everything archive.org has"


def save_artist(name: str, lookback_days=None, **prefs) -> dict:
    """Add or update. A newly added artist looks back lookback_days (default
    30; -1 = everything), so following the Grateful Dead doesn't queue
    15,000 tapes unless you ask it to."""
    name = (name or "").strip()
    if not name:
        raise ValueError("artist name required")
    norm = _norm(name)
    vals = {}
    for k in FIELDS:
        if k in prefs and prefs[k] is not None:
            v = prefs[k]
            if k == "sources" and isinstance(v, (list, tuple)):
                v = ",".join(s.strip().upper() for s in v if s.strip())
            if k in ("monitored", "require_lossless", "upgrades"):
                v = 1 if v else 0
            if k == "action" and v not in ("wanted", "grab"):
                raise ValueError("action must be wanted or grab")
            if k == "indexer":
                indexers.get(v)
            vals[k] = v
    existing = _c().execute("SELECT id FROM monitored_artists WHERE norm=?", (norm,)).fetchone()
    if existing:
        if vals:
            sets = ", ".join(f"{k}=?" for k in vals)
            _c().execute(f"UPDATE monitored_artists SET {sets}, name=? WHERE id=?",
                         [*vals.values(), name, existing["id"]])
        aid = existing["id"]
    else:
        since = _since(lookback_days)
        cols = {"name": name, "norm": norm, "added_at": time.time(), "last_checked": since, **vals}
        cur = _c().execute(f"INSERT INTO monitored_artists ({', '.join(cols)}) "
                           f"VALUES ({', '.join('?' for _ in cols)})", list(cols.values()))
        aid = cur.lastrowid
        db.log("info", "artists", f"now monitoring {name} ({_since_text(since)})")
    _c().commit()
    return get_artist(aid)


def remove_artist(artist_id: int) -> bool:
    a = get_artist(artist_id)
    if not a:
        return False
    _c().execute("UPDATE releases SET artist_id=NULL WHERE artist_id=?", (artist_id,))
    _c().execute("DELETE FROM monitored_artists WHERE id=?", (artist_id,))
    _c().commit()
    db.log("info", "artists", f"stopped monitoring {a['name']}")
    return True


def look_back(artist_id: int, days) -> dict:
    """Search an artist's older uploads again. Releases that were passed over
    are judged afresh (your settings may have changed); ones you grabbed or
    ignored stay as they are."""
    a = get_artist(artist_id)
    if not a:
        raise ValueError("no such artist")
    since = _since(days)
    _c().execute("DELETE FROM releases WHERE artist_id=? AND status='skipped'", (artist_id,))
    _c().execute("UPDATE monitored_artists SET last_checked=? WHERE id=?", (since, artist_id))
    _c().commit()
    db.log("info", "artists", f"{a['name']}: looking back again — {_since_text(since)}")
    return get_artist(artist_id)


# ── Judging a release ────────────────────────────────────────────────────────

def _owned_sources(artist: str, date: str) -> list:
    """Source types of library copies of this artist on this date."""
    out = []
    an = _norm(artist)[:12]
    for hit in db.shows_on_date(date):
        blob = _norm((hit.get("filed_path") or "") + (hit.get("meta") or ""))
        if an and an not in blob:
            continue
        try:
            out.append((json.loads(hit.get("meta") or "{}").get("source_type") or "").upper())
        except ValueError:
            out.append("")
    return out


def judge(release, artist: dict) -> tuple:
    """(status, reason) for a release, without network calls."""
    if artist.get("require_lossless") and release.formats and not release.lossless:
        return "skipped", "lossy only (" + ", ".join(release.formats) + ")"
    allowed = artist.get("sources") or []
    st = (release.source_type or "").upper()
    if allowed and st and st not in allowed and st.split(".")[0] not in allowed:
        return "skipped", f"{st} isn't one of your sources ({', '.join(allowed)})"
    if release.date:
        have = _owned_sources(artist["name"], release.date)
        if have:
            if not artist.get("upgrades"):
                return "skipped", "already in your library"
            best = max(SOURCE_RANK.get(h, 0) for h in have)
            if SOURCE_RANK.get(st, 0) <= best:
                return "skipped", f"not better than the {max(have, key=lambda h: SOURCE_RANK.get(h, 0)) or 'copy'} you have"
            return "wanted", f"upgrade: {st} over your {', '.join(h or '?' for h in have)}"
    if not st:
        return "wanted", "source type not stated — have a look"
    return "wanted", ""


def _record(release, artist_id, status, reason) -> int:
    now = time.time()
    _c().execute(
        "INSERT INTO releases (indexer, release_id, artist_id, data, status, reason, first_seen, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(indexer, release_id) DO UPDATE SET "
        "status=excluded.status, reason=excluded.reason, data=excluded.data, updated_at=excluded.updated_at",
        (release.indexer, release.id, artist_id, json.dumps(release.to_dict()), status, reason, now, now))
    _c().commit()
    return _c().execute("SELECT id FROM releases WHERE indexer=? AND release_id=?",
                        (release.indexer, release.id)).fetchone()["id"]


def _seen(indexer: str, release_id: str) -> bool:
    return _c().execute("SELECT 1 FROM releases WHERE indexer=? AND release_id=?",
                        (indexer, release_id)).fetchone() is not None


# ── The loop ─────────────────────────────────────────────────────────────────

def check_artist(artist: dict) -> dict:
    idx = indexers.get(artist.get("indexer") or "lma")
    since = artist.get("last_checked") or ""
    out = {"artist": artist["name"], "new": 0, "wanted": 0, "grabbed": 0, "skipped": 0,
           "since": since, "notes": []}
    try:
        found = idx.new_for_artist(artist["name"], since=since,
                                   collection=artist.get("collection") or "")
    except indexers.IndexerError as e:
        db.log("warn", "artists", f"{artist['name']}: {idx.label} search failed: {e}")
        out["error"] = str(e)
        return out
    out["found"] = len(found)
    newest = since
    to_grab = []
    for rel in found:
        if rel.added and rel.added[:10] > newest:
            newest = rel.added[:10]
        if _seen(rel.indexer, rel.id):
            continue
        out["new"] += 1
        status, reason = judge(rel, artist)
        if status == "wanted":
            why = idx.check_downloadable(rel)
            if why:
                rel.restricted = why
                status, reason = "skipped", why
        rid = _record(rel, artist["id"], status, reason)
        if status == "wanted" and artist.get("action") == "grab":
            to_grab.append(rid)
        out["wanted" if status == "wanted" else "skipped"] += 1
    for rid in to_grab:
        r = grab_release(rid, automatic=True)
        if r.get("grabbed"):
            out["grabbed"] += 1
            out["wanted"] -= 1
        elif r.get("dry_run"):
            out["dry_run"] = True
        elif r.get("error"):
            out["grab_error"] = r["error"]
    # Say why nothing reached the download client, so it's never a mystery.
    notes = out["notes"]
    if not found:
        total = None
        try:
            total = idx.count_for_artist(artist["name"], artist.get("collection") or "")
        except indexers.IndexerError:
            pass
        if total == 0:
            what = (f"the collection '{artist['collection']}'" if artist.get("collection")
                    else f"the name '{artist['name']}'")
            notes.append(f"{idx.label} has nothing at all under {what}")
            if not artist.get("collection"):
                alts = [n for n in idx.similar_names(artist["name"]) if _norm(n) != _norm(artist["name"])]
                if alts:
                    notes.append("names it does have: " + ", ".join(alts))
        elif total:
            notes.append(f"nothing added since {since or 'ever'} ({total:,} older recordings — "
                         f"use Look back to fetch those)")
    if out["skipped"] and not out["wanted"] and not out["grabbed"]:
        notes.append("everything new was passed over — see Queue → Passed over for why")
    if out["wanted"] and artist.get("action") != "grab":
        notes.append("waiting in Queue → Wanted (this artist is set to ask you first)")
    if out.get("dry_run"):
        notes.append("not grabbed automatically because dry run is on")
    if out.get("grab_error"):
        notes.append("grab failed: " + out["grab_error"])

    summary = (f"{artist['name']}: {out['new']} new on {idx.label} — {out['wanted']} wanted, "
               f"{out['grabbed']} grabbed, {out['skipped']} skipped")
    if notes:
        summary += " · " + "; ".join(notes)
    db.log("warn" if (out.get("grab_error") or (not found and "nothing at all" in " ".join(notes)))
           else "info", "artists", summary)
    out["summary"] = summary
    note = (f"{out['new']} new: {out['wanted']} wanted, {out['grabbed']} grabbed, "
            f"{out['skipped']} skipped") + (" · " + "; ".join(notes) if notes else "")
    _c().execute("UPDATE monitored_artists SET last_checked=?, checked_at=?, last_note=? WHERE id=?",
                 (newest, time.time(), note, artist["id"]))
    _c().commit()
    return out


def gates() -> list:
    """Settings that would stop monitored artists' finds reaching the client."""
    cfg = config.load()
    t = cfg.get("torrents", {}) or {}
    artists = [a for a in list_artists() if a["monitored"]]
    out = []
    if not artists:
        return out
    if not t.get("watch_dir"):
        out.append("No .torrent folder is set, so nothing can be grabbed — choose one in Settings → Torrents.")
    elif not t.get("enabled"):
        out.append("No download client is switched on, so grabbed .torrent files will wait in the "
                   "torrent folder — set one up in Settings → Torrents.")
    auto = [a["name"] for a in artists if a.get("action") == "grab"]
    if auto and fileops.dry_run_enabled():
        out.append("Dry run is on, so artists set to grab automatically put new finds in the Queue "
                   "instead. Go live (or press Grab in the Queue) to send them.")
    if not auto:
        out.append("Every artist is set to ask you first: new finds wait in Queue → Wanted. "
                   "Choose “grab automatically” on an artist to skip that.")
    return out


def run_all() -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    _state["running"] = True
    totals = {"artists": 0, "new": 0, "wanted": 0, "grabbed": 0, "skipped": 0}
    try:
        for a in [a for a in list_artists() if a["monitored"]]:
            _state["current"] = a["name"]
            r = check_artist(a)
            totals["artists"] += 1
            for k in ("new", "wanted", "grabbed", "skipped"):
                totals[k] += r.get(k, 0)
            time.sleep(0.5)            # be polite to the indexer
        _state["last"] = time.time()
        db.kv_set("monitor_last_run", _state["last"])
        return totals
    finally:
        _state["running"] = False
        _state["current"] = ""
        _lock.release()


def status() -> dict:
    return {**_state, "last": db.kv_get("monitor_last_run")}


# ── Wanted ───────────────────────────────────────────────────────────────────

def list_releases(status: str = "wanted", limit: int = 200) -> list:
    rows = _c().execute(
        "SELECT r.*, a.name artist_name FROM releases r LEFT JOIN monitored_artists a "
        "ON a.id = r.artist_id WHERE r.status=? ORDER BY r.updated_at DESC LIMIT ?",
        (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d.get("data") or "{}")
        out.append(d)
    return out


def counts() -> dict:
    return {r["status"]: r["n"] for r in _c().execute(
        "SELECT status, COUNT(*) n FROM releases GROUP BY status")}


def _set(rid: int, status: str, reason: str = ""):
    _c().execute("UPDATE releases SET status=?, reason=?, updated_at=? WHERE id=?",
                 (status, reason, time.time(), rid))
    _c().commit()


def grab_release(rid: int, automatic: bool = False) -> dict:
    """Hand a wanted release to its indexer. Automatic grabs honour dry-run;
    one you click yourself goes ahead (it's one torrent, and you chose it)."""
    r = _c().execute("SELECT * FROM releases WHERE id=?", (rid,)).fetchone()
    if not r:
        return {"error": "no such release"}
    if automatic and fileops.dry_run_enabled():
        _set(rid, "wanted", "would grab automatically — dry run is on")
        return {"grabbed": 0, "dry_run": True}
    try:
        res = indexers.get(r["indexer"]).grab([r["release_id"]])
    except indexers.IndexerError as e:
        _set(rid, "wanted", f"grab failed: {e}")
        return {"error": str(e)}
    if res.get("grabbed"):
        try:            # hand it to the client now rather than at the next poll
            if config.load()["torrents"].get("enabled"):
                from . import scheduler, torrents
                scheduler.run_in_background(torrents.scan, True)
        except Exception:
            pass
    if res.get("grabbed") or res.get("skipped"):
        _set(rid, "grabbed", "handed to the download client" if res.get("grabbed")
             else "already in the torrent folder")
        return {"grabbed": 1}
    _set(rid, "wanted", "grab failed — see Activity")
    return {"error": "grab failed — see Activity"}


def ignore_release(rid: int) -> bool:
    _set(rid, "ignored", "ignored by you")
    return True

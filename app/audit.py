"""
Maintenance — Reelarr learns from its mistakes.

Every filed show's decision (parsed meta, destination, provenance) is on
record. When you corrects a show on disk — moves it to the right
artist, retags the album — the learning pass diffs the record against
reality and mints corrections:

  wrong artist  → right artist   (alias + correction rule)
  wrong venue   → right venue/city/state (gazetteer + correction rule)

Those rules are applied to every FUTURE show in the pipeline, so each
manual fix prevents the whole class of repeat mistakes.

It also flags suspects (filed shows whose tags look wrong) and exports a
full audit report — original folder names, final names, tags, confidence,
provenance, flags — as JSON suitable for dropping into Claude so the
parsing rules themselves can be improved.
"""
import difflib
import json
import re
import time
from pathlib import Path

from mutagen import File as MutagenFile

from . import config, database as db, library_index
from .metadata import parse_album_string, AUDIO_EXTS, TAGGABLE_EXTS, _US_STATES

from . import paths
EXPORT_DIR = paths.config_dir() / "exports"

_SUSPECT_WORDS = re.compile(
    r"\b(venue|location|setlist|unknown|shntool|xml|lineage|source matrix|"
    r"tracklist|untitled)\b", re.IGNORECASE)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _first_tags(folder: Path) -> dict:
    for p in sorted(folder.rglob("*")):
        if p.suffix.lower() in TAGGABLE_EXTS and p.is_file():
            try:
                f = MutagenFile(p, easy=True)
                if f:
                    g = lambda k, f=f: (f.get(k) or [""])[0].strip()
                    if g("album") or g("artist"):
                        return {"artist": g("artist"),
                                "albumartist": g("albumartist"),
                                "album": g("album"), "date": g("date")}
            except Exception:
                continue
    return {}


def _walk_shows(library_dir: Path):
    """Yield (artist_folder_name, show_dir) incl. year-subfolder layouts."""
    for artist_dir in sorted(library_dir.iterdir()):
        if not artist_dir.is_dir() or artist_dir.name.startswith((".", "_")):
            continue
        for child in artist_dir.iterdir():
            if not child.is_dir():
                continue
            if re.match(r"^.*?(?:19|20)\d{2}$", child.name) \
                    and not re.search(r"\d{4}-\d{2}-\d{2}", child.name):
                for sub in child.iterdir():
                    if sub.is_dir():
                        yield artist_dir.name, sub
            else:
                yield artist_dir.name, child


def _locate(date: str, recorded_name: str, index: dict):
    """Find the show on disk today: match by date, break ties by similarity."""
    candidates = index.get(date, [])
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0]
    best = max(candidates, key=lambda c: difflib.SequenceMatcher(
        None, _norm(recorded_name), _norm(c[1].name)).ratio())
    return best


# ── Learning pass ────────────────────────────────────────────────────────────

def learn_from_corrections() -> dict:
    """Diff every filed show's recorded decision against on-disk reality."""
    cfg = config.load()
    library_dir = Path(cfg["paths"]["library_dir"])
    if not library_dir.is_dir():
        return {"error": "library dir missing"}

    disk = {}
    for artist_name, show in _walk_shows(library_dir):
        m = re.search(r"((?:19|20)\d{2}-\d{2}-\d{2})", show.name)
        if m:
            disk.setdefault(m.group(1), []).append((artist_name, show))

    learned_artists, learned_venues, reconciled, missing = {}, {}, 0, 0
    rows = db.list_shows(status="filed", limit=100000)
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        date = meta.get("date", "")
        if not date:
            continue
        rec_path = Path(row["filed_path"] or "")
        rec_artist = (meta.get("host_artist") or meta.get("artist") or "").strip()
        actual_artist, actual_dir = _locate(date, rec_path.name, disk)
        if actual_dir is None:
            missing += 1
            db.update_show(row["id"], status="rejected",
                           notes="removed from library by you")
            continue

        changed = False

        # 1) artist correction: you moved it to a different folder
        if rec_artist and actual_artist and _norm(actual_artist) != _norm(rec_artist):
            library_index.correction_set("artist", rec_artist, actual_artist)
            learned_artists[rec_artist] = actual_artist
            changed = True

        # 2) venue/location correction: you retagged the album
        tags = _first_tags(actual_dir)
        if tags.get("album"):
            actual_meta, complete = parse_album_string(tags["album"])
            rv, av = meta.get("venue", ""), actual_meta.venue
            if av and actual_meta.city and rv and _norm(av) != _norm(rv):
                trio = {"venue": av, "city": actual_meta.city,
                        "state": actual_meta.state}
                # key the correction on the wrong string AND its head segment —
                # different parse paths surface different fragments of it
                keys = {rv}
                head = rv.split(",")[0].strip()
                if len(head) > 3:
                    keys.add(head)
                for k in keys:
                    if _norm(k) and _norm(k) != _norm(av):
                        library_index.correction_set("venue", k, trio)
                learned_venues[rv] = trio
                changed = True
            if av and actual_meta.city:
                # corrected or confirmed — either way, feed the gazetteer
                library_index._upsert_venue(
                    library_index._conn(), av, actual_meta.city, actual_meta.state)

        # 3) reconcile the record with reality
        if str(actual_dir) != str(rec_path) or changed:
            new_meta = dict(meta)
            if tags.get("album"):
                am, _c = parse_album_string(tags["album"])
                for f in ("venue", "city", "state", "source_type", "official"):
                    if getattr(am, f):
                        new_meta[f] = getattr(am, f)
            if actual_artist:
                new_meta["host_artist"] = actual_artist
                if tags.get("artist"):
                    new_meta["artist"] = tags["artist"]
            db.update_show(row["id"], filed_path=str(actual_dir),
                           current_path=str(actual_dir), meta=new_meta)
            reconciled += 1

    library_index._conn().commit()
    result = {"shows_checked": len(rows), "artists_learned": learned_artists,
              "venues_learned": learned_venues, "reconciled": reconciled,
              "missing_from_disk": missing,
              "total_corrections": library_index.corrections_count()}
    db.log("info", "maintenance",
           f"learning pass: {len(rows)} shows checked, "
           f"{len(learned_artists)} artist + {len(learned_venues)} venue "
           f"correction(s) learned, {reconciled} records reconciled, "
           f"{missing} missing from disk")
    return result


# ── Suspect detection ────────────────────────────────────────────────────────

def _flags_for(folder_name: str, tags: dict, meta: dict) -> list:
    flags = []
    album = tags.get("album", "")
    hay = f"{folder_name} | {album}"
    if _SUSPECT_WORDS.search(hay):
        flags.append("label/junk word in name or album")
    if album and not re.search(r"\[[^\]]+\]\s*$", album):
        flags.append("album has no [SOURCE] bracket")
    parts = [p.strip().lower() for p in re.sub(r"\[[^\]]*\]", "", album).split(",")]
    if len(parts) != len(set(parts)) and len(parts) > 2:
        flags.append("repeated location parts in album")
    m, _ = parse_album_string(album) if album else (None, False)
    if m:
        from . import places
        if m.state and len(m.state) > 3 and m.state.lower() not in _US_STATES \
                and not places.is_country(m.state) \
                and not re.match(r"^[A-Za-z]{2,14}$", m.state):
            flags.append(f"odd state: {m.state!r}")
        if m.city and " - " in m.city:
            flags.append("dash-composed city")
    art = tags.get("artist", "")
    if art and re.search(r"\d\.\d|[<>=]", art):
        flags.append(f"suspicious artist tag: {art!r}")
    if not re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", folder_name):
        flags.append("no date in folder name")
    return flags


def _title_problems(folder: Path, cap: int = 60) -> list:
    """Assess the written TITLE tags of a filed show and flag anything off:
    duplicative titles, missing/placeholder titles, or junk titles."""
    titles, total_audio = [], 0
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() in AUDIO_EXTS and f.is_file():
            total_audio += 1
            if f.suffix.lower() in TAGGABLE_EXTS and len(titles) < cap:
                try:
                    t = (MutagenFile(f, easy=True).get("title") or [""])[0].strip()
                except Exception:
                    t = ""
                titles.append((t, f.stem))
    if total_audio <= 1:
        return []

    flags = []
    present = [t for t, _ in titles if t]
    # 1) missing / placeholder titles
    _placeholder = re.compile(
        r"^(track|audio|untitled|title)\s*\d*$|^d?\d{1,2}t?\d{1,3}$|^\d{1,3}$",
        re.IGNORECASE)
    missing = sum(1 for t, stem in titles
                  if not t or _placeholder.match(t) or t.lower() == stem.lower())
    if titles and missing / len(titles) >= 0.5:
        flags.append(f"missing/placeholder titles: {missing} of {len(titles)} tracks")

    # 2) duplicative titles (mass-tag)
    if len(present) > 3:
        from collections import Counter
        top, n = Counter(t.lower() for t in present).most_common(1)[0]
        if n / len(present) >= 0.8 and n >= 4:
            flags.append(f"duplicative titles: {n} of {len(present)} are {top!r:.40}")

    # 3) junk titles: filenames, timestamps, digit-soup in the title
    junk = 0
    for t, _ in titles:
        if not t:
            continue
        if re.search(r"\.(flac|shn|wav|mp3)\b", t, re.IGNORECASE) \
                or re.match(r"^\d{1,2}:\d{2}", t) \
                or (sum(c.isdigit() for c in t) / max(len(t), 1) > 0.5):
            junk += 1
    if junk >= 2:
        flags.append(f"junk titles (filenames/timestamps): {junk} track(s)")

    return flags


def _duplicative_titles(folder: Path, cap: int = 40):
    """Back-compat shim; superseded by _title_problems."""
    probs = _title_problems(folder, cap)
    dup = [p for p in probs if p.startswith("duplicative")]
    return dup[0] if dup else ""


def run_suspects_job():
    db.kv_set("audit_suspects", json.dumps({"status": "running", "scanned": 0}))
    try:
        def tick(n):
            db.kv_set("audit_suspects", json.dumps({"status": "running", "scanned": n}))
        rows = find_suspects(progress=tick)
        db.kv_set("audit_suspects", json.dumps({"status": "done", "rows": rows}))
    except Exception as e:
        db.kv_set("audit_suspects", json.dumps({"status": "error", "error": str(e)}))


def find_suspects(progress=None) -> list:
    """Suspicious shows among those Reelarr handled (not the whole library —
    a 20k-show walk made this take minutes and look broken)."""
    cfg = config.load()
    library_dir = Path(cfg["paths"]["library_dir"])
    suspects = []
    for i, row in enumerate(db.list_shows(status="filed", limit=100000), 1):
        if progress and i % 25 == 0:
            progress(i)
        p = Path(row["filed_path"] or "")
        if not p.is_dir():
            suspects.append({"path": row["filed_path"], "artist_folder": "",
                             "album": "", "flags": ["filed but missing from disk"]})
            continue
        tags = _first_tags(p)
        flags = _flags_for(p.name, tags, {})
        flags.extend(_title_problems(p))
        if flags:
            suspects.append({"path": str(p), "artist_folder": p.parent.name,
                             "album": tags.get("album", ""), "flags": flags})
    artist_names = []
    if library_dir.is_dir():
        artist_names = [d.name for d in library_dir.iterdir()
                        if d.is_dir() and not d.name.startswith((".", "_"))]
    # artist folders that look like near-duplicates of each other
    norms = {_norm(a): a for a in artist_names}
    seen = set()
    for n1, a1 in norms.items():
        match = difflib.get_close_matches(
            n1, [n for n in norms if n != n1 and n not in seen], n=1, cutoff=0.87)
        if match:
            seen.add(n1)
            suspects.append({"path": "", "artist_folder": a1,
                             "album": "",
                             "flags": [f"artist folder resembles: {norms[match[0]]!r}"]})
    return suspects


# ── Export for Claude ────────────────────────────────────────────────────────

def export_report() -> str:
    """Everything Reelarr did + how it looks now, as one JSON file."""
    rows = db.list_shows(limit=100000)
    shows = []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        entry = {"id": r["id"], "status": r["status"],
                 "original_folder": r["folder_name"],
                 "confidence": r["confidence"],
                 "provenance": json.loads(r["provenance"] or "{}"),
                 "recorded_meta": {k: v for k, v in meta.items()
                                   if k != "tracks" and v},
                 "track_count": len(meta.get("tracks", []) or []),
                 "filed_path": r["filed_path"], "notes": r["notes"]}
        p = Path(r["filed_path"] or "")
        if r["status"] == "filed" and p.is_dir():
            entry["on_disk"] = {"folder": p.name, "tags": _first_tags(p)}
        shows.append(entry)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": ("Reelarr audit export — original names vs decisions vs "
                    "current on-disk state. Upload to Claude to analyse "
                    "systematic tagging mistakes and improve the rules."),
        "totals": db.counts(),
        "corrections_learned": library_index.corrections_count(),
        "suspects": find_suspects(),
        "shows": shows,
    }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / f"reelarr_audit_{time.strftime('%Y%m%d_%H%M')}.json"
    out.write_text(json.dumps(report, indent=1))
    db.log("info", "maintenance", f"audit report exported: {out.name} "
           f"({len(shows)} shows, {len(report['suspects'])} suspects)")
    return str(out)


# ── Return suspects to the watch folder ──────────────────────────────────────────────

def return_suspects_to_watch() -> dict:
    """Move every flagged show back into the watch folder so the current
    (repaired) rules reprocess it. Records flip back to pending; emptied
    artist/year folders are pruned."""
    from datetime import datetime
    from . import fileops, perms
    cfg = config.load()
    if fileops.dry_run_enabled():
        n = sum(1 for s in find_suspects() if s.get("path") and Path(s["path"]).is_dir())
        db.log("info", "maintenance", f"dry run: would send {n} suspect(s) back to the "
               f"watch folder — nothing moved")
        return {"moved": 0, "would_move": n, "dry_run": True}
    watch = Path(cfg["paths"]["watch_dir"])
    library = Path(cfg["paths"]["library_dir"])
    moved, skipped = 0, 0
    for s in find_suspects():
        p = Path(s["path"]) if s.get("path") else None
        if not p or not p.is_dir():
            skipped += 1
            continue
        dest = watch / p.name
        if dest.exists():
            dest = watch / f"{p.name} ({datetime.now():%H%M%S})"
        parent = p.parent
        row = db.get_show_by_filed(str(p))
        b = fileops.Batch(f"send back {p.name}", kind="send_back",
                          show_id=row["id"] if row else None, dry_run=False,
                          undo_state=fileops.show_undo_state(row["id"]) if row else {})
        b.move(p, dest)
        perms.fix_tree(dest)
        if row:
            db.update_show(row["id"], status="pending", current_path=str(dest),
                           filed_path="", notes="sent back to the watch folder (suspect)")
        # prune emptied year/artist folders, never past the library root
        while parent != library and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        moved += 1
    db.log("info", "maintenance",
           f"sent {moved} suspect(s) back to the watch folder"
           + (f", {skipped} had no folder to move" if skipped else ""))
    return {"moved": moved, "skipped": skipped}


def run_return_job():
    db.kv_set("audit_return", json.dumps({"status": "running"}))
    try:
        r = return_suspects_to_watch()
        r["status"] = "done"
        db.kv_set("audit_return", json.dumps(r))
    except Exception as e:
        db.kv_set("audit_return", json.dumps({"status": "error", "error": str(e)}))


# ── Background wrappers (long jobs must not block HTTP requests) ────────────

def run_export_job():
    db.kv_set("audit_export", json.dumps({"status": "running"}))
    try:
        path = export_report()
        rep = json.loads(Path(path).read_text())
        db.kv_set("audit_export", json.dumps(
            {"status": "done", "name": Path(path).name,
             "shows": len(rep["shows"]), "suspects": len(rep["suspects"])}))
    except Exception as e:
        db.kv_set("audit_export", json.dumps({"status": "error", "error": str(e)}))


def run_learn_job():
    db.kv_set("audit_learn", json.dumps({"status": "running"}))
    try:
        r = learn_from_corrections()
        r["status"] = "error" if "error" in r else "done"
        db.kv_set("audit_learn", json.dumps(r))
    except Exception as e:
        db.kv_set("audit_learn", json.dumps({"status": "error", "error": str(e)}))


def job_state(key: str) -> dict:
    raw = db.kv_get(key)
    try:
        return json.loads(raw) if raw else {"status": "idle"}
    except Exception:
        return {"status": "idle"}


def list_exports() -> list:
    if not EXPORT_DIR.is_dir():
        return []
    return sorted((p.name for p in [*EXPORT_DIR.glob("reelarr_audit_*.json"), *EXPORT_DIR.glob("barbosa_audit_*.json")]),
                  reverse=True)[:10]

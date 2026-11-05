"""Library re-check — a library-wide re-tag pass against setlist.fm.

Walks every filed show, looks it up on setlist.fm by artist+date, and where
the match is trustworthy proposes venue-name normalization and junk-title
cleanup. High-confidence fixes are applied and the audio re-tagged in place;
uncertain ones are routed to Review for your eye. Nothing good is
ever overwritten with something worse — a fix must be a clear improvement.
"""
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from . import fileops
from . import config, database as db, library_index, sources
from .metadata import (ShowMeta, AUDIO_EXTS, TAGGABLE_EXTS, parse_album_string,
                       is_junk_track_title, clean_track_title)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _title_is_junk(t: str) -> bool:
    """A title worth replacing: empty, placeholder, filename, timestamp, or
    mostly digits."""
    t = (t or "").strip()
    if not t:
        return True
    if is_junk_track_title(t):
        return True
    if re.match(r"^(track|audio|untitled|title)\s*\d*$", t, re.IGNORECASE):
        return True
    if re.search(r"\.(flac|shn|wav|mp3)\b", t, re.IGNORECASE):
        return True
    if re.match(r"^\d{1,2}[:.]\d{2}", t):        # leading timestamp
        return True
    if re.match(r"^d?\d{1,2}t\d{1,3}$", t, re.IGNORECASE):   # d1t03 leftovers
        return True
    if sum(c.isdigit() for c in t) / max(len(t), 1) > 0.5:
        return True
    return False


def _artist_for(folder: Path, library: Path, tag_artist: str = "") -> str:
    """Who to ask setlist.fm about: the show's own ARTIST tag, else its artist
    folder — the one directly under the library, even with year folders
    (Artist/1977/show) in between."""
    if tag_artist:
        return tag_artist
    try:
        rel = folder.resolve().relative_to(library.resolve())
    except ValueError:
        return folder.parent.name
    return rel.parts[0] if len(rel.parts) > 1 else ""


def _read_show_tags(folder: Path):
    """Return (album_meta_from_tags, [(file,title)]) for a filed show.

    The embedded ALBUM tag is the authority — Reelarr wrote it as a clean
    'YYYY-MM-DD Venue, City, ST [SOURCE ...]' with no artist prefix, so parse
    that. Fall back to the folder name only when tags are missing/unreadable.
    """
    from mutagen import File as MutagenFile
    from .metadata import parse_folder_name

    album_tag = ""
    meta_artist = [""]
    titles = []
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() in AUDIO_EXTS and f.is_file():
            t = ""
            if f.suffix.lower() in TAGGABLE_EXTS:
                try:
                    easy = MutagenFile(f, easy=True)
                    t = (easy.get("title") or [""])[0].strip()
                    if not album_tag:
                        album_tag = (easy.get("album") or [""])[0].strip()
                    if not meta_artist[0]:
                        meta_artist[0] = (easy.get("artist") or [""])[0].strip()
                except Exception as e:
                    db.log("warn", "backtag", f"{f.name}: tags unreadable ({e})")
                    t = ""
            titles.append((f, t))

    # parse the clean album tag; fall back to the folder name if it's empty
    meta, _ = parse_album_string(album_tag) if album_tag else (ShowMeta(), None)
    if not meta.date:
        meta2, _ = parse_album_string(folder.name)
        if meta2.date:
            meta = meta2
        else:
            fm = parse_folder_name(folder.name, {})   # handles glued prefix
            meta.date = fm.date
            if not meta.venue:
                meta.venue, meta.city, meta.state = fm.venue, fm.city, fm.state
            if not meta.source_type:
                meta.source_type = fm.source_type
    meta.artist = meta_artist[0]
    return meta, titles


def _propose(folder: Path, cfg: dict):
    """Compare a filed show against setlist.fm. Returns a proposal dict or None.
    verdict ∈ {'apply','brig','skip'}."""
    key = cfg["sources"].get("setlistfm_api_key", "")
    if not key:
        return None
    fmeta, titled = _read_show_tags(folder)
    artist = _artist_for(folder, Path(cfg["paths"]["library_dir"]), fmeta.artist)
    date = fmeta.date
    if not (artist and date):
        return {"folder": str(folder), "verdict": "skip",
                "reason": "no artist/date to match on"}

    slf = sources.fetch_setlistfm(artist, date, key)
    if not slf:
        return {"folder": str(folder), "verdict": "skip",
                "reason": "no setlist.fm match for this date"}

    # ── venue sanity check: artist+date matched, but confirm the venue looks
    # like the same place before trusting it (moderate matching) ──
    cur_venue = fmeta.venue
    venue_sim = _sim(cur_venue, slf.venue) if cur_venue and slf.venue else 0.0
    venue_ok = (not cur_venue) or venue_sim >= 0.55 or \
        _norm(cur_venue) in _norm(slf.venue) or _norm(slf.venue) in _norm(cur_venue)

    changes = {}
    # venue normalization
    if slf.venue and _norm(slf.venue) != _norm(cur_venue):
        changes["venue"] = {"from": cur_venue, "to": slf.venue}
    if slf.city and _norm(slf.city) != _norm(fmeta.city):
        changes["city"] = {"from": fmeta.city, "to": slf.city}
    if slf.state and _norm(slf.state) != _norm(fmeta.state):
        changes["state"] = {"from": fmeta.state, "to": slf.state}

    # title cleanup: only junk titles, and only where the setlist pairs with
    # the files cleanly — the same pairing filing uses, so a tuning track or an
    # encore break can't shift every title by one
    audio = [f for f, _ in titled]
    current = {f.name: t for f, t in titled}
    title_fixes = []
    counts_align = False
    if slf.tracks and any(_title_is_junk(t) for t in current.values()):
        from . import align
        pairing = align.plan(audio, slf.tracks)
        counts_align = bool(pairing["ok"])
        if counts_align:
            for e in pairing["entries"]:
                cur, new = current.get(e["name"], ""), (e.get("title") or "")
                if e["kind"] == "song" and new and _title_is_junk(cur) \
                        and _norm(new) != _norm(cur):
                    title_fixes.append({"file": e["name"], "from": cur, "to": new})
        else:
            title_fixes = [{"file": "", "from": "", "to": ""}]   # flags it for Review

    if not changes and not title_fixes:
        return {"folder": str(folder), "verdict": "skip",
                "reason": "already clean / nothing to improve"}

    # ── decide confidence ──
    # venue changes are auto-safe only when the venue clearly matched; title
    # changes are auto-safe when counts align (junk → real names). Otherwise Review.
    venue_change = any(k in changes for k in ("venue", "city", "state"))
    reasons = []
    verdict = "apply"
    if venue_change and not venue_ok:
        verdict = "brig"
        reasons.append(f"venue mismatch (current {cur_venue!r} vs "
                       f"setlist.fm {slf.venue!r}, {venue_sim:.0%} similar)")
    if title_fixes and not counts_align:
        verdict = "brig"
        title_fixes = []
        reasons.append("setlist.fm's songs don't pair cleanly with the files: " +
                       "check the titles by hand")
    # a huge venue rewrite with weak similarity is suspicious even if it passed
    if "venue" in changes and 0 < venue_sim < 0.4:
        verdict = "brig"
        reasons.append("venue name changes drastically")

    return {"folder": str(folder), "verdict": verdict,
            "artist": artist, "date": date,
            "venue_sim": round(venue_sim, 2),
            "changes": changes, "title_fixes": title_fixes,
            "slf_venue": slf.venue, "reason": "; ".join(reasons) or "clean match"}


def _write_title(f: Path, new: str):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2
    if f.suffix.lower() == ".flac":
        a = FLAC(f)
        a["TITLE"] = new
        a.save()
        return
    try:
        a = ID3(f)
    except ID3NoHeaderError:
        a = ID3()
    a.setall("TIT2", [TIT2(encoding=3, text=new)])
    a.save(f)


def _write_album(f: Path, album: str, location: str):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB
    if f.suffix.lower() == ".flac":
        a = FLAC(f)
        a["ALBUM"] = album
        if location:
            a["COMMENT"] = location
        a.save()
        return
    try:
        a = ID3(f)
    except ID3NoHeaderError:
        a = ID3()
    a.setall("TALB", [TALB(encoding=3, text=album)])
    a.save(f)


def renamed_folder(name: str, date: str, new_album: str) -> str:
    """The show folder's new name: whatever comes before the date (the artist
    prefix, in either naming style) is kept and the rest becomes the new album
    title. '' when the folder name doesn't contain the show's date — then it's
    left as it is."""
    i = name.find(date) if date else -1
    if i < 0:
        return ""
    return re.sub(r'[<>:"/\\|?*]', "", name[:i] + new_album).strip()


def _apply(proposal: dict, cfg: dict) -> bool:
    """Apply an approved proposal: rewrite tags and rename the folder to match.
    Every change goes through one undoable batch. Returns True only if every
    file was written."""
    from . import perms
    folder = Path(proposal["folder"])
    if not folder.is_dir():
        return False
    fmeta, titled = _read_show_tags(folder)
    ch = proposal.get("changes", {})
    for k in ("venue", "city", "state"):
        if k in ch:
            setattr(fmeta, k, ch[k]["to"])

    row0 = db.get_show_by_filed(proposal["folder"])
    b = fileops.Batch(f"backtag {folder.name}", kind="backtag",
                      show_id=row0["id"] if row0 else None, dry_run=False,
                      undo_state=fileops.show_undo_state(row0["id"]) if row0 else {})
    failed = []

    fixes = {tf["file"]: tf["to"] for tf in proposal.get("title_fixes", []) if tf.get("file")}
    for f, _cur in titled:
        if f.name in fixes:
            new = clean_track_title(fixes[f.name])
            try:
                b.retag(f, lambda f, new=new: _write_title(f, new), {"title": new})
            except Exception as e:
                failed.append(f"{f.name}: {e}")

    if any(k in ch for k in ("venue", "city", "state")):
        if not fmeta.source_type and not fmeta.bracket_override:
            fmeta.source_type = cfg["filing"].get("default_source_type", "AUD")
        album = fmeta.album_title
        location = ", ".join(p for p in (fmeta.venue, fmeta.city, fmeta.state) if p)
        for f, _cur in titled:
            if f.suffix.lower() not in TAGGABLE_EXTS:
                continue
            try:
                b.retag(f, lambda f: _write_album(f, album, location), {"album": album})
            except Exception as e:
                failed.append(f"{f.name}: {e}")
        new_name = renamed_folder(folder.name, fmeta.date, album)
        dest = folder.parent / new_name if new_name else folder
        if not failed and new_name and dest != folder and not dest.exists():
            try:
                b.move(folder, dest)
                folder = dest
            except OSError as e:
                failed.append(f"rename to {new_name}: {e}")
        elif new_name and dest.exists() and dest != folder:
            db.log("warn", "backtag", f"{folder.name}: not renamed, {new_name} already exists")

    try:
        perms.fix_tree(folder)
    except OSError as e:
        db.log("warn", "backtag", f"{folder.name}: permissions not fixed ({e})")

    if failed:
        db.log("error", "backtag", f"{folder.name}: {len(failed)} change(s) failed — "
               + "; ".join(failed[:5]) + " (Activity → Undo puts back what was changed)")

    # update the DB row + seed the gazetteer with the confirmed data
    row = db.get_show_by_filed(proposal["folder"])
    if row:
        m = json.loads(row["meta"] or "{}")
        m.update({"venue": fmeta.venue, "city": fmeta.city, "state": fmeta.state})
        db.update_show(row["id"], filed_path=str(folder), current_path=str(folder), meta=m)
    if not failed and "venue" in ch and fmeta.venue and fmeta.city:
        c = library_index._conn()
        library_index._upsert_venue(c, fmeta.venue, fmeta.city, fmeta.state)
        c.commit()
    # teach the venue correction so the live pipeline fixes it too
    if not failed and "venue" in ch and ch["venue"]["from"]:
        library_index.correction_set("venue", ch["venue"]["from"],
                                     {"venue": fmeta.venue, "city": fmeta.city,
                                      "state": fmeta.state})
    return not failed


def _iter_library_shows(library: Path):
    """Yield every leaf show folder (contains audio) under the library."""
    for artist_dir in sorted(p for p in library.iterdir() if p.is_dir()
                             and not p.name.startswith((".", "_"))):
        for sub in sorted(artist_dir.rglob("*")):
            if not sub.is_dir():
                continue
            if any(f.suffix.lower() in AUDIO_EXTS for f in sub.iterdir()
                   if f.is_file()):
                yield sub


def run_backtag(dry_run: bool = True, progress=None) -> dict:
    """Walk the library, propose fixes from setlist.fm. dry_run=True only
    reports; dry_run=False applies high-confidence fixes and routes the rest
    to Review. Rate-limited to respect setlist.fm."""
    cfg = config.load()
    forced_preview = False
    if not dry_run and fileops.dry_run_enabled():
        # global dry-run beats the per-tool switch: a library-wide retag is
        # exactly the kind of thing dry-run exists to stop
        dry_run, forced_preview = True, True
    if not cfg["sources"].get("setlistfm_api_key"):
        return {"error": "setlist.fm API key required — set it in Settings"}
    library = Path(cfg["paths"]["library_dir"])
    if not library.is_dir():
        return {"error": "library folder not found"}

    shows = list(_iter_library_shows(library))
    total = len(shows)
    applied, brigged, skipped, errors = 0, 0, 0, 0
    proposals = []
    delay = float(cfg["sources"].get("setlistfm_delay_seconds", 1.1))

    for i, folder in enumerate(shows, 1):
        if progress:
            progress(i, total, folder.name)
        try:
            p = _propose(folder, cfg)
        except Exception as e:
            errors += 1
            db.log("warn", "backtag", f"{folder.name}: {e}")
            continue
        if not p or p["verdict"] == "skip":
            skipped += 1
        elif dry_run:
            proposals.append(p)
        elif p["verdict"] == "apply":
            if _apply(p, cfg):
                applied += 1
                db.log("info", "backtag",
                       f"{folder.name}: applied "
                       + ", ".join(f"{k}→{v['to']}" for k, v in p["changes"].items())
                       + (f", {len(p['title_fixes'])} title(s)" if p["title_fixes"] else ""))
            else:
                errors += 1
        elif p["verdict"] == "brig":
            _send_to_brig(p, cfg)
            brigged += 1
        # collect a capped sample of proposals even in apply mode for the report
        if not dry_run and p and len(proposals) < 200 and p["verdict"] != "skip":
            proposals.append(p)
        time.sleep(delay)     # be kind to setlist.fm

    result = {"total": total, "applied": applied, "brigged": brigged,
              "skipped": skipped, "errors": errors, "dry_run": dry_run,
              "forced_preview": forced_preview,
              "proposals": proposals[:200]}
    db.log("info", "backtag",
           f"backtag {'preview' if dry_run else 'run'} complete: "
           f"{total} shows, {applied} fixed, {brigged} to review, {skipped} clean")
    return result


def _send_to_brig(proposal: dict, cfg: dict) -> None:
    """Flag a filed show for review with the proposed setlist.fm changes noted,
    without moving files — you reviews it in place."""
    folder = Path(proposal["folder"])
    row = db.get_show_by_filed(str(folder))
    note = "Backtag review: " + (proposal.get("reason") or "check against setlist.fm")
    if proposal.get("changes"):
        note += " | proposed: " + ", ".join(
            f"{k} {v['from']!r}→{v['to']!r}" for k, v in proposal["changes"].items())
    if row:
        db.update_show(row["id"], status="review", notes=note)
    else:
        db.upsert_show(str(folder), folder_name=folder.name, status="review",
                       confidence=60, current_path=str(folder),
                       filed_path=str(folder),
                       meta={"venue": proposal.get("slf_venue", "")}, notes=note)


# ── Background job wrappers (poll via kv) ────────────────────────────────────

def run_backtag_job(dry_run: bool = True):
    db.kv_set("backtag", json.dumps({"status": "running", "done": 0, "total": 0}))

    def progress(done, total, name):
        db.kv_set("backtag", json.dumps(
            {"status": "running", "done": done, "total": total, "current": name}))

    try:
        r = run_backtag(dry_run=dry_run, progress=progress)
        r["status"] = "done"
        db.kv_set("backtag", json.dumps(r))
    except Exception as e:
        db.kv_set("backtag", json.dumps({"status": "error", "error": str(e)}))

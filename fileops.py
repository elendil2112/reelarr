"""Every change Reelarr makes to files on disk goes through here.

Three guarantees:
  1. Nothing is deleted. "Removing" a file moves it to a trash folder on the
     same drive (<root>/.reelarr-trash/<date>/<batch>/…), purged only after
     safety.trash_days. Same drive means the move is an instant rename, even
     for a 2 GB show.
  2. Everything is journalled. Moves, trashes, created folders and tag
     rewrites are recorded per batch (one batch ≈ one action you'd recognise:
     "filed this show", "replaced that one"), so a batch can be undone.
  3. Dry-run is enforced here, not by each caller remembering. A dry-run
     batch records what it WOULD do and touches nothing.

Usage:
    with fileops.batch("file show", kind="file", show_id=7) as b:
        b.move(src, dst)
        b.trash(junk, "spectrogram")
"""
import json
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, database as db, paths

TRASH_DIRNAME = ".reelarr-trash"


class DryRun(Exception):
    """Raised by callers that can't sensibly plan past a point."""


# ── Trash location ───────────────────────────────────────────────────────────

def _roots() -> list:
    cfg = config.load()
    roots = [cfg["paths"].get("library_dir"), cfg["paths"].get("watch_dir"),
             cfg.get("torrents", {}).get("watch_dir")]
    out = []
    for r in roots:
        if r:
            try:
                out.append(Path(r).resolve())
            except OSError:
                pass
    # longest first, so a watch folder nested inside the library (a bad setup,
    # but possible) trashes into the nearer root
    return sorted(set(out), key=lambda p: len(p.parts), reverse=True)


def trash_root_for(path: Path) -> tuple:
    """(root, path relative to it). Falls back to <config>/trash."""
    rp = Path(path).resolve()
    for root in _roots():
        try:
            return root, rp.relative_to(root)
        except ValueError:
            continue
    fallback = paths.config_dir()
    return fallback, Path(rp.name)


def _trash_dir(root: Path) -> Path:
    if root == paths.config_dir():
        return root / "trash"
    return root / TRASH_DIRNAME


def _ensure_trash(trash: Path):
    if not trash.exists():
        trash.mkdir(parents=True, exist_ok=True)
        # keep media servers from indexing trashed copies
        try:
            (trash / ".plexignore").write_text("*\n")
            (trash / ".ignore").write_text("*\n")   # Jellyfin/Emby
            (trash / "README.txt").write_text(
                "Files Reelarr removed. Each dated folder is purged automatically "
                "after the retention period in Settings → Safety. Restore from "
                "Activity → Undo, or move files back by hand.\n")
        except OSError:
            pass


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "op"


# ── Tag snapshots (so a retag can be undone) ─────────────────────────────────

_FLAC_KEYS = ("ARTIST", "ALBUMARTIST", "ALBUM", "DATE", "GENRE", "COMMENT",
              "TITLE", "TRACKNUMBER", "SOURCE", "SOURCEMEDIA")
_ID3_FRAMES = ("TPE1", "TPE2", "TALB", "TDRC", "TCON", "TIT2", "TRCK", "COMM")


def snapshot_tags(path: Path) -> dict:
    """The text tags Reelarr may rewrite, as they are now. {} if unreadable."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".flac":
            from mutagen.flac import FLAC
            a = FLAC(path)
            return {"fmt": "flac",
                    "tags": {k: (list(a[k]) if k in a else None) for k in _FLAC_KEYS}}
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            a = ID3(path)
        except ID3NoHeaderError:
            return {"fmt": "id3", "tags": {k: None for k in _ID3_FRAMES}, "noheader": True}
        out = {}
        for k in _ID3_FRAMES:
            frames = a.getall(k)
            out[k] = [str(t) for f in frames for t in getattr(f, "text", [])] if frames else None
        return {"fmt": "id3", "tags": out}
    except Exception:
        return {}


def restore_tags(path: Path, snap: dict) -> bool:
    if not snap or not Path(path).exists():
        return False
    try:
        if snap["fmt"] == "flac":
            from mutagen.flac import FLAC
            a = FLAC(path)
            for k, v in snap["tags"].items():
                if v is None:
                    if k in a:
                        del a[k]
                else:
                    a[k] = v
            a.save()
            return True
        from mutagen import id3
        try:
            a = id3.ID3(path)
        except id3.ID3NoHeaderError:
            a = id3.ID3()
        for k, v in snap["tags"].items():
            a.delall(k)
            if v:
                frame = getattr(id3, k)
                if k == "COMM":
                    a.add(frame(encoding=3, lang="eng", desc="", text=v))
                else:
                    a.add(frame(encoding=3, text=v))
        a.save(path)
        return True
    except Exception:
        return False


# ── Batches ──────────────────────────────────────────────────────────────────

def dry_run_enabled() -> bool:
    return bool(config.load().get("safety", {}).get("dry_run", False))


class Batch:
    def __init__(self, label: str, kind: str = "", show_id=None,
                 dry_run: bool = None, undo_state: dict = None):
        self.label = label
        self.kind = kind
        self.show_id = show_id
        self.dry_run = dry_run_enabled() if dry_run is None else dry_run
        self.undo_state = undo_state or {}
        self.id = None
        self.seq = 0
        self.plan = []          # dry-run: what would happen
        self.ops = 0
        self._stamp = time.strftime("%Y-%m-%d")

    # journal ----------------------------------------------------------------
    def _open(self):
        if self.id is None and not self.dry_run:
            cur = db._conn().execute(
                "INSERT INTO batches (ts, label, kind, show_id, undo_state) VALUES (?,?,?,?,?)",
                (time.time(), self.label, self.kind, self.show_id,
                 json.dumps(self.undo_state)))
            db._conn().commit()
            self.id = cur.lastrowid

    def _record(self, op: str, src="", dst="", **detail):
        self.ops += 1
        if self.dry_run:
            if any(p.get("op") == op and p.get("src") == str(src) and str(src)
                   for p in self.plan):
                return          # planned already (e.g. junk scrub runs twice)
            self.plan.append({"op": op, "src": str(src), "dst": str(dst), **detail})
            return
        self._open()
        self.seq += 1
        db._conn().execute(
            "INSERT INTO file_ops (batch_id, seq, ts, op, src, dst, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.id, self.seq, time.time(), op, str(src), str(dst), json.dumps(detail)))
        db._conn().commit()

    # operations --------------------------------------------------------------
    def note(self, op: str, **detail):
        """Plan-only description of a step with no single file op behind it
        (e.g. "would extract show.zip"). Ignored when running for real."""
        if self.dry_run:
            self.ops += 1
            self.plan.append({"op": op, **{k: str(v) if isinstance(v, Path) else v
                                          for k, v in detail.items()}})

    def mkdir(self, path: Path) -> Path:
        path = Path(path)
        if path.exists():
            return path
        missing, p = [], path
        while not p.exists() and p != p.parent:
            missing.append(p)
            p = p.parent
        if self.dry_run:
            self._record("mkdir", dst=path)
            return path
        from . import perms
        perms.make_dir(path)
        for m in reversed(missing):
            self._record("mkdir", dst=m)
        return path

    def move(self, src: Path, dst: Path) -> Path:
        src, dst = Path(src), Path(dst)
        if self.dry_run:
            self._record("move", src, dst)
            return dst
        if dst.exists():
            raise FileExistsError(f"refusing to overwrite {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # journal FIRST: if the move dies halfway, the record still says
        # where everything was meant to be.
        self._record("move", src, dst)
        shutil.move(str(src), str(dst))
        return dst

    def rename(self, src: Path, dst: Path) -> Path:
        return self.move(src, dst)

    def trash(self, path: Path, reason: str = "") -> Path:
        path = Path(path)
        if not path.exists() and not self.dry_run:
            return None
        root, rel = trash_root_for(path)
        trash = _trash_dir(root)
        bucket = f"{self.id or 'plan'}-{_slug(self.label)}" if not self.dry_run else "plan"
        dst = trash / self._stamp / bucket / rel
        if self.dry_run:
            self._record("trash", path, dst, reason=reason)
            return dst
        self._open()
        bucket = f"{self.id}-{_slug(self.label)}"
        dst = trash / self._stamp / bucket / rel
        _ensure_trash(trash)
        i = 2
        while dst.exists():
            dst = dst.with_name(f"{rel.name} ({i})")
            i += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._record("trash", path, dst, reason=reason)
        shutil.move(str(path), str(dst))
        return dst

    def retag(self, path: Path, write_fn, changes: dict = None):
        """Rewrite a file's tags via write_fn(path), keeping a snapshot of the
        old values for undo. changes is a short summary used in dry-run plans."""
        path = Path(path)
        if self.dry_run:
            self._record("retag", path, changes=changes or {})
            return
        snap = snapshot_tags(path)
        self._record("retag", path, before=snap)
        write_fn(path)


@contextmanager
def batch(label: str, kind: str = "", show_id=None, dry_run: bool = None,
          undo_state: dict = None):
    b = Batch(label, kind, show_id, dry_run, undo_state)
    yield b


def show_undo_state(show_id) -> dict:
    """Snapshot of the show row fields an undo needs to put back."""
    if not show_id:
        return {}
    row = db.get_show(show_id)
    if not row:
        return {}
    return {k: row[k] for k in ("status", "current_path", "filed_path", "notes")}


# ── Undo ─────────────────────────────────────────────────────────────────────

def list_batches(limit: int = 50, offset: int = 0) -> list:
    rows = db._conn().execute(
        "SELECT b.*, (SELECT COUNT(*) FROM file_ops o WHERE o.batch_id=b.id) n_ops "
        "FROM batches b ORDER BY b.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def batch_ops(batch_id: int) -> list:
    rows = db._conn().execute(
        "SELECT * FROM file_ops WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.get("detail") or "{}")
        d["detail"].pop("before", None)   # tag snapshots are bulky; not for the UI
        out.append(d)
    return out


def undo(batch_id: int) -> dict:
    row = db._conn().execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        return {"error": "no such action"}
    if row["undone_at"]:
        return {"error": "already undone"}
    ops = db._conn().execute(
        "SELECT * FROM file_ops WHERE batch_id=? ORDER BY seq DESC", (batch_id,)).fetchall()

    restored, conflicts = 0, []
    for op in ops:
        kind, src, dst = op["op"], Path(op["src"]) if op["src"] else None, \
            Path(op["dst"]) if op["dst"] else None
        detail = json.loads(op["detail"] or "{}")
        try:
            if kind in ("move", "trash"):
                if not dst.exists():
                    conflicts.append(f"{dst} is gone — can't put {src.name} back")
                    continue
                if src.exists():
                    conflicts.append(f"{src} is occupied — left {dst.name} where it is")
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                restored += 1
            elif kind == "retag":
                if restore_tags(src, detail.get("before") or {}):
                    restored += 1
                else:
                    conflicts.append(f"couldn't restore tags on {src.name}")
            elif kind == "mkdir":
                try:
                    dst.rmdir()          # only if empty — never removes content
                except OSError:
                    pass
        except Exception as e:
            conflicts.append(f"{kind} {src or dst}: {e}")

    state = json.loads(row["undo_state"] or "{}")
    others = state.pop("_other_rows", {}) or {}
    if row["show_id"] and state:
        db.update_show(row["show_id"], **state)
    for sid, st in others.items():
        if st:
            db.update_show(int(sid), **st)
    note = f"restored {restored} item(s)" + (f", {len(conflicts)} problem(s)" if conflicts else "")
    db._conn().execute("UPDATE batches SET undone_at=?, undo_note=? WHERE id=?",
                       (time.time(), note, batch_id))
    db._conn().commit()
    db.log("warn" if conflicts else "info", "undo",
           f"undid “{row['label']}”: {note}" + ("; " + "; ".join(conflicts[:5]) if conflicts else ""),
           row["show_id"])
    return {"ok": True, "restored": restored, "conflicts": conflicts}


# ── Housekeeping ─────────────────────────────────────────────────────────────

def purge_trash(days: int = None) -> int:
    """Delete trash day-folders older than the retention period. This is the
    ONLY place Reelarr permanently removes files, and only from its own trash."""
    days = int(config.load().get("safety", {}).get("trash_days", 30) if days is None else days)
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    purged = 0
    trashes = {_trash_dir(r) for r in _roots()} | {paths.config_dir() / "trash"}
    for trash in trashes:
        if not trash.is_dir():
            continue
        for day in trash.iterdir():
            if not day.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
                continue
            try:
                ts = time.mktime(time.strptime(day.name, "%Y-%m-%d"))
            except ValueError:
                continue
            if ts < cutoff:
                shutil.rmtree(day, ignore_errors=True)
                purged += 1
    if purged:
        db.log("info", "trash", f"purged {purged} day(s) of trash older than {days} days")
    return purged


def prune_journal(days: int = None) -> int:
    days = int(config.load().get("safety", {}).get("undo_keep_days", 90) if days is None else days)
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    c = db._conn()
    ids = [r["id"] for r in c.execute("SELECT id FROM batches WHERE ts < ?", (cutoff,))]
    for i in ids:
        c.execute("DELETE FROM file_ops WHERE batch_id=?", (i,))
        c.execute("DELETE FROM batches WHERE id=?", (i,))
    c.commit()
    return len(ids)

"""Torrent intake — watch a folder, hand new torrents to Deluge, label them.

Drop a .torrent (or a .magnet text file) into the torrent folder and Reelarr
passes it to Deluge with the label you've chosen, then files the .torrent
itself away so it's never added twice. Nothing is deleted by default: handled
files move to _added/, and anything Deluge refused moves to _failed/ so you
can see what happened.
"""
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import clients, config, database as db, perms

_lock = threading.Lock()
_status = {"running": False, "last_run": None, "last_result": ""}

TORRENT_EXTS = (".torrent",)
MAGNET_EXTS = (".magnet", ".magnetlink")


def status() -> dict:
    cfg = config.load()["torrents"]
    watch = Path(cfg["watch_dir"]) if cfg.get("watch_dir") else None
    waiting = 0
    if watch and watch.is_dir():
        waiting = sum(1 for p in watch.iterdir()
                      if p.is_file() and p.suffix.lower() in TORRENT_EXTS + MAGNET_EXTS)
    return {**_status,
            "enabled": bool(cfg.get("enabled")),
            "configured": bool(cfg.get("watch_dir") and cfg.get("url")),
            "client": cfg.get("client", ""),
            "watch_dir_ok": bool(watch and watch.is_dir()),
            "waiting": waiting,
            "label": cfg.get("label", "")}


def _settled(p: Path, seconds: int) -> bool:
    """Don't grab a .torrent that's still being written."""
    try:
        return (time.time() - p.stat().st_mtime) >= seconds
    except OSError:
        return False


def _stash(p: Path, sub: str, keep: bool = True) -> None:
    """Move a handled file out of the watch folder (or delete if asked)."""
    if not keep:
        p.unlink(missing_ok=True)
        return
    dest_dir = p.parent / sub
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / p.name
    if dest.exists():
        dest = dest_dir / f"{p.stem} ({datetime.now():%Y%m%d-%H%M%S}){p.suffix}"
    try:
        p.rename(dest)
    except OSError:
        pass


def scan(force: bool = False) -> dict:
    """Check the torrent folder and hand anything new to Deluge."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "a torrent scan is already running"}
    _status["running"] = True
    try:
        cfg = config.load()["torrents"]
        if not cfg.get("enabled") and not force:
            return {"skipped": "disabled"}
        watch_dir = (cfg.get("watch_dir") or "").strip()
        if not watch_dir:
            _status["last_result"] = "no torrent folder set (Settings → Torrents)"
            return {"error": "no watch_dir"}
        watch = Path(watch_dir)
        if not watch.is_dir():
            _status["last_result"] = f"torrent folder not found: {watch_dir}"
            db.log("warn", "torrent", f"torrent folder not found: {watch_dir}")
            return {"error": "watch_dir missing"}
        if not (cfg.get("url") or "").strip():
            _status["last_result"] = "no download client URL set (Settings → Torrents)"
            return {"error": "no url"}

        settle = int(cfg.get("settle_seconds", 10))
        files = sorted(p for p in watch.iterdir()
                       if p.is_file()
                       and p.suffix.lower() in TORRENT_EXTS + MAGNET_EXTS)
        if not files:
            _status["last_result"] = "nothing waiting"
            return {"added": 0, "found": 0}

        ready = [p for p in files if force or _settled(p, settle)]
        if not ready:
            _status["last_result"] = f"{len(files)} file(s) still being written"
            return {"added": 0, "found": len(files), "waiting": len(files)}

        try:
            client = clients.build(cfg)
            client.connect()
        except clients.DownloadClientError as e:
            _status["last_result"] = f"{e}"
            db.log("error", "torrent", f"download client unreachable: {e}")
            return {"error": str(e)}

        label = cfg.get("label", "")
        dl_dir = (cfg.get("download_dir") or "").strip()
        paused = bool(cfg.get("add_paused"))
        keep = (cfg.get("after_add", "move") or "move") != "delete"

        added = dupes = failed = 0
        for p in ready:
            try:
                if p.suffix.lower() in MAGNET_EXTS:
                    lines = [l.strip() for l in
                             p.read_text(errors="replace").splitlines() if l.strip()]
                    uri = lines[0] if lines else ""
                    if not uri.lower().startswith("magnet:"):
                        raise ValueError("no magnet link inside this file")
                    res = client.add_magnet(uri, label, dl_dir, paused)
                else:
                    # check it really is a torrent before bothering the client —
                    # otherwise a truncated file comes back as a confusing
                    # network error instead of "this file is broken"
                    if p.stat().st_size > clients.base.MAX_TORRENT_BYTES:
                        raise ValueError("far too big to be a .torrent file")
                    try:
                        clients.torrent_infohash(p.read_bytes())
                    except Exception:
                        raise ValueError(
                            "not a readable .torrent file (truncated or corrupt)") from None
                    res = client.add_torrent_file(p, label, dl_dir, paused)
                shown = res.name or p.name
                if res.already:
                    dupes += 1
                    db.log("info", "torrent",
                           f"{client.name} already had {shown}"
                           + (f" — {client.label_term} set to {res.label}"
                              if res.label else ""))
                else:
                    added += 1
                    db.log("info", "torrent",
                           f"sent to {client.name}: {shown}"
                           + (f" [{res.label}]" if res.label
                              else f" (no {client.label_term})"))
                _stash(p, "_added", keep)
            except Exception as e:
                failed += 1
                db.log("error", "torrent", f"{p.name}: {e}")
                _stash(p, "_failed", True)

        bits = []
        if added:
            bits.append(f"{added} sent to {client.name}")
        if dupes:
            bits.append(f"{dupes} already there")
        if failed:
            bits.append(f"{failed} failed")
        _status["last_result"] = ", ".join(bits) or "nothing to do"
        db.kv_set("last_torrent_run", datetime.now().isoformat())
        return {"added": added, "duplicates": dupes, "failed": failed,
                "found": len(ready)}
    except Exception as e:
        _status["last_result"] = f"failed: {e}"
        db.log("error", "torrent", f"torrent scan crashed: {e}")
        return {"error": str(e)}
    finally:
        _status["running"] = False
        _status["last_run"] = datetime.now().isoformat()
        _lock.release()


# ── Bringing finished downloads back in ─────────────────────────────────────

def _imported_file() -> Path:
    from . import paths
    d = paths.config_dir() / "torrents"
    d.mkdir(parents=True, exist_ok=True)
    return d / "imported.txt"


def _load_imported() -> set:
    f = _imported_file()
    if not f.exists():
        return set()
    return {l.strip() for l in f.read_text().splitlines() if l.strip()}


def _mark_imported(tid: str) -> None:
    with _imported_file().open("a") as f:
        f.write(tid + "\n")


def map_path(p: str, mapping: dict) -> str:
    """Translate a path the download client reported into one Reelarr can see.

    The client and Reelarr are usually different containers, so the same folder
    has two names — the client says /downloads/foo, Reelarr sees
    /torrent-downloads/foo. Longest prefix wins so nested rules behave.
    """
    if not p or not mapping:
        return p
    best = ""
    for remote in mapping:
        if (p == remote or p.startswith(remote.rstrip("/") + "/")) \
                and len(remote) > len(best):
            best = remote
    if not best:
        return p
    tail = p[len(best):].lstrip("/\\")
    local = str(mapping[best]).rstrip("/\\")
    return f"{local}/{tail}" if tail else local


def _copy_in(src: Path, watch: Path, move: bool, log) -> Path:
    """Bring one finished download into the watch folder.

    Staged under a dot-name first and renamed into place, so the watcher can
    never pick up a half-copied show.
    """
    name = src.name
    dest = watch / name
    if dest.exists():
        dest = watch / f"{name} ({datetime.now():%Y%m%d-%H%M%S})"
    staging = watch / f".importing-{dest.name}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if src.is_dir():
        if move:
            shutil.move(str(src), str(staging))
        else:
            shutil.copytree(src, staging, symlinks=False)
    else:
        # a single-file torrent: give it a folder so the pipeline sees a show
        staging.mkdir(parents=True)
        if move:
            shutil.move(str(src), str(staging / src.name))
        else:
            shutil.copy2(src, staging / src.name)
    staging.rename(dest)
    try:
        perms.fix_tree(dest)
    except Exception:
        pass
    return dest


def import_completed(force: bool = False) -> dict:
    """Ask the download client what's finished and copy it into the watch
    folder, where the normal sweep picks it up and files it."""
    from . import setup
    if not setup.done():
        return {"skipped": "setup wizard not finished"}
    if not _lock.acquire(blocking=False):
        return {"skipped": "a torrent job is already running"}
    _status["running"] = True
    try:
        cfg = config.load()["torrents"]
        if not cfg.get("import_completed") and not force:
            return {"skipped": "import disabled"}
        if not (cfg.get("url") or "").strip():
            return {"error": "no download client URL set"}
        watch = Path(config.load()["paths"]["watch_dir"])
        if not watch.is_dir():
            db.log("error", "torrent", f"watch folder not found: {watch}")
            return {"error": "watch folder missing"}

        try:
            client = clients.build(cfg)
            client.connect()
        except clients.DownloadClientError as e:
            _status["last_result"] = f"{e}"
            db.log("error", "torrent", f"download client unreachable: {e}")
            return {"error": str(e)}

        label = cfg.get("label", "")
        mapping = cfg.get("path_map") or {}
        move = (cfg.get("import_mode", "copy") or "copy") == "move"
        min_age = float(cfg.get("min_age_minutes", 2) or 0) * 60
        done = _load_imported()

        try:
            torrents_list = client.get_torrents(label)
        except clients.DownloadClientError as e:
            _status["last_result"] = f"couldn't list torrents: {e}"
            db.log("error", "torrent", f"couldn't list torrents: {e}")
            return {"error": str(e)}

        imported = skipped = missing = failed = 0
        for t in torrents_list:
            if not t.finished or not t.id or t.id in done:
                continue
            src = Path(map_path(t.source(), mapping))
            if not src.exists():
                missing += 1
                db.log("warn", "torrent",
                       f"{t.name}: finished, but Reelarr can't see it at "
                       f"{src} — check the path mapping in Settings → Torrents")
                continue
            if min_age:
                try:
                    if (time.time() - src.stat().st_mtime) < min_age:
                        skipped += 1
                        continue
                except OSError:
                    pass
            try:
                dest = _copy_in(src, watch, move, db.log)
                _mark_imported(t.id)
                imported += 1
                db.log("info", "torrent",
                       f"{'moved' if move else 'copied'} finished download into "
                       f"the watch folder: {dest.name}")
            except Exception as e:
                failed += 1
                db.log("error", "torrent", f"couldn't import {t.name}: {e}")

        bits = []
        if imported:
            bits.append(f"{imported} brought into the watch folder")
        if missing:
            bits.append(f"{missing} not visible to Reelarr")
        if failed:
            bits.append(f"{failed} failed")
        if bits:
            _status["last_result"] = ", ".join(bits)
        db.kv_set("last_torrent_import", datetime.now().isoformat())
        return {"imported": imported, "waiting": skipped,
                "unmapped": missing, "failed": failed}
    except Exception as e:
        db.log("error", "torrent", f"import crashed: {e}")
        return {"error": str(e)}
    finally:
        _status["running"] = False
        _lock.release()


def poll() -> dict:
    """The scheduled job: hand out new torrents, then collect finished ones."""
    out = scan()
    cfg = config.load()["torrents"]
    if cfg.get("import_completed"):
        out["import"] = import_completed()
    return out

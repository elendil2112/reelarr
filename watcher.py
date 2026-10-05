"""Watch-folder scanner: finds settled show folders and runs the pipeline."""
import time
from pathlib import Path

from . import config, database as db, fileops, pipeline

SKIP_PREFIXES = (".", "_")
_running = False
_stop_requested = False


def is_running() -> bool:
    return _running


def request_stop() -> dict:
    """Ask a running sweep to stop after it finishes the current show."""
    global _stop_requested
    if not _running:
        return {"running": False, "message": "no sweep is running"}
    _stop_requested = True
    db.log("info", "scan", "stop requested — finishing the current show, then halting")
    return {"running": True, "message": "stopping after the current show"}


_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2",
                     ".tar.xz", ".txz", ".rar", ".7z")


def _unpack_loose_archives(watch: Path) -> None:
    """A show dropped as a single archive file at the top of the watch folder has no
    folder for the pipeline to grab. Give each one its own folder (named after
    the archive) and move it inside, so the normal extractor takes over."""
    dry = fileops.dry_run_enabled()
    for p in sorted(watch.iterdir()):
        if not p.is_file() or p.name.startswith(SKIP_PREFIXES):
            continue
        low = p.name.lower()
        if not low.endswith(_ARCHIVE_SUFFIXES):
            continue
        # strip the full compound suffix (.tar.gz) for the folder name
        stem = p.name
        for suf in sorted(_ARCHIVE_SUFFIXES, key=len, reverse=True):
            if low.endswith(suf):
                stem = p.name[: -len(suf)]
                break
        dest = watch / stem
        i = 2
        while dest.exists():
            dest = watch / f"{stem} ({i})"
            i += 1
        if dry:
            continue        # planning only: leave the archive where it is
        try:
            b = fileops.Batch(f"stage {p.name}", kind="stage", dry_run=False)
            b.mkdir(dest)
            b.move(p, dest / p.name)
            db.log("info", "extract",
                   f"loose archive {p.name} → folder {dest.name}/ for processing")
        except OSError as e:
            db.log("warn", "extract", f"couldn't stage {p.name}: {e}")


def newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for p in path.rglob("*"):
        try:
            m = p.stat().st_mtime
            if m > newest:
                newest = m
        except OSError:
            continue
    return newest


def scan(force: bool = False) -> dict:
    """One pass over the watch folder. force=True skips the settle wait.
    Checks a cooperative stop flag between shows so the GUI can halt it."""
    global _running, _stop_requested
    from . import setup
    if not setup.done():
        return {"skipped": "setup wizard not finished"}
    if _running:
        return {"skipped": "scan already running"}
    _running = True
    _stop_requested = False
    try:
        cfg = config.load()
        watch = Path(cfg["paths"]["watch_dir"])
        if not watch.is_dir():
            db.log("error", "scan", f"watch dir missing: {watch}")
            return {"error": f"watch dir missing: {watch}"}

        settle_seconds = float(cfg["watcher"]["settle_minutes"]) * 60
        dry = fileops.dry_run_enabled()
        now = time.time()
        results = {"filed": 0, "review": 0, "planned": 0, "error": 0,
                   "waiting": 0, "skipped": 0}
        stopped = False

        # a show downloaded as a single loose archive (show.zip dropped straight
        # into the watch folder, not inside a subfolder) — unpack it into its own folder
        # so it becomes a normal show directory the loop can process.
        _unpack_loose_archives(watch)

        def ready(entry) -> bool:
            existing = db.get_show_by_path(str(entry))
            if existing and existing["status"] in ("review", "filed", "processing"):
                return False
            if existing and existing["status"] == "planned" and dry:
                return False
            return force or (now - newest_mtime(entry)) >= settle_seconds

        # early + late shows of one night arrive as two folders: pair them first
        from . import pairs
        candidates = [e for e in sorted(watch.iterdir())
                      if e.is_dir() and not e.name.startswith(SKIP_PREFIXES) and ready(e)]
        partner = {}
        pair_wait = float(cfg["watcher"].get("pair_wait_minutes", 120) or 0) * 60
        try:
            for early, late in pairs.match(candidates, cfg["artists"]["aliases"]):
                partner[early], partner[late] = late, None      # the late one rides along
        except Exception as e:
            db.log("warn", "pair", f"couldn't check for early/late shows: {e}")

        for entry in sorted(watch.iterdir()):
            if _stop_requested:
                stopped = True
                break
            if not entry.is_dir() or entry.name.startswith(SKIP_PREFIXES):
                continue
            if entry in partner and partner[entry] is None:
                continue                                        # handled with its early show
            if entry not in partner and pair_wait and pairs.part_of(entry) \
                    and (now - newest_mtime(entry)) < pair_wait:
                results["waiting"] += 1                         # its other half may still be coming
                continue
            existing = db.get_show_by_path(str(entry))
            if existing and existing["status"] in ("review", "filed", "processing"):
                results["skipped"] += 1
                continue
            if existing and existing["status"] == "planned" and dry:
                results["skipped"] += 1     # already planned; re-plan on demand
                continue
            if not force and (now - newest_mtime(entry)) < settle_seconds:
                results["waiting"] += 1
                continue

            if partner.get(entry):
                outcome = pipeline.process_pair(entry, partner[entry])
            else:
                outcome = pipeline.process_show(entry)
            results[outcome.get("status", "error")] = \
                results.get(outcome.get("status", "error"), 0) + 1

        db.kv_set("last_scan", now)
        results["stopped"] = stopped
        if stopped:
            db.log("info", "scan",
                   f"sweep stopped by request — filed {results['filed']}, "
                   f"review {results['review']}, errors {results['error']} "
                   f"before halting")
        elif any(results[k] for k in ("filed", "review", "planned", "error")):
            db.log("info", "scan",
                   (f"planned {results['planned']} (dry run), " if results["planned"] else "")
                   + f"filed {results['filed']}, review {results['review']}, "
                   f"errors {results['error']}, waiting {results['waiting']}")
        return results
    finally:
        _running = False
        _stop_requested = False

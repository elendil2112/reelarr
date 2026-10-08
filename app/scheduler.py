"""Scheduler: watch-folder polling, library index, housekeeping, and any
provider jobs (providers schedule themselves via providers.schedule_all).
Jobs are rebuilt whenever settings are saved, so schedule changes apply live."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database as db, watcher, library_index, torrents, providers, setup

_scheduler: BackgroundScheduler = None


DEFAULT_TZ = "America/New_York"


def valid_timezone(name: str) -> str:
    """The zone if it exists here, else the default (and say so)."""
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(name or "")
        return name
    except Exception:
        if name:
            db.log("warn", "system", f"unknown time zone {name!r} — using {DEFAULT_TZ}")
        return DEFAULT_TZ


def start():
    global _scheduler
    cfg = config.load()
    _scheduler = BackgroundScheduler(timezone=valid_timezone(cfg.get("timezone")))
    _scheduler.start()
    reschedule()
    if setup.done() and library_index.is_empty():
        cfg = config.load()
        _scheduler.add_job(library_index.scan, args=[cfg["paths"]["library_dir"]])
        db.log("info", "index", "library index empty — building it in the background")


def _housekeeping():
    from . import fileops
    try:
        fileops.purge_trash()
        fileops.prune_journal()
    except Exception as e:
        db.log("warn", "trash", f"housekeeping failed: {e}")
    try:
        from . import notes
        notes.purge()
    except Exception as e:
        db.log("warn", "system", f"clearing old deleted notes failed: {e}")


def stop():
    """On shutdown: let a running job finish its current step, start nothing new."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
    _scheduler = None


def reschedule():
    cfg = config.load()
    tz = valid_timezone(cfg.get("timezone"))
    for job in _scheduler.get_jobs():
        job.remove()

    # trash retention + undo-journal pruning: the only permanent deletions
    # Reelarr makes, and only of its own trash. Runs even before setup.
    try:
        _scheduler.add_job(_housekeeping, CronTrigger(hour=5, minute=15, timezone=tz),
                           id="housekeeping", coalesce=True, max_instances=1,
                           misfire_grace_time=6 * 3600)
    except Exception as e:
        db.log("error", "scheduler", f"could not schedule housekeeping: {e}")

    if not setup.done():
        # nothing touches the folders until a person has confirmed them
        db.log("info", "scheduler", "waiting for the setup wizard — watching is paused")
        return

    if cfg["watcher"]["enabled"]:
        try:
            _scheduler.add_job(
                watcher.scan, IntervalTrigger(minutes=int(cfg["watcher"]["poll_minutes"])),
                id="watch_scan", coalesce=True, max_instances=1)
        except Exception as e:
            db.log("error", "scheduler", f"could not schedule watch scan: {e}")

    try:
        _scheduler.add_job(
            library_index.scan, CronTrigger(hour=4, minute=30, timezone=tz),
            args=[cfg["paths"]["library_dir"]],
            id="library_index", coalesce=True, max_instances=1,
            misfire_grace_time=3600)
    except Exception as e:
        db.log("error", "scheduler", f"could not schedule index scan: {e}")

    if cfg.get("torrents", {}).get("enabled"):
        try:
            _scheduler.add_job(
                torrents.poll,
                IntervalTrigger(minutes=int(cfg["torrents"].get("poll_minutes", 5))),
                id="torrent_scan", coalesce=True, max_instances=1)
        except Exception as e:
            db.log("error", "scheduler", f"could not schedule torrent scan: {e}")

    mon = cfg.get("monitor", {})
    if mon.get("enabled", True):
        try:
            from . import monitor
            _scheduler.add_job(
                monitor.run_all, IntervalTrigger(hours=max(1, float(mon.get("interval_hours", 12)))),
                id="monitor", coalesce=True, max_instances=1)
        except Exception as e:
            db.log("error", "scheduler", f"could not schedule artist monitoring: {e}")

    providers.schedule_all(_scheduler)

    db.log("info", "scheduler", f"watch every {cfg['watcher']['poll_minutes']}m")


def next_runs():
    out = {}
    if _scheduler:
        for job in _scheduler.get_jobs():
            out[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
    return out


def run_in_background(fn, *args):
    _scheduler.add_job(fn, args=args)

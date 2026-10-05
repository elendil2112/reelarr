"""System page: health checks and configuration backups."""
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from . import auth, config, database as db, fileops, migrations, paths, setup
from .version import __version__

BACKUP_PREFIX = "reelarr-backup-"
KEEP = 10


def _check(name, ok, detail, level=None):
    return {"name": name, "ok": bool(ok), "level": level or ("ok" if ok else "error"),
            "detail": detail}


def _free(path: str) -> str:
    try:
        u = shutil.disk_usage(path)
        return f"{u.free / 1e9:,.0f} GB free of {u.total / 1e9:,.0f} GB"
    except OSError:
        return "unknown"


def health() -> list:
    cfg = config.load()
    out = []
    st = migrations.status()
    stale = [k for k, v in st["latest"].items() if st.get(k) not in (None, v)]
    out.append(_check("Database", not stale,
                      "up to date" if not stale else f"not migrated: {', '.join(stale)}"))
    out.append(_check("Setup", setup.done(),
                      "finished" if setup.done() else "the setup wizard hasn't been finished — nothing is being watched",
                      None if setup.done() else "warn"))
    mp = setup.media_problem()
    if mp:
        out.append(_check("Media folders", False,
                          "Docker hasn't given Reelarr any folders with music in them — "
                          "see the setup wizard's Folders step for the fix", "warn"))
    for label, key in (("Watch folder", "watch_dir"), ("Library", "library_dir")):
        p = Path(cfg["paths"][key])
        if not p.is_dir():
            out.append(_check(label, False, f"{p} isn't reachable"))
        elif not os.access(p, os.W_OK):
            out.append(_check(label, False, f"{p} isn't writable by uid {os.getuid() if hasattr(os, 'getuid') else '?'}"))
        else:
            out.append(_check(label, True, f"{p} — {_free(str(p))}"))
    t = cfg.get("torrents", {})
    if t.get("enabled") and t.get("url"):
        try:
            from . import clients
            info = clients.build(t).test()
            out.append(_check("Download client", True, f"{info.get('client')} {info.get('version', '')}".strip()))
        except Exception as e:
            out.append(_check("Download client", False, str(e)))
    else:
        out.append(_check("Download client", True, "not used", "info"))
    key = cfg.get("sources", {}).get("setlistfm_api_key")
    out.append(_check("setlist.fm", bool(key), "key saved" if key else
                      "no key — more shows will need review", None if key else "warn"))
    out.append(_check("Login", auth.mode() == "forms",
                      "on" if auth.mode() == "forms" else "OFF — anyone who can reach this port has full control",
                      None if auth.mode() == "forms" else "warn"))
    out.append(_check("Dry run", True, "on — planning only" if fileops.dry_run_enabled() else "off — live", "info"))
    b = list_backups()
    if b:
        age_days = (time.time() - b[0]["mtime"]) / 86400
        out.append(_check("Backup", age_days < 30, f"newest is {age_days:.0f} day(s) old",
                          None if age_days < 30 else "warn"))
    else:
        out.append(_check("Backup", False, "none yet — make one below", "warn"))
    return out


# ── Backups ──────────────────────────────────────────────────────────────────

def list_backups() -> list:
    out = []
    for p in sorted(paths.backups_dir().glob(BACKUP_PREFIX + "*.zip"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        out.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return out


def make_backup() -> dict:
    """A consistent snapshot of every database plus settings.json. Restoring
    is: stop Reelarr, unzip into the config folder, start it again."""
    cfg_dir = paths.config_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = paths.backups_dir() / f"{BACKUP_PREFIX}{__version__}-{stamp}.zip"
    tmp = paths.backups_dir() / f".{dest.name}.part"
    work = paths.backups_dir() / f".snap-{stamp}"
    work.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("reelarr.db", "library_index.db"):
                src = cfg_dir / name
                if not src.exists():
                    continue
                snap = work / name
                s = sqlite3.connect(src)
                try:
                    d = sqlite3.connect(snap)
                    try:
                        s.backup(d)
                    finally:
                        d.close()
                finally:
                    s.close()
                z.write(snap, name)
            if (cfg_dir / "settings.json").exists():
                z.write(cfg_dir / "settings.json", "settings.json")
            z.writestr("BACKUP.txt",
                       f"Reelarr {__version__} backup, {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                       "Contains settings.json, which holds passwords and API keys — keep it private.\n"
                       "To restore: stop Reelarr, unzip into the config folder, start it.\n")
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp.exists():
            tmp.unlink()
    for old in list_backups()[KEEP:]:
        try:
            (paths.backups_dir() / old["name"]).unlink()
        except OSError:
            pass
    db.log("info", "system", f"backup made: {dest.name}")
    return {"name": dest.name, "size": dest.stat().st_size}


def backup_path(name: str):
    p = paths.backups_dir() / Path(name).name
    if not p.name.startswith(BACKUP_PREFIX) or not p.is_file():
        return None
    return p

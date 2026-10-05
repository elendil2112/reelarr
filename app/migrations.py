"""Schema migrations for every piece of state Reelarr keeps.

Three stores are versioned:
  reelarr.db         shows, activity log, kv, file-operation journal
  library_index.db   artists, venues, songs, learned corrections
  settings.json      user settings (versioned by a "_schema" key)

Rules this module enforces:
  * Steps are ordered and forward-only. A step never edits an earlier one;
    fixing a mistake means adding a new step.
  * Before any step touches an existing database, a full copy goes to
    <config>/backups/ (the newest few per store are kept).
  * Each step runs in its own transaction. If it fails, the database is left
    at the last good version and startup stops with the backup's location.
  * If a store was written by a NEWER Reelarr than this one, we refuse to
    start rather than guess — downgrading silently is how data gets lost.

A Barbosa install migrates in place: its databases are COPIED to the new
names (barbosa.db is never modified), and treated as schema version 0.
"""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

from . import paths
from .version import __version__

KEEP_BACKUPS = 10


class MigrationError(RuntimeError):
    """Startup must stop: the message is written for the person running it."""


# ── Step definitions ─────────────────────────────────────────────────────────
# Each step: (version, description, function(conn)). Versions are 1, 2, 3 …
# with no gaps. Version 0 means "a Barbosa-era database with no version table".

def _main_v1_baseline(c):
    # The schema Barbosa shipped with. IF NOT EXISTS makes this a no-op on a
    # Barbosa database and a fresh create on a new install.
    for stmt in (
        """CREATE TABLE IF NOT EXISTS shows (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_name  TEXT NOT NULL,
            current_path TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            confidence   INTEGER DEFAULT 0,
            meta         TEXT DEFAULT '{}',
            provenance   TEXT DEFAULT '{}',
            missing      TEXT DEFAULT '[]',
            notes        TEXT DEFAULT '',
            source_hint  TEXT DEFAULT '',
            filed_path   TEXT DEFAULT '',
            error        TEXT DEFAULT '',
            created_at   REAL,
            updated_at   REAL)""",
        """CREATE TABLE IF NOT EXISTS log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL, level TEXT, event TEXT, detail TEXT, show_id INTEGER)""",
        "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_shows_status ON shows(status)",
        "CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts)",
    ):
        c.execute(stmt)


def _main_v2_file_journal(c):
    # Every change Reelarr makes on disk is recorded here, grouped into
    # batches (one batch ≈ one user-visible action, e.g. "file this show").
    # Undo walks a batch's ops in reverse.
    c.execute("""CREATE TABLE batches (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL NOT NULL,
        label      TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT '',
        show_id    INTEGER,
        undo_state TEXT DEFAULT '{}',   -- what the show row looked like before
        undone_at  REAL,
        undo_note  TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE file_ops (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id  INTEGER NOT NULL REFERENCES batches(id),
        seq       INTEGER NOT NULL,
        ts        REAL NOT NULL,
        op        TEXT NOT NULL,        -- move | trash | retag | mkdir
        src       TEXT DEFAULT '',
        dst       TEXT DEFAULT '',
        detail    TEXT DEFAULT '{}')""")
    c.execute("CREATE INDEX idx_ops_batch ON file_ops(batch_id, seq)")
    c.execute("CREATE INDEX idx_batches_ts ON batches(ts)")


def _main_v3_plans(c):
    # Dry-run: what Reelarr WOULD do with a show, stored instead of doing it.
    c.execute("ALTER TABLE shows ADD COLUMN plan TEXT DEFAULT ''")


def _main_v4_auth(c):
    # One admin login (more later, if ever). Passwords are scrypt hashes;
    # session tokens are stored only as SHA-256 digests, so a copied database
    # can't be used to hijack a live session.
    c.execute("""CREATE TABLE users (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash    TEXT NOT NULL,
        created_at REAL NOT NULL)""")
    c.execute("""CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_seen  REAL,
        ip         TEXT DEFAULT '',
        agent      TEXT DEFAULT '')""")


def _main_v5_monitoring(c):
    # Artists you follow, and every release an indexer has shown us for them
    # (so each one is judged once, and "wanted" survives restarts).
    c.execute("""CREATE TABLE monitored_artists (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        norm             TEXT NOT NULL UNIQUE,
        monitored        INTEGER NOT NULL DEFAULT 1,
        indexer          TEXT NOT NULL DEFAULT 'lma',
        collection       TEXT DEFAULT '',     -- e.g. GratefulDead: exact, beats name matching
        sources          TEXT DEFAULT '',     -- comma list of acceptable source types; '' = any
        require_lossless INTEGER NOT NULL DEFAULT 1,
        upgrades         INTEGER NOT NULL DEFAULT 0,   -- want better sources of dates you own
        action           TEXT NOT NULL DEFAULT 'wanted',  -- wanted (ask me) | grab (automatic)
        last_checked     TEXT DEFAULT '',     -- newest indexer upload date already seen
        checked_at       REAL,
        added_at         REAL NOT NULL)""")
    c.execute("""CREATE TABLE releases (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        indexer     TEXT NOT NULL,
        release_id  TEXT NOT NULL,
        artist_id   INTEGER REFERENCES monitored_artists(id),
        data        TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL,           -- wanted | grabbed | skipped | ignored | failed
        reason      TEXT DEFAULT '',
        first_seen  REAL NOT NULL,
        updated_at  REAL NOT NULL,
        UNIQUE (indexer, release_id))""")
    c.execute("CREATE INDEX idx_releases_status ON releases(status, updated_at)")


def _main_v6_artist_notes(c):
    # The outcome of each artist's last check, in words, so "why didn't it
    # grab anything?" is answered on the Artists page.
    c.execute("ALTER TABLE monitored_artists ADD COLUMN last_note TEXT DEFAULT ''")


MAIN_STEPS = [
    (1, "baseline (Barbosa schema)", _main_v1_baseline),
    (2, "file-operation journal for undo", _main_v2_file_journal),
    (3, "dry-run plans on shows", _main_v3_plans),
    (4, "users and sessions", _main_v4_auth),
    (5, "monitored artists and releases", _main_v5_monitoring),
    (6, "last check result per artist", _main_v6_artist_notes),
]


def _index_v1_baseline(c):
    for stmt in (
        "CREATE TABLE IF NOT EXISTS artists (norm TEXT PRIMARY KEY, name TEXT)",
        "CREATE TABLE IF NOT EXISTS host_map (artist_norm TEXT PRIMARY KEY, host_name TEXT)",
        """CREATE TABLE IF NOT EXISTS venues (
            norm TEXT PRIMARY KEY, venue TEXT, city TEXT, state TEXT,
            seen INTEGER DEFAULT 1)""",
        "CREATE TABLE IF NOT EXISTS idx_meta (k TEXT PRIMARY KEY, v TEXT)",
        """CREATE TABLE IF NOT EXISTS corrections (
            kind TEXT, wrong_norm TEXT, right TEXT,
            PRIMARY KEY (kind, wrong_norm))""",
        """CREATE TABLE IF NOT EXISTS song_titles (
            artist_norm TEXT, title_norm TEXT, title TEXT,
            confirmed INTEGER DEFAULT 0, seen INTEGER DEFAULT 1,
            PRIMARY KEY (artist_norm, title_norm))""",
    ):
        c.execute(stmt)


INDEX_STEPS = [
    (1, "baseline (Barbosa schema)", _index_v1_baseline),
]


def _settings_v1_retired(s: dict, legacy: bool):
    # Retired before 0.1.0 (it corrected a setting that no longer exists).
    # Kept as a no-op so settings version numbers stay in sequence.
    pass


def _settings_v2_safety(s: dict, legacy: bool):
    # Dry-run defaults ON for a brand-new install. An existing Barbosa install
    # was already filing for real, so it keeps doing so — switching it to
    # plan-only overnight would look like the app had silently stopped.
    if legacy:
        s.setdefault("safety", {})["dry_run"] = False


def _settings_v3_conversion(s: dict, legacy: bool):
    # Barbosa converted every WAV/SHN to 16-bit/44.1 FLAC. Reelarr preserves
    # the source's bit depth and rate. An existing install keeps the old
    # behaviour, because its library was built that way.
    if legacy:
        s.setdefault("watcher", {})["convert_target"] = "cd"


SETTINGS_STEPS = [
    (1, "retired step", _settings_v1_retired),
    (2, "dry-run default", _settings_v2_safety),
    (3, "conversion target", _settings_v3_conversion),
]

LATEST = {"reelarr.db": MAIN_STEPS[-1][0],
          "library_index.db": INDEX_STEPS[-1][0],
          "settings.json": SETTINGS_STEPS[-1][0]}


# ── Machinery ────────────────────────────────────────────────────────────────

def _connect(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=30, isolation_level=None)  # explicit BEGIN
    c.row_factory = sqlite3.Row
    return c


def _has_table(c, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (name,)).fetchone() is not None


def _user_tables(c) -> int:
    return c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                     "AND name NOT LIKE 'sqlite_%'").fetchone()[0]


def db_version(c) -> int:
    if not _has_table(c, "schema_version"):
        return 0
    r = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    return int(r or 0)


def _prune_backups(stem: str):
    files = sorted(paths.backups_dir().glob(f"{stem}-v*"), key=lambda p: p.stat().st_mtime)
    for old in files[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


def backup_db(path: Path, from_version: int) -> Path:
    """Consistent copy via SQLite's backup API (safe with WAL and live writers)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = paths.backups_dir() / f"{path.stem}-v{from_version}-{stamp}{path.suffix}"
    src = sqlite3.connect(path)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    _prune_backups(path.stem)
    return dest


def migrate_db(path: Path, steps: list, label: str) -> dict:
    """Bring one SQLite file up to the newest step. Returns a short report."""
    latest = steps[-1][0]
    existed = path.exists() and path.stat().st_size > 0
    path.parent.mkdir(parents=True, exist_ok=True)
    c = _connect(path)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        current = db_version(c)
        if current > latest:
            raise MigrationError(
                f"{label} ({path}) was written by a newer version of Reelarr "
                f"(schema v{current}; this build, {__version__}, understands up "
                f"to v{latest}). Upgrade Reelarr, or restore an older copy from "
                f"{paths.backups_dir()}.")
        pending = [s for s in steps if s[0] > current]
        if not pending:
            return {"store": label, "from": current, "to": current, "backup": None}

        backup = None
        if existed and _user_tables(c):
            backup = backup_db(path, current)

        c.execute("""CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY, name TEXT, applied_at REAL, app_version TEXT)""")
        for version, name, fn in pending:
            try:
                c.execute("BEGIN")
                fn(c)
                c.execute("INSERT INTO schema_version VALUES (?,?,?,?)",
                          (version, name, time.time(), __version__))
                c.execute("COMMIT")
            except Exception as e:
                c.execute("ROLLBACK")
                where = f" A copy from before the upgrade is at {backup}." if backup else ""
                raise MigrationError(
                    f"{label}: upgrade step v{version} ({name}) failed: {e}. "
                    f"The database was left at v{db_version(c)}.{where}") from e
        return {"store": label, "from": current, "to": latest,
                "backup": str(backup) if backup else None}
    finally:
        c.close()


def write_settings(path: Path, data: dict):
    """Atomic, owner-only write: a crash mid-write can never leave a
    half-written settings file, and other local users can't read secrets."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def migrate_settings(path: Path, from_barbosa: bool = False) -> dict:
    """from_barbosa: this settings file belonged to a Barbosa install (decides
    the defaults existing users keep). A settings.json without a version that
    did NOT come from Barbosa — hand-written, or from a deployment template —
    is treated as a new install."""
    latest = SETTINGS_STEPS[-1][0]
    if not path.exists():
        # fresh install: nothing to migrate; defaults apply. Stamp the version
        # the first time settings are saved (config.save does this).
        return {"store": "settings.json", "from": None, "to": latest, "backup": None}
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError) as e:
        raise MigrationError(
            f"settings.json ({path}) can't be read ({e}). Fix or remove it — "
            f"removing it resets every setting to its default.") from None
    current = int(data.get("_schema", 0))
    if current > latest:
        raise MigrationError(
            f"settings.json ({path}) was written by a newer version of Reelarr "
            f"(v{current}; this build understands up to v{latest}). Upgrade "
            f"Reelarr, or restore an older copy from {paths.backups_dir()}.")
    if current == latest:
        _tighten_mode(path)
        return {"store": "settings.json", "from": current, "to": current, "backup": None}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = paths.backups_dir() / f"settings-v{current}-{stamp}.json"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    _prune_backups("settings")
    legacy = current == 0 and from_barbosa
    for version, _name, fn in SETTINGS_STEPS:
        if version > current:
            fn(data, legacy)
    data["_schema"] = latest
    write_settings(path, data)
    return {"store": "settings.json", "from": current, "to": latest, "backup": str(backup)}


def _tighten_mode(path: Path):
    try:
        if os.name != "nt" and (path.stat().st_mode & 0o077):
            os.chmod(path, 0o600)
    except OSError:
        pass


def _copy_sqlite(src: Path, dest: Path):
    s = sqlite3.connect(src)
    try:
        d = sqlite3.connect(dest)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def carry_over_barbosa(cfg_dir: Path) -> list:
    """First start of Reelarr beside (or instead of) Barbosa: copy its state
    under the new names. The Barbosa files themselves are left untouched, so
    Barbosa can keep running from them until you're happy with Reelarr."""
    notes = []
    main = cfg_dir / "reelarr.db"
    if main.exists():
        return notes
    candidates = [cfg_dir]
    legacy_dir = paths.legacy_user_data_dir()
    if legacy_dir != cfg_dir:
        candidates.append(legacy_dir)
    for src_dir in candidates:
        old = src_dir / "barbosa.db"
        try:
            if not old.is_file():
                continue
        except OSError:         # a folder we can't read isn't one we migrate from
            continue
        _copy_sqlite(old, main)
        notes.append(f"copied {old} → {main}")
        if src_dir != cfg_dir:
            idx = src_dir / "library_index.db"
            if idx.is_file() and not (cfg_dir / "library_index.db").exists():
                _copy_sqlite(idx, cfg_dir / "library_index.db")
                notes.append(f"copied {idx}")
            st = src_dir / "settings.json"
            if st.is_file() and not (cfg_dir / "settings.json").exists():
                shutil.copy2(st, cfg_dir / "settings.json")
                notes.append(f"copied {st}")
        break
    return notes


def run_all() -> list:
    """Called once at startup, before anything else opens a database."""
    cfg_dir = paths.config_dir()
    notes = carry_over_barbosa(cfg_dir)
    from_barbosa = bool(notes) or (cfg_dir / "barbosa.db").exists()
    reports = [
        migrate_settings(cfg_dir / "settings.json", from_barbosa),
        migrate_db(cfg_dir / "reelarr.db", MAIN_STEPS, "reelarr.db"),
        migrate_db(cfg_dir / "library_index.db", INDEX_STEPS, "library_index.db"),
    ]
    if notes:
        reports.insert(0, {"store": "barbosa", "notes": notes})
    return reports


def status() -> dict:
    """Current schema versions, for the System page."""
    cfg_dir = paths.config_dir()
    out = {}
    for name in ("reelarr.db", "library_index.db"):
        p = cfg_dir / name
        if p.exists():
            c = _connect(p)
            try:
                out[name] = db_version(c)
            finally:
                c.close()
    sp = cfg_dir / "settings.json"
    try:
        out["settings.json"] = int(json.loads(sp.read_text()).get("_schema", 0)) if sp.exists() else None
    except (OSError, ValueError):
        out["settings.json"] = None
    out["latest"] = LATEST
    return out

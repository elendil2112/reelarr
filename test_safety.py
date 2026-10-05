"""Phase 1a safety tests: migrations, dry-run, trash, undo, conversion.

Run:  pip install pytest mutagen fastapi && python -m pytest tests -q
Needs ffmpeg/ffprobe on PATH (they're in the Docker image).
"""
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


# ── harness ──────────────────────────────────────────────────────────────────

def fresh_app(tmp: Path, settings: dict = None):
    """Import the app against a brand-new config/watch/library trio."""
    for d in ("config", "watch", "music"):
        (tmp / d).mkdir(exist_ok=True)
    os.environ["REELARR_CONFIG"] = str(tmp / "config")
    os.environ["REELARR_WATCH"] = str(tmp / "watch")
    os.environ["REELARR_LIBRARY"] = str(tmp / "music")
    if settings is not None:
        (tmp / "config" / "settings.json").write_text(json.dumps(settings))
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    mods = {n: importlib.import_module(f"app.{n}") for n in
            ("database", "config", "fileops", "migrations", "pipeline")}
    return mods


def offline(settings: dict = None) -> dict:
    s = {"sources": {"use_internet_archive": False, "use_setlistfm": False,
                     "use_musicbrainz_genre": False},
         "watcher": {"audio_source_analysis": False},
         "filing": {"auto_file_threshold": 50, "hold_uncertain_titles": False}}
    for k, v in (settings or {}).items():
        s.setdefault(k, {}).update(v) if isinstance(v, dict) else s.__setitem__(k, v)
    return s


def make_audio(path: Path, seconds=3, rate=44100, fmt="s16", freq=440):
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = {".flac": ["-c:a", "flac"], ".mp3": ["-c:a", "libmp3lame", "-q:a", "6"],
             ".wav": ["-c:a", {"s16": "pcm_s16le", "s24": "pcm_s24le",
                               "flt": "pcm_f32le"}[fmt]]}[path.suffix]
    extra = ["-sample_fmt", "s32"] if path.suffix == ".flac" and fmt == "s24" else []
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
                    *codec, *extra, str(path)], check=True)


def make_show(watch: Path, name="gd1977-05-08.sbd.miller") -> Path:
    from mutagen.flac import FLAC
    show = watch / name
    for i, title in enumerate(["New Minglewood Blues", "Loser", "El Paso"], 1):
        f = show / f"gd77-05-08d1t{i:02d}.flac"
        make_audio(f, freq=300 + 50 * i)
        a = FLAC(f)
        a["ARTIST"] = "Grateful Dead"
        a["ALBUM"] = "1977-05-08 Barton Hall, Cornell University, Ithaca, NY [SBD]"
        a["TITLE"] = title
        a.save()
    (show / "gd77-05-08.txt").write_text(
        "Grateful Dead\n1977-05-08\nBarton Hall, Cornell University\nIthaca, NY\n"
        "Source: SBD > Reel > DAT\n\n1. New Minglewood Blues\n2. Loser\n3. El Paso\n")
    (show / "gd77-05-08.torrent").write_bytes(b"d4:infoe")
    (show / "gd77-05-08d1t01.flac.png").write_bytes(b"\x89PNG")
    return show


def tree(p: Path) -> dict:
    """path → sha1 for every file under p (the 'did anything change' oracle)."""
    return {str(f.relative_to(p)): hashlib.sha1(f.read_bytes()).hexdigest()
            for f in sorted(p.rglob("*")) if f.is_file()}


# ── migrations ───────────────────────────────────────────────────────────────

BARBOSA_SCHEMA = """
CREATE TABLE shows (id INTEGER PRIMARY KEY AUTOINCREMENT, folder_name TEXT NOT NULL,
  current_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  confidence INTEGER DEFAULT 0, meta TEXT DEFAULT '{}', provenance TEXT DEFAULT '{}',
  missing TEXT DEFAULT '[]', notes TEXT DEFAULT '', source_hint TEXT DEFAULT '',
  filed_path TEXT DEFAULT '', error TEXT DEFAULT '', created_at REAL, updated_at REAL);
CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT,
  event TEXT, detail TEXT, show_id INTEGER);
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO shows (folder_name, current_path, status, filed_path)
  VALUES ('gd1977-05-08', '/music/Grateful Dead/gd1977-05-08', 'filed',
          '/music/Grateful Dead/gd1977-05-08');
"""


def test_barbosa_install_migrates_without_touching_originals(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    old = sqlite3.connect(cfg / "barbosa.db")
    old.executescript(BARBOSA_SCHEMA)
    old.commit(); old.close()
    idx = sqlite3.connect(cfg / "library_index.db")
    idx.executescript("CREATE TABLE corrections (kind TEXT, wrong_norm TEXT, right TEXT,"
                      " PRIMARY KEY (kind, wrong_norm));"
                      "INSERT INTO corrections VALUES ('artist','gdead','Grateful Dead');")
    idx.commit(); idx.close()
    (cfg / "settings.json").write_text(json.dumps({"watcher": {"poll_minutes": 7}}))
    before = hashlib.sha1((cfg / "barbosa.db").read_bytes()).hexdigest()

    m = fresh_app(tmp_path)
    m["database"].init()

    assert hashlib.sha1((cfg / "barbosa.db").read_bytes()).hexdigest() == before
    st = m["migrations"].status()
    assert st["reelarr.db"] == st["latest"]["reelarr.db"] >= 4
    assert st["library_index.db"] == 1 and st["settings.json"] == 3
    rows = m["database"].list_shows()
    assert rows and rows[0]["folder_name"] == "gd1977-05-08" and rows[0]["plan"] == ""
    s = json.loads((cfg / "settings.json").read_text())
    assert s["watcher"]["poll_minutes"] == 7        # their own settings survive
    assert s["safety"]["dry_run"] is False          # existing install stays live
    assert s["watcher"]["convert_target"] == "cd"    # and keeps its old conversion
    assert (cfg / "settings.json").stat().st_mode & 0o777 == 0o600
    backups = sorted(p.name for p in (cfg / "backups").iterdir())
    assert any(b.startswith("reelarr-v0-") for b in backups)
    assert any(b.startswith("library_index-v0-") for b in backups)
    assert any(b.startswith("settings-v0-") for b in backups)
    # learned corrections survived
    c = sqlite3.connect(cfg / "library_index.db")
    assert c.execute("SELECT right FROM corrections").fetchone()[0] == "Grateful Dead"


def test_fresh_install_defaults_to_dry_run(tmp_path):
    m = fresh_app(tmp_path)
    m["database"].init()
    assert m["fileops"].dry_run_enabled() is True
    assert m["config"].load()["watcher"]["convert_target"] == "preserve"
    bk = tmp_path / "config" / "backups"
    assert not bk.exists() or not list(bk.iterdir())   # nothing to back up


def test_refuses_database_from_newer_version(tmp_path):
    m = fresh_app(tmp_path)
    m["database"].init()
    c = sqlite3.connect(tmp_path / "config" / "reelarr.db")
    c.execute("INSERT INTO schema_version VALUES (99, 'future', 0, '9.9')")
    c.commit(); c.close()
    m = fresh_app(tmp_path)
    with pytest.raises(m["migrations"].MigrationError, match="newer version"):
        m["database"].init()


def test_failed_step_rolls_back_and_names_backup(tmp_path, monkeypatch):
    m = fresh_app(tmp_path)
    mig = m["migrations"]
    db_path = tmp_path / "config" / "reelarr.db"
    mig.migrate_db(db_path, mig.MAIN_STEPS[:2], "reelarr.db")   # stop at v2

    def boom(c):
        c.execute("ALTER TABLE shows ADD COLUMN half_done TEXT")
        raise RuntimeError("simulated failure")
    steps = mig.MAIN_STEPS[:2] + [(3, "boom", boom)]
    with pytest.raises(mig.MigrationError, match="backups"):
        mig.migrate_db(db_path, steps, "reelarr.db")
    c = sqlite3.connect(db_path)
    assert mig.db_version(c) == 2
    cols = [r[1] for r in c.execute("PRAGMA table_info(shows)")]
    assert "half_done" not in cols                      # rolled back cleanly


# ── dry-run ──────────────────────────────────────────────────────────────────

def test_dry_run_changes_nothing_and_records_a_plan(tmp_path):
    m = fresh_app(tmp_path, offline())
    m["database"].init()
    show = make_show(tmp_path / "watch")
    before = tree(tmp_path)
    r = m["pipeline"].process_show(show)
    assert r["status"] == "planned", r
    assert tree(tmp_path / "watch") == {k[len("watch/"):]: v for k, v in before.items()
                                        if k.startswith("watch/")}
    assert not any((tmp_path / "music").iterdir())
    row = m["database"].get_show_by_path(str(show))
    plan = json.loads(row["plan"])
    ops = {o["op"] for o in plan["ops"]}
    assert {"trash", "retag", "move", "tags"} <= ops
    assert "Grateful Dead" in plan["dest"]
    # nothing journalled: a plan isn't an action
    assert m["fileops"].list_batches() == []


# ── live intake, trash, undo ─────────────────────────────────────────────────

def test_live_intake_trashes_junk_and_undo_restores_everything(tmp_path):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}}))
    m["database"].init()
    show = make_show(tmp_path / "watch")
    original = tree(show)
    r = m["pipeline"].process_show(show)
    assert r["status"] == "filed", r
    dest = Path(r["dest"])
    assert dest.is_dir() and not show.exists()
    names = {p.name for p in dest.rglob("*")}
    assert "gd77-05-08.torrent" not in names
    trash = tmp_path / "watch" / ".reelarr-trash"
    trashed = {p.name for p in trash.rglob("*") if p.is_file()}
    assert {"gd77-05-08.torrent", "gd77-05-08d1t01.flac.png"} <= trashed
    assert (trash / ".plexignore").exists()

    batch = m["fileops"].list_batches()[0]
    assert batch["kind"] == "intake" and batch["n_ops"] > 0
    u = m["fileops"].undo(batch["id"])
    assert u["conflicts"] == [], u
    assert tree(show) == original           # same files, same bytes — tags included
    assert not dest.exists()
    row = m["database"].get_show_by_path(str(show))
    assert row["status"] == "review"           # held, not re-filed by the next sweep
    assert m["fileops"].undo(batch["id"]).get("error") == "already undone"


def test_replace_trashes_old_library_copy_instead_of_deleting(tmp_path):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}}))
    m["database"].init()
    first = m["pipeline"].process_show(make_show(tmp_path / "watch"))
    assert first["status"] == "filed"
    old_copy = Path(first["dest"])
    old_tree = tree(old_copy)

    incoming = make_show(tmp_path / "watch", "gd1977-05-08.sbd.better")
    sid = m["database"].upsert_show(str(incoming), status="review",
                                    meta=json.loads(m["database"].get_show_by_filed(
                                        str(old_copy))["meta"]))
    r = m["pipeline"].replace_show(sid, {})
    assert r.get("status") == "filed", r
    trash = tmp_path / "music" / ".reelarr-trash"
    kept = [p for p in trash.rglob("*") if p.is_dir() and tree(p) == old_tree]
    assert kept, "old copy should be in the library's trash, byte-for-byte"

    batch = m["fileops"].list_batches()[0]
    assert batch["kind"] == "replace"
    u = m["fileops"].undo(batch["id"])
    assert u["conflicts"] == [], u
    assert tree(old_copy) == old_tree
    old_row = m["database"].get_show_by_filed(str(old_copy))
    assert old_row and old_row["status"] == "filed"


# ── format dedupe and conversion ─────────────────────────────────────────────

def test_dedupe_respects_discs_and_trashes_rather_than_deletes(tmp_path):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}}))
    m["database"].init()
    show = tmp_path / "watch" / "show"
    make_audio(show / "Disc1" / "t01.flac", freq=300)
    make_audio(show / "Disc2" / "t01.mp3", seconds=5, freq=500)   # different song
    make_audio(show / "FLAC" / "t02.flac", freq=600)
    make_audio(show / "MP3" / "t02.mp3", freq=600)                # same song, worse
    b = m["fileops"].Batch("t", dry_run=False)
    n = m["pipeline"].dedupe_formats(show, lambda *a: None, b)
    assert n == 1
    assert (show / "Disc2" / "t01.mp3").exists()
    assert not (show / "MP3" / "t02.mp3").exists()
    assert any(p.name == "t02.mp3" for p in (tmp_path / "watch" / ".reelarr-trash").rglob("*"))


@pytest.mark.parametrize("target,fmt,rate,want_bits,want_rate", [
    ("preserve", "s24", 96000, 24, 96000),
    ("preserve", "s16", 44100, 16, 44100),
    ("preserve", "flt", 48000, 24, 48000),
    ("cd", "s24", 96000, 16, 44100),
])
def test_wav_conversion_target(tmp_path, target, fmt, rate, want_bits, want_rate):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False},
                                     "watcher": {"convert_target": target}}))
    m["database"].init()
    show = tmp_path / "watch" / "show"
    make_audio(show / "t01.wav", rate=rate, fmt=fmt)
    b = m["fileops"].Batch("t", dry_run=False)
    m["pipeline"].prep_folder(show, m["config"].load(), lambda *a: None, b)
    out = show / "t01.flac"
    assert out.exists() and not (show / "t01.wav").exists()
    p = m["pipeline"]._probe_audio(out)
    assert (p["bits"], p["rate"]) == (want_bits, want_rate)
    assert any(f.name == "t01.wav" for f in (tmp_path / "watch" / ".reelarr-trash").rglob("*"))

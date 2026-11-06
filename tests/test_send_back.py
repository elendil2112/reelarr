"""Maintenance → send back: the show is identified again, then waits in
Review instead of filing itself; approving it files it and drops the note."""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app, make_show, offline

pytestmark = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")


def _filed(tmp_path):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}}))
    m["database"].init()
    importlib.import_module("app.setup").mark_done()
    show = make_show(tmp_path / "watch")
    assert m["pipeline"].process_show(show)["status"] == "filed"
    row = m["database"].list_shows("filed")[0]
    return m, row


def test_sent_back_show_waits_in_review_then_files(tmp_path, monkeypatch):
    m, row = _filed(tmp_path)
    audit = importlib.import_module("app.audit")
    filed = Path(row["filed_path"])
    monkeypatch.setattr(audit, "find_suspects", lambda *a, **k: [
        {"path": str(filed), "artist_folder": filed.parent.name, "album": "",
         "flags": ["odd state: 'Nowhere'"]}])
    assert audit.return_suspects_to_watch()["moved"] == 1
    back = tmp_path / "watch" / filed.name
    note = json.loads((back / ".reelarr-review").read_text())
    assert note["flags"] == ["odd state: 'Nowhere'"] and note["was"] == str(filed)

    # a clean, confident show that would normally file itself
    r = m["pipeline"].process_show(back)
    assert r["status"] == "review"
    rec = m["database"].get_show(row["id"])
    assert rec["status"] == "review"
    assert "Sent back from Maintenance" in rec["notes"] and "odd state" in rec["notes"]

    # reprocessing keeps it in Review
    assert m["pipeline"].process_show(back)["status"] == "review"

    # approving files it, and the note doesn't follow it into the library
    m["pipeline"].approve_show(row["id"], {})
    rec = m["database"].get_show(row["id"])
    assert rec["status"] == "filed"
    dest = Path(rec["filed_path"])
    assert dest.is_dir() and not (dest / ".reelarr-review").exists()


def test_shows_without_the_note_still_file_themselves(tmp_path):
    m, row = _filed(tmp_path)
    assert row["status"] == "filed"
    assert m["pipeline"].review_marker(Path(row["filed_path"])) == {}

"""Notes: saved as you type, never overwritten by a second window, and a
deleted note can be brought back."""
import importlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_auth import client, setup_admin  # noqa: E402


def _c(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    return c


def test_create_save_list(tmp_path):
    c = _c(tmp_path)
    n = c.post("/api/notes", json={}).json()
    assert n["rev"] == 1 and n["title"] == "" and n["body"] == ""
    s = c.put(f"/api/notes/{n['id']}", json={"rev": 1, "title": "Trades",
                                             "body": "\n  send Mike the 5/8/77 Miller\nmore"}).json()
    assert s["rev"] == 2 and s["title"] == "Trades"
    lst = c.get("/api/notes").json()["notes"]
    assert lst[0]["id"] == n["id"] and lst[0]["preview"] == "send Mike the 5/8/77 Miller"
    assert "more" in lst[0]["body"]                       # whole text, for searching
    assert c.get(f"/api/notes/{n['id']}").json()["body"].endswith("more")


def test_a_second_window_cant_overwrite(tmp_path):
    c = _c(tmp_path)
    n = c.post("/api/notes", json={"title": "x"}).json()
    a = c.put(f"/api/notes/{n['id']}", json={"rev": 1, "body": "from tab A"})
    assert a.status_code == 200
    b = c.put(f"/api/notes/{n['id']}", json={"rev": 1, "body": "from tab B"})   # B still thinks rev 1
    assert b.status_code == 409 and b.json()["current"]["body"] == "from tab A"
    # "keep mine": retry against the revision it was shown
    k = c.put(f"/api/notes/{n['id']}", json={"rev": b.json()["current"]["rev"], "body": "from tab B"})
    assert k.status_code == 200 and k.json()["body"] == "from tab B"


def test_pinned_first(tmp_path):
    c = _c(tmp_path)
    a = c.post("/api/notes", json={"title": "old"}).json()
    time.sleep(0.01)
    c.post("/api/notes", json={"title": "new"})
    c.put(f"/api/notes/{a['id']}", json={"rev": a["rev"], "pinned": True})
    titles = [n["title"] for n in c.get("/api/notes").json()["notes"]]
    assert titles == ["old", "new"]


def test_delete_undo_and_purge(tmp_path):
    c = _c(tmp_path)
    n = c.post("/api/notes", json={"title": "gone"}).json()
    assert c.delete(f"/api/notes/{n['id']}").json()["ok"]
    assert c.get("/api/notes").json()["notes"] == []
    assert c.get(f"/api/notes/{n['id']}").status_code == 404
    assert c.put(f"/api/notes/{n['id']}", json={"rev": 1, "body": "x"}).status_code == 404
    assert c.post(f"/api/notes/{n['id']}/restore").json()["title"] == "gone"
    notes = importlib.import_module("app.notes")
    c.delete(f"/api/notes/{n['id']}")
    assert notes.purge(older_than_days=30) == 0          # recent: kept for Undo
    assert notes.purge(older_than_days=-1) == 1


def test_bad_input_is_refused(tmp_path):
    c = _c(tmp_path)
    n = c.post("/api/notes", json={}).json()
    assert c.put(f"/api/notes/{n['id']}", json={"rev": 1, "title": "x" * 201}).status_code == 400
    assert c.put(f"/api/notes/{n['id']}", json={"rev": 1, "body": 5}).status_code == 400
    assert c.post("/api/notes", json={"title": ["no"]}).status_code == 400


def test_notes_need_login(tmp_path):
    c = _c(tmp_path)
    c.post("/api/auth/logout")
    c.cookies.clear()
    assert c.get("/api/notes").status_code == 401

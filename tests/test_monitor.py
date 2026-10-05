"""Phase 3: indexers, monitored artists, the search loop, queue, system."""
import importlib
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402
from test_auth import client, setup_admin  # noqa: E402


def _fake_indexer():
    idx = importlib.import_module("app.indexers")
    base = importlib.import_module("app.indexers.base")

    class Fake(base.Indexer):
        name, label = "fake", "Fake Archive"
        grabbed = []

        def __init__(self):
            self.items = [
                base.Release(indexer="fake", id="bs2026-08-01.sbd.flac16", artist="Billy Strings",
                             date="2026-08-01", source_type="SBD", formats=["flac"], added="2026-09-20"),
                base.Release(indexer="fake", id="bs2026-08-02.aud.flac16", artist="Billy Strings",
                             date="2026-08-02", source_type="AUD", formats=["flac"], added="2026-09-21"),
                base.Release(indexer="fake", id="bs2026-08-03.sbd.mp3", artist="Billy Strings",
                             date="2026-08-03", source_type="SBD", formats=["mp3"], added="2026-09-22"),
                base.Release(indexer="fake", id="bs2026-08-04.sbd.flac", artist="Billy Strings",
                             date="2026-08-04", source_type="SBD", formats=["flac"], added="2026-09-23"),
                base.Release(indexer="fake", id="bs2026-08-05.mtx.restricted", artist="Billy Strings",
                             date="2026-08-05", source_type="MTX", formats=["flac"], added="2026-09-24"),
                base.Release(indexer="fake", id="bs2026-08-06.unknown", artist="Billy Strings",
                             date="2026-08-06", source_type="", formats=["flac"], added="2026-09-25"),
            ]

        def search(self, query, page=1, rows=25, sort=""):
            return {"total": len(self.items), "page": 1, "results": list(self.items), "kind": "query"}

        def new_for_artist(self, artist, since="", rows=50, collection=""):
            return [r for r in self.items if not since or r.added[:10] >= since[:10]]

        def check_downloadable(self, release):
            return "stream-only" if "restricted" in release.id else ""

        def grab(self, ids):
            Fake.grabbed += ids
            return {"grabbed": len(ids), "skipped": 0, "failed": 0}

    f = Fake()
    idx.register(f)
    return f


def _own(date, artist="Billy Strings", source="AUD"):
    db = importlib.import_module("app.database")
    db.upsert_show(f"/music/{artist}/{date}", status="filed", filed_path=f"/music/{artist}/{date}",
                   meta={"artist": artist, "album_artist": artist, "date": date, "source_type": source})


def test_lma_parsing_helpers(tmp_path):
    fresh_app(tmp_path)
    lma = importlib.import_module("app.lma")
    il = importlib.import_module("app.indexers.lma")
    assert lma._formats(["Flac", "24bit Flac", "VBR MP3", "Shorten", "Text"]) == ["flac", "flac24", "mp3", "shn"]
    assert lma._formats("Flac") == ["flac"]
    assert il._source_type({"identifier": "gd1977-05-08.sbd.hicks.4982.sbeok.shnf"}) == "SBD"
    assert il._source_type({"identifier": "bs2023-04-21.mk4.aud.flac16"}) == "AUD"
    assert il._source_type({"identifier": "phish1997-11-22.mtx.flac"}) == "MTX"
    assert il._source_type({"identifier": "x", "source": "Schoeps MK4 > Sound Devices"}) == "AUD"
    r = il._release({"identifier": "bs.sbd", "formats": ["flac24"], "added": "2026-09-01T00:00:00"})
    assert r.lossless and r.hires and r.url.endswith("/details/bs.sbd")


def test_monitor_judges_each_release_once(tmp_path):
    fresh_app(tmp_path, {"safety": {"dry_run": True}})
    importlib.import_module("app.database").init()
    mon = importlib.import_module("app.monitor")
    _fake_indexer()                                      # registers "fake"
    _own("2026-08-04")                                   # an AUD copy already in the library
    a = mon.save_artist("Billy Strings", indexer="fake", sources=["SBD", "MTX"])
    mon._c().execute("UPDATE monitored_artists SET last_checked='2026-09-01' WHERE id=?", (a["id"],))
    out = mon.check_artist(mon.get_artist(a["id"]))
    assert out["new"] == 6
    by_id = {r["release_id"]: r for s in ("wanted", "skipped") for r in mon.list_releases(s)}
    assert by_id["bs2026-08-01.sbd.flac16"]["status"] == "wanted"
    assert "isn't one of your sources" in by_id["bs2026-08-02.aud.flac16"]["reason"]
    assert "lossy" in by_id["bs2026-08-03.sbd.mp3"]["reason"]
    assert "already in your library" in by_id["bs2026-08-04.sbd.flac"]["reason"]
    assert by_id["bs2026-08-05.mtx.restricted"]["reason"] == "stream-only"
    assert by_id["bs2026-08-06.unknown"]["status"] == "wanted"           # unknown source: let a human look
    assert mon.get_artist(a["id"])["last_checked"] == "2026-09-25"
    assert mon.check_artist(mon.get_artist(a["id"]))["new"] == 0          # remembered

    # with upgrades on, an SBD beats the AUD you own
    mon._c().execute("DELETE FROM releases")
    mon._c().execute("UPDATE monitored_artists SET last_checked='', upgrades=1 WHERE id=?", (a["id"],))
    mon.check_artist(mon.get_artist(a["id"]))
    up = [r for r in mon.list_releases("wanted") if r["release_id"] == "bs2026-08-04.sbd.flac"]
    assert up and "upgrade" in up[0]["reason"]


def test_automatic_grabs_wait_for_go_live_but_clicks_dont(tmp_path):
    fresh_app(tmp_path, {"safety": {"dry_run": True}})
    importlib.import_module("app.database").init()
    mon = importlib.import_module("app.monitor")
    fake = _fake_indexer()
    fake.grabbed.clear()
    a = mon.save_artist("Billy Strings", indexer="fake", action="grab", sources=["SBD"])
    mon._c().execute("UPDATE monitored_artists SET last_checked='' WHERE id=?", (a["id"],))
    out = mon.check_artist(mon.get_artist(a["id"]))
    assert out["grabbed"] == 0 and fake.grabbed == []
    wanted = mon.list_releases("wanted")
    assert wanted and all("dry run" in w["reason"] for w in wanted)
    r = mon.grab_release(wanted[0]["id"])                 # you clicked it
    assert r == {"grabbed": 1} and fake.grabbed == [wanted[0]["release_id"]]
    assert mon.list_releases("grabbed")[0]["release_id"] == wanted[0]["release_id"]
    assert mon.ignore_release(wanted[1]["id"]) and mon.list_releases("ignored")


def test_new_artist_only_looks_back_a_little(tmp_path):
    fresh_app(tmp_path, {"monitor": {"lookback_days": 10}})
    importlib.import_module("app.database").init()
    mon = importlib.import_module("app.monitor")
    a = mon.save_artist("Grateful Dead")
    from datetime import datetime, timedelta, timezone
    assert a["last_checked"] == (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    again = mon.save_artist("grateful dead", upgrades=True)          # same artist, not a duplicate
    assert again["id"] == a["id"] and again["upgrades"] == 1 and len(mon.list_artists()) == 1


def test_api_artists_queue_discover_system(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    _fake_indexer()
    r = c.post("/api/monitor/artists", json={"name": "Goose", "indexer": "fake", "sources": ["SBD"]})
    assert r.status_code == 200 and r.json()["sources"] == ["SBD"]
    assert c.post("/api/monitor/artists", json={"name": "X", "action": "sometimes"}).status_code == 400
    listing = c.get("/api/monitor/artists").json()
    assert [a["name"] for a in listing["artists"]] == ["Goose"]
    assert {"name": "fake", "label": "Fake Archive"} in listing["indexers"]

    s = c.post("/api/discover/search", json={"q": "billy", "indexer": "fake"}).json()
    assert len(s["results"]) == 6 and s["results"][0]["lossless"] is True

    q = c.get("/api/queue").json()
    assert set(q) >= {"wanted", "downloading", "intake", "grabbed"}

    h = c.get("/api/system/health").json()
    names = [x["name"] for x in h["checks"]]
    assert {"Database", "Setup", "Library", "Login", "Backup"} <= set(names)
    b = c.post("/api/system/backup").json()
    dl = c.get(f"/api/system/backup/{b['name']}")
    assert dl.status_code == 200
    zpath = tmp_path / "b.zip"
    zpath.write_bytes(dl.content)
    with zipfile.ZipFile(zpath) as z:
        assert {"reelarr.db", "library_index.db", "BACKUP.txt"} <= set(z.namelist())
    assert c.get("/api/system/backup/..%2Fsettings.json").status_code == 404
    assert c.get("/api/status").json()["wanted"] == 0


def test_no_pirate_language_left_in_the_ui():
    import re
    root = Path(__file__).resolve().parent.parent / "app" / "static"
    words = re.compile(r"\b(brig|stow(ed)?|plunder|manifest|the articles|reckoning|ship's log|"
                       r"captain|cap'n|scuttled|aground|quartermaster|overboard|the hold)\b", re.I)
    for f in ("index.html", "app.js", "setup.html", "setup.js", "login.html"):
        text = (root / f).read_text()
        hits = [m.group(0) for m in words.finditer(text)
                if m.group(0).lower() != "brig"]          # "brig" survives only as an API value
        assert not hits, (f, hits[:5])

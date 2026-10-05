"""The Live Music Archive monitor, against responses shaped like archive.org's
real ones (checked against the live API on 2026-10-02 for Goose)."""
import importlib
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402
from test_auth import client, setup_admin  # noqa: E402

GOOSE = [  # trimmed from archive.org's real answer
    {"identifier": "goose2026-04-23.InsideOut_DPA.2012_V3.digital.remix", "creator": "Goose",
     "date": "2026-04-23T00:00:00Z", "addeddate": "2026-09-22T07:54:32Z",
     "source": "DPA 2012 (DIN) > Lunatec V3 (AES out) > SD 702t (AES in) 24/48",
     "format": ["24bit Flac", "Archive BitTorrent", "Columbia Peaks", "Item Tile", "VBR MP3"]},
    {"identifier": "goose2026-08-14", "creator": "Goose", "date": "2026-08-14",
     "addeddate": "2026-09-10T19:31:41Z", "source": "Aud > Hollyland Lark A1 > iPhone",
     "format": ["Archive BitTorrent", "Flac", "VBR MP3"]},
    {"identifier": "goose2026-09-02", "creator": "Goose", "date": "2026-09-02",
     "addeddate": "2026-09-03T08:12:57Z", "source": "Schoeps CCM4V'S>Sound Devices Mix-Pre 6 II (48/32)",
     "format": ["24bit Flac", "Archive BitTorrent", "Flac FingerPrint", "VBR MP3"]},
]
META_OK = {"files": [{"name": "01.flac", "format": "24bit Flac"},
                     {"name": "x_archive.torrent", "format": "Archive BitTorrent"}]}


def _fake_archive(monkeypatch, lma, docs, total=None, by_name=None):
    """Serve advancedsearch pages and metadata like archive.org does."""
    calls = []

    def get_json(url, timeout=30):
        calls.append(url)
        if "/metadata/" in url:
            return META_OK
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        q, rows, page = qs["q"][0], int(qs["rows"][0]), int(qs["page"][0])
        pool = docs
        if by_name is not None:
            pool = by_name.get(q, [])
        start = (page - 1) * rows
        return {"response": {"numFound": total if total is not None else len(pool),
                             "docs": pool[start:start + rows]}}
    monkeypatch.setattr(lma, "_get_json", get_json)
    monkeypatch.setattr(lma, "DELAY", 0)
    return calls


def _setup(tmp_path, **cfg):
    fresh_app(tmp_path, {"safety": {"dry_run": False}, **cfg})
    importlib.import_module("app.database").init()
    return (importlib.import_module("app.monitor"), importlib.import_module("app.lma"),
            importlib.import_module("app.database"))


def test_real_shaped_results_become_wanted(tmp_path, monkeypatch):
    mon, lma, db = _setup(tmp_path)
    _fake_archive(monkeypatch, lma, GOOSE)
    a = mon.save_artist("Goose", lookback_days=60)
    r = mon.check_artist(a)
    assert r["found"] == 3 and r["wanted"] == 3, r
    rel = {x["release_id"]: x for x in mon.list_releases("wanted")}
    d = rel["goose2026-08-14"]["data"]
    assert d["formats"] == ["flac", "mp3"] and d["source_type"] == "AUD" and d["date"] == "2026-08-14"
    assert rel["goose2026-09-02"]["data"]["hires"]
    assert mon.get_artist(a["id"])["last_checked"] == "2026-09-22"
    assert "waiting in Queue" in mon.get_artist(a["id"])["last_note"]


def test_more_than_one_page_is_read(tmp_path, monkeypatch):
    """Before 0.0.10 only the first 50 were read, yet 'seen up to' moved past
    the rest — so they were never picked up."""
    mon, lma, _ = _setup(tmp_path)
    docs = [{"identifier": f"band2026-{i:04d}.flac", "creator": "Band", "date": "2026-01-01",
             "addeddate": f"2026-09-{1 + i % 28:02d}T00:00:00Z", "format": ["Flac"]} for i in range(230)]
    calls = _fake_archive(monkeypatch, lma, docs)
    a = mon.save_artist("Band", lookback_days=-1)
    assert a["last_checked"] == ""                   # everything
    r = mon.check_artist(a)
    assert r["found"] == 230 and r["new"] == 230
    assert sum("advancedsearch" in c for c in calls) == 3     # 100 + 100 + 30


def test_nothing_found_says_why(tmp_path, monkeypatch):
    mon, lma, db = _setup(tmp_path)
    il = importlib.import_module("app.indexers.lma").LiveMusicArchive
    q_exact = il()._artist_query("Billy Strings Band")
    _fake_archive(monkeypatch, lma, [], by_name={
        q_exact: [],
        '("Billy Strings Band") AND mediatype:etree': [{"identifier": "x", "creator": "Billy Strings"}] * 3,
    })
    a = mon.save_artist("Billy Strings Band", lookback_days=30)
    r = mon.check_artist(a)
    assert "nothing at all under the name 'Billy Strings Band'" in r["summary"]
    assert "Billy Strings" in r["summary"].split("names it does have:")[1]
    logged = db._conn().execute("SELECT level, detail FROM log ORDER BY id DESC LIMIT 1").fetchone()
    assert logged["level"] == "warn" and "Billy Strings Band" in logged["detail"]


def test_quiet_artist_points_to_look_back(tmp_path, monkeypatch):
    mon, lma, _ = _setup(tmp_path)
    il = importlib.import_module("app.indexers.lma").LiveMusicArchive
    q = il()._artist_query("Goose")
    _fake_archive(monkeypatch, lma, [], by_name={q: GOOSE})     # nothing recent; 3 ever
    a = mon.save_artist("Goose", lookback_days=0)
    _c = mon._c()
    _c.execute("UPDATE monitored_artists SET last_checked='2026-10-01' WHERE id=?", (a["id"],))
    _c.commit()
    # since-filtered query matches nothing in this fake
    r = mon.check_artist(mon.get_artist(a["id"]))
    assert r["found"] == 0 and "Look back" in r["summary"]


def test_look_back_rejudges_what_was_passed_over(tmp_path, monkeypatch):
    mon, lma, _ = _setup(tmp_path)
    _fake_archive(monkeypatch, lma, GOOSE)
    a = mon.save_artist("Goose", lookback_days=-1, sources=["SBD"])
    r = mon.check_artist(a)
    assert r["skipped"] == 3 and r["wanted"] == 0          # all three are audience tapes
    assert "AUD isn't one of your sources" in mon.list_releases("skipped")[0]["reason"]
    assert "passed over" in r["summary"]
    rid = mon.list_releases("skipped")[0]["id"]
    mon.ignore_release(rid)                                 # your own decision survives a look-back
    mon.save_artist("Goose", sources=[])
    mon.look_back(a["id"], -1)
    r2 = mon.check_artist(mon.get_artist(a["id"]))
    assert r2["new"] == 2 and r2["wanted"] == 2
    assert len(mon.list_releases("ignored")) == 1


def test_auto_grab_writes_the_torrent(tmp_path, monkeypatch):
    tdir = tmp_path / "torrents"
    tdir.mkdir()
    mon, lma, _ = _setup(tmp_path, torrents={"watch_dir": str(tdir), "enabled": False})
    _fake_archive(monkeypatch, lma, GOOSE[:1])
    monkeypatch.setattr(lma, "_get", lambda url, timeout=30: b"d8:announce0:e")
    a = mon.save_artist("Goose", lookback_days=-1, action="grab")
    r = mon.check_artist(a)
    assert r["grabbed"] == 1, r
    assert list(tdir.glob("*.torrent"))
    assert any("No download client" in g for g in mon.gates())


def test_gates_explain_a_quiet_setup(tmp_path, monkeypatch):
    mon, lma, _ = _setup(tmp_path, safety={"dry_run": True})
    mon.save_artist("Goose", action="grab")
    g = " ".join(mon.gates())
    assert "No .torrent folder" in g and "Dry run is on" in g


def test_adding_an_artist_checks_straight_away(tmp_path, monkeypatch):
    c = client(tmp_path)
    setup_admin(c)
    c.post("/api/setup/folders", json={"watch": str(tmp_path / "watch"), "library": str(tmp_path / "music")})
    c.post("/api/setup/finish", json={"index_library": False})
    sched = importlib.import_module("app.scheduler")
    ran = []
    monkeypatch.setattr(sched, "run_in_background", lambda fn, *a: ran.append((fn.__name__, a)))
    r = c.post("/api/monitor/artists", json={"name": "Goose", "lookback_days": 365, "action": "grab"}).json()
    assert r["checking"] and ran and ran[0][0] == "check_artist"
    assert r["action"] == "grab"
    # editing an artist that's been checked doesn't trigger another check
    conn = importlib.import_module("app.monitor")._c()
    conn.execute("UPDATE monitored_artists SET checked_at=1")
    conn.commit()
    ran.clear()
    c.post("/api/monitor/artists", json={"name": "Goose", "upgrades": True})
    assert not ran
    lb = c.post(f"/api/monitor/artists/{r['id']}/lookback", json={"days": -1}).json()
    assert lb["checking"] and lb["last_checked"] == ""

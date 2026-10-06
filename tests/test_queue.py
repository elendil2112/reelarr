"""Queue → Downloading: every client reports queue order, speeds, ETA etc.
the same way, and the API keeps finished downloads listed until imported."""
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402
from test_auth import client, setup_admin  # noqa: E402


def test_deluge_fields(tmp_path):
    fresh_app(tmp_path)
    from app.clients.deluge import DelugeClient as C
    c = C("http://x")
    c._rpc = lambda m, p=None: {
        "aa": {"name": "gd1977-05-08", "progress": 42.5, "save_path": "/dl", "state": "Downloading",
               "label": "bootlegs", "is_finished": False, "queue": 0, "total_wanted": 1000,
               "total_done": 425, "download_payload_rate": 2048, "upload_payload_rate": 10,
               "eta": 120, "num_seeds": 3, "num_peers": 7, "ratio": 0.1, "time_added": 1700000000},
        "bb": {"name": "bs2024", "progress": 0, "state": "Queued", "label": "bootlegs", "queue": 4,
               "eta": 0, "num_seeds": 0, "download_payload_rate": 0},
        "cc": {"name": "other", "progress": 100, "state": "Seeding", "label": "movies", "queue": -1},
    }
    t = {x.id: x for x in c.get_torrents("bootlegs")}
    assert set(t) == {"aa", "bb"}
    a, b = t["aa"], t["bb"]
    assert (a.status, a.queue, a.size, a.done, a.dl_speed, a.eta, a.seeds, a.peers) == \
        ("downloading", 1, 1000, 425, 2048, 120, 3, 7)
    assert (b.status, b.queue, b.eta) == ("queued", 5, None)


def test_qbittorrent_fields(tmp_path):
    fresh_app(tmp_path)
    from app.clients.qbittorrent import QBittorrentClient as C
    c = C("http://x")
    rows = [
        {"hash": "AA", "name": "phish1997", "progress": 0.5, "state": "downloading", "priority": 2,
         "size": 2000, "completed": 1000, "dlspeed": 5000, "upspeed": 1, "eta": 200,
         "num_seeds": 4, "num_leechs": 9, "ratio": 0.0, "added_on": 1700000001, "category": "bootlegs"},
        {"hash": "BB", "name": "stuck", "progress": 0.1, "state": "stalledDL", "priority": 1,
         "eta": 8640000, "category": "bootlegs"},
        {"hash": "CC", "name": "done", "progress": 1, "state": "stalledUP", "priority": 0,
         "eta": 8640000, "category": "bootlegs"},
    ]
    c._http = lambda path, *a, **k: (200, json.dumps(rows).encode(), {})
    t = {x.id: x for x in c.get_torrents("bootlegs")}
    assert (t["aa"].status, t["aa"].queue, t["aa"].done, t["aa"].eta, t["aa"].peers) == \
        ("downloading", 2, 1000, 200, 9)
    assert (t["bb"].status, t["bb"].eta) == ("stalled", None)          # ∞ → unknown
    assert (t["cc"].status, t["cc"].queue, t["cc"].finished) == ("seeding", None, True)


def test_transmission_fields(tmp_path):
    fresh_app(tmp_path)
    from app.clients.transmission import TransmissionClient as C
    c = C("http://x")
    c._rpc = lambda m, a=None, _retry=True: {"torrents": [
        {"hashString": "AA", "name": "goose2026", "percentDone": 0.25, "status": 4, "labels": ["bootlegs"],
         "queuePosition": 0, "sizeWhenDone": 4000, "leftUntilDone": 3000, "rateDownload": 900,
         "rateUpload": 0, "eta": 3600, "peersSendingToUs": 2, "peersGettingFromUs": 1,
         "uploadRatio": -1, "addedDate": 1700000002},
        {"hashString": "BB", "name": "waiting", "percentDone": 0, "status": 3, "labels": ["bootlegs"],
         "queuePosition": 3, "eta": -1},
        {"hashString": "CC", "name": "broken", "percentDone": 0.3, "status": 4, "labels": ["bootlegs"],
         "error": 3, "eta": -2},
    ]}
    t = {x.id: x for x in c.get_torrents("bootlegs")}
    assert (t["aa"].status, t["aa"].queue, t["aa"].done, t["aa"].eta) == ("downloading", 1, 1000, 3600)
    assert (t["bb"].status, t["bb"].queue, t["bb"].eta) == ("queued", 4, None)
    assert t["cc"].status == "error"


def test_queue_api_lists_until_imported(tmp_path, monkeypatch):
    c = client(tmp_path)
    setup_admin(c)
    cfgm = importlib.import_module("app.config")
    cfgm.set_key(["torrents"], {**cfgm.load()["torrents"], "enabled": True, "url": "http://x",
                                "label": "bootlegs", "import_completed": True})
    clients = importlib.import_module("app.clients")
    base = importlib.import_module("app.clients.base")
    torrents = importlib.import_module("app.torrents")

    class Fake:
        name = "Fake"
        def connect(self): pass
        def get_torrents(self, label=""):
            return [base.TorrentInfo(id="a", name="dl", progress=0.5, status="downloading", queue=1, eta=60),
                    base.TorrentInfo(id="b", name="done-waiting", progress=1, finished=True, status="seeding"),
                    base.TorrentInfo(id="c", name="done-imported", progress=1, finished=True, status="seeding")]
    monkeypatch.setattr(clients, "build", lambda cfg: Fake())
    torrents._mark_imported("c")
    r = c.get("/api/queue").json()
    rows = {d["name"]: d for d in r["downloading"]}
    assert set(rows) == {"dl", "done-waiting"}
    assert rows["dl"]["queue"] == 1 and rows["dl"]["eta"] == 60
    assert rows["done-waiting"]["status"] == "import pending"
    assert "save_path" not in rows["dl"]


def test_recently_grabbed_shows_progress_and_eta(tmp_path, monkeypatch):
    """Recently grabbed: each grab carries its torrent's status, progress and
    ETA — including ones already handed to the library."""
    c = client(tmp_path)
    setup_admin(c)
    cfgm = importlib.import_module("app.config")
    cfgm.set_key(["torrents"], {**cfgm.load()["torrents"], "enabled": True, "url": "http://x",
                                "label": "bootlegs", "import_completed": True})
    clients = importlib.import_module("app.clients")
    base = importlib.import_module("app.clients.base")
    torrents = importlib.import_module("app.torrents")
    monitor = importlib.import_module("app.monitor")
    a = monitor.save_artist("Goose")
    conn = monitor._c()
    for rid, title in (("goose2026-09-02", "Goose Live at Red Rocks"), ("goose2026-08-14", "x"),
                       ("goose2026-01-01", "y")):
        conn.execute("INSERT INTO releases (artist_id, indexer, release_id, status, reason, data, first_seen, updated_at) "
                     "VALUES (?, 'lma', ?, 'grabbed', '', ?, 1, 1)",
                     (a["id"], rid, json.dumps({"title": title, "date": rid[5:]})))
    conn.commit()

    class Fake:
        name = "Fake"
        def connect(self): pass
        def get_torrents(self, label=""):
            return [base.TorrentInfo(id="a", name="goose2026-09-02", progress=0.4, status="downloading", eta=90),
                    base.TorrentInfo(id="b", name="goose2026-08-14", progress=1, finished=True, status="seeding")]
    monkeypatch.setattr(clients, "build", lambda cfg: Fake())
    torrents._mark_imported("b")
    r = c.get("/api/queue").json()
    g = {x["release_id"]: x for x in r["grabbed"]}
    assert g["goose2026-09-02"]["client"] == {"status": "downloading", "progress": 0.4, "eta": 90,
                                              "dl_speed": None, "name": "goose2026-09-02"}
    assert g["goose2026-08-14"]["client"]["status"] == "imported"
    assert g["goose2026-01-01"]["client"] is None             # not in the client
    assert [d["name"] for d in r["downloading"]] == ["goose2026-09-02"]

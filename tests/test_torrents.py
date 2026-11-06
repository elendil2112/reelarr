"""Torrent intake (hand .torrent/.magnet files to the client) and bringing
finished downloads back into the watch folder, against a stand-in client."""
import hashlib
import importlib
import os
import time
from pathlib import Path

import pytest

from test_safety import fresh_app, offline


def _bencode(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(_bencode(i) for i in x) + b"e"
    return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in sorted(x.items())) + b"e"


INFO = {"name": "gd1977-05-08.sbd.flac16", "piece length": 16384, "pieces": b"\0" * 20,
        "length": 1}
TORRENT = _bencode({"announce": "http://tracker/", "info": INFO})
INFOHASH = hashlib.sha1(_bencode(INFO)).hexdigest()


class FakeClient:
    name, label_term = "Fake", "label"
    instances = []

    def __init__(self, url="", user="", pw=""):
        self.added, self.magnets = [], []
        self.torrents = []
        self.fail_connect = False
        FakeClient.instances.append(self)

    def connect(self):
        if FakeClient.unreachable:
            from app import clients
            raise clients.DownloadClientError("connection refused")

    def add_torrent_file(self, p, label, dl_dir, paused):
        from app.clients.base import AddResult
        self.added.append((Path(p).name, label, dl_dir, paused))
        return AddResult(torrent_id=INFOHASH, already=Path(p).stem == "dupe",
                         label=label, name="")

    def add_magnet(self, uri, label, dl_dir, paused):
        from app.clients.base import AddResult
        self.magnets.append(uri)
        return AddResult(torrent_id="m", label=label)

    def get_torrents(self, label=""):
        return FakeClient.listing


@pytest.fixture
def m(tmp_path, monkeypatch):
    tdir = tmp_path / "torrents"
    tdir.mkdir()
    mods = fresh_app(tmp_path, offline({"torrents": {
        "enabled": True, "watch_dir": str(tdir), "url": "http://client", "label": "reelarr",
        "settle_seconds": 0, "import_completed": True, "min_age_minutes": 0}}))
    mods["database"].init()
    importlib.import_module("app.setup").mark_done()
    mods["torrents"] = importlib.import_module("app.torrents")
    clients = importlib.import_module("app.clients")
    FakeClient.instances, FakeClient.unreachable, FakeClient.listing = [], False, []
    monkeypatch.setattr(clients, "build", lambda cfg: FakeClient())
    mods["tdir"] = tdir
    return mods


def test_torrent_and_magnet_handed_to_client_and_filed_away(m):
    (m["tdir"] / "show.torrent").write_bytes(TORRENT)
    (m["tdir"] / "other.magnet").write_text("\nmagnet:?xt=urn:btih:abc\n")
    r = m["torrents"].scan()
    assert (r["added"], r["failed"]) == (2, 0)
    c = FakeClient.instances[-1]
    assert c.added == [("show.torrent", "reelarr", "", False)]
    assert c.magnets == ["magnet:?xt=urn:btih:abc"]
    assert (m["tdir"] / "_added" / "show.torrent").exists()
    assert not (m["tdir"] / "show.torrent").exists()


def test_already_there_counts_as_duplicate(m):
    (m["tdir"] / "dupe.torrent").write_bytes(TORRENT)
    r = m["torrents"].scan()
    assert (r["added"], r["duplicates"]) == (0, 1)


@pytest.mark.parametrize("name,data", [
    ("cut.torrent", TORRENT[:40]),                     # truncated download
    ("html.torrent", b"<html>login required</html>"),
    ("empty.magnet", b"not a link"),
])
def test_broken_files_go_to_failed_with_a_reason(m, name, data):
    (m["tdir"] / name).write_bytes(data)
    r = m["torrents"].scan()
    assert r["failed"] == 1 and FakeClient.instances[-1].added == []
    assert (m["tdir"] / "_failed" / name).exists()
    msg = m["database"].recent_log(5)[0]["detail"]
    assert name in msg and ("corrupt" in msg or "magnet" in msg)


def test_files_still_being_written_wait(m):
    m["config"].save({"torrents": {"settle_seconds": 3600}})
    (m["tdir"] / "show.torrent").write_bytes(TORRENT)
    r = m["torrents"].scan()
    assert r == {"added": 0, "found": 1, "waiting": 1}


def test_delete_after_add_when_asked(m):
    m["config"].save({"torrents": {"after_add": "delete"}})
    (m["tdir"] / "show.torrent").write_bytes(TORRENT)
    m["torrents"].scan()
    assert not any(m["tdir"].rglob("*.torrent"))


def test_unreachable_client_leaves_files_alone(m):
    FakeClient.unreachable = True
    (m["tdir"] / "show.torrent").write_bytes(TORRENT)
    r = m["torrents"].scan()
    assert "connection refused" in r["error"]
    assert (m["tdir"] / "show.torrent").exists()


def test_disabled_does_nothing(m):
    m["config"].save({"torrents": {"enabled": False}})
    (m["tdir"] / "show.torrent").write_bytes(TORRENT)
    assert m["torrents"].scan() == {"skipped": "disabled"}


@pytest.mark.parametrize("p,want", [
    ("/downloads/show", "/torrent-downloads/show"),
    ("/downloads/music/show", "/music-dl/show"),           # longest prefix wins
    ("/downloads", "/torrent-downloads"),
    ("/downloadsX/show", "/downloadsX/show"),              # not a real prefix
    ("/other/show", "/other/show"),
])
def test_map_path(m, p, want):
    mapping = {"/downloads": "/torrent-downloads/", "/downloads/music": "/music-dl"}
    assert m["torrents"].map_path(p, mapping) == want


def _finished(tmp_path, name="gd1977-05-08.sbd", tid="t1", single=False):
    from app.clients.base import TorrentInfo
    dl = tmp_path / "dl"
    src = dl / (name + (".flac" if single else ""))
    if single:
        dl.mkdir(exist_ok=True)
        src.write_bytes(b"x")
    else:
        (src / "d1").mkdir(parents=True, exist_ok=True)
        (src / "d1" / "t01.flac").write_bytes(b"x")
    old = time.time() - 3600
    os.utime(src, (old, old))
    return TorrentInfo(id=tid, name=src.name, save_path="/client/dl", finished=True,
                       progress=1.0, content_path=f"/client/dl/{src.name}")


def test_finished_download_copied_once(m, tmp_path):
    m["config"].save({"torrents": {"path_map": {"/client/dl": str(tmp_path / "dl")}}})
    FakeClient.listing = [_finished(tmp_path)]
    r = m["torrents"].import_completed()
    assert r["imported"] == 1
    show = tmp_path / "watch" / "gd1977-05-08.sbd"
    assert (show / "d1" / "t01.flac").exists()
    assert (tmp_path / "dl" / "gd1977-05-08.sbd").exists()      # copy keeps seeding
    assert not any(p.name.startswith(".importing") for p in (tmp_path / "watch").iterdir())
    assert m["torrents"].import_completed()["imported"] == 0    # remembered


def test_single_file_torrent_gets_a_folder(m, tmp_path):
    m["config"].save({"torrents": {"path_map": {"/client/dl": str(tmp_path / "dl")}}})
    FakeClient.listing = [_finished(tmp_path, single=True)]
    m["torrents"].import_completed()
    assert (tmp_path / "watch" / "gd1977-05-08.sbd.flac" / "gd1977-05-08.sbd.flac").is_file()


def test_unmapped_path_is_reported_not_guessed(m, tmp_path):
    FakeClient.listing = [_finished(tmp_path)]
    r = m["torrents"].import_completed()
    assert r["unmapped"] == 1 and r["imported"] == 0
    assert "path mapping" in m["database"].recent_log(5)[0]["detail"]


def test_unfinished_and_young_downloads_wait(m, tmp_path):
    m["config"].save({"torrents": {"path_map": {"/client/dl": str(tmp_path / "dl")},
                                   "min_age_minutes": 120}})
    t = _finished(tmp_path)
    FakeClient.listing = [t]
    assert m["torrents"].import_completed()["waiting"] == 1
    t.finished = False
    assert m["torrents"].import_completed()["waiting"] == 0


def test_name_clash_in_watch_folder_gets_a_new_name(m, tmp_path):
    m["config"].save({"torrents": {"path_map": {"/client/dl": str(tmp_path / "dl")}}})
    (tmp_path / "watch" / "gd1977-05-08.sbd").mkdir()
    FakeClient.listing = [_finished(tmp_path)]
    m["torrents"].import_completed()
    assert len([p for p in (tmp_path / "watch").iterdir()
                if p.name.startswith("gd1977-05-08.sbd")]) == 2

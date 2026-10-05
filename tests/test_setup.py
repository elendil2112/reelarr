"""Phase 2: zero-config compose, setup wizard backend, path-mapping helper."""
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402
from test_auth import client, setup_admin  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_folder_checks(tmp_path):
    fresh_app(tmp_path)
    setup = importlib.import_module("app.setup")
    importlib.import_module("app.database").init()
    w, l = tmp_path / "watch", tmp_path / "music"
    assert setup.check_folders(str(w), str(l))["ok"]
    assert not setup.check_folders("", str(l))["ok"]
    assert not setup.check_folders("relative/path", str(l))["ok"]
    assert "different" in setup.check_folders(str(w), str(w))["problems"][0]
    (l / "inbox").mkdir()
    assert "inside the library" in setup.check_folders(str(l / "inbox"), str(l))["problems"][0]
    (w / "lib").mkdir()
    assert "inside the watch" in setup.check_folders(str(w), str(w / "lib"))["problems"][0]
    assert not setup.check_folders(str(tmp_path / "nope"), str(l))["ok"]
    assert "config" in " ".join(setup.check_folders(str(tmp_path / "config"), str(l))["problems"])
    (l / "Grateful Dead").mkdir()
    notes = setup.check_folders(str(w), str(l))["notes"]
    assert any("artist folder" in n for n in notes)


def test_mapping_suggestion_finds_the_shared_folder(tmp_path):
    fresh_app(tmp_path)
    setup = importlib.import_module("app.setup")
    media = tmp_path / "media"
    (media / "torrents" / "complete" / "gd1977-05-08").mkdir(parents=True)
    hit = setup._suggest("/downloads/complete/gd1977-05-08", [str(media)])
    assert hit == ("/downloads", str(media / "torrents"))
    torrents = importlib.import_module("app.torrents")
    assert torrents.map_path("/downloads/complete/gd1977-05-08", {hit[0]: hit[1]}) == \
        str(media / "torrents" / "complete" / "gd1977-05-08")
    assert setup._suggest("/downloads/complete/not-here-anywhere", [str(media / "torrents" / "complete" / "gd1977-05-08")]) is None


def test_probe_proposes_a_mapping_that_fixes_every_download(tmp_path, monkeypatch):
    fresh_app(tmp_path)
    importlib.import_module("app.database").init()
    setup = importlib.import_module("app.setup")
    clients = importlib.import_module("app.clients")
    base = importlib.import_module("app.clients.base")
    media = tmp_path / "media"
    for name in ("show-a", "show-b"):
        (media / "dl" / "done" / name).mkdir(parents=True)

    class Fake:
        def connect(self): pass
        def get_torrents(self, label=""):
            return [base.TorrentInfo(id=n, name=n, save_path="/data/done", content_path=f"/data/done/{n}",
                                     finished=True) for n in ("show-a", "show-b")]
    monkeypatch.setattr(clients, "build", lambda cfg: Fake())
    monkeypatch.setattr(setup, "roots", lambda: [str(media)])
    r = setup.probe_client({"url": "http://x"})
    assert r["visible"] == 0 and r["total"] == 2
    assert r["suggestion"] == {"client": "/data", "local": str(media / "dl"), "fixes": 2, "of": 2}
    r2 = setup.probe_client({"url": "http://x", "path_map": {"/data": str(media / "dl")}})
    assert r2["visible"] == 2 and "suggestion" not in r2


def test_nothing_runs_until_the_wizard_finishes(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    assert c.get("/api/system").json()["setup_done"] is False
    watcher = importlib.import_module("app.watcher")
    assert watcher.scan(force=True) == {"skipped": "setup wizard not finished"}
    sched = importlib.import_module("app.scheduler")
    assert "watch_scan" not in sched.next_runs()
    assert c.get("/setup").status_code == 200

    bad = c.post("/api/setup/folders", json={"watch": str(tmp_path / "music"), "library": str(tmp_path / "music")})
    assert bad.status_code == 400 and bad.json()["problems"]

    ok = c.post("/api/setup/folders", json={"watch": str(tmp_path / "watch"), "library": str(tmp_path / "music")})
    assert ok.status_code == 200
    cfg = importlib.import_module("app.config").load()
    assert cfg["paths"]["watch_dir"] == str(tmp_path / "watch")
    r = c.post("/api/setup/finish", json={"index_library": False})
    assert r.status_code == 200
    assert c.get("/api/system").json()["setup_done"] is True
    assert "watch_scan" in sched.next_runs()


def test_browse_lists_folders_and_refuses_files(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    (tmp_path / "music" / "Phish").mkdir()
    (tmp_path / "music" / ".hidden").mkdir()
    r = c.get("/api/setup/browse", params={"path": str(tmp_path / "music")}).json()
    assert [d["name"] for d in r["dirs"]] == ["Phish"] and r["writable"] is True
    (tmp_path / "f.txt").write_text("x")
    assert c.get("/api/setup/browse", params={"path": str(tmp_path / "f.txt")}).status_code == 400
    assert c.get("/api/setup/browse").json()["dirs"]           # roots
    m = c.post("/api/setup/mkdir", json={"path": str(tmp_path / "music" / "Goose")})
    assert m.status_code == 200 and (tmp_path / "music" / "Goose").is_dir()


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_compose_needs_no_env_file(tmp_path):
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("REELARR_", "PUID", "PGID", "UMASK"))}
    r = subprocess.run(["docker", "compose", "config"], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "not set" not in r.stderr and "invalid" not in r.stderr.lower()
    assert "8189" in r.stdout and "/config" in r.stdout and "/media" in r.stdout


def test_entrypoint_is_valid_shell():
    assert subprocess.run(["sh", "-n", str(ROOT / "docker" / "entrypoint.sh")]).returncode == 0


def test_mount_detection_ignores_docker_plumbing(tmp_path):
    fresh_app(tmp_path)
    setup = importlib.import_module("app.setup")
    media = tmp_path / "mnt user"
    media.mkdir()
    esc = str(media).replace(" ", "\\040")
    info = "\n".join([
        "1 0 0:50 / / rw - overlay overlay rw",
        "2 1 0:51 / /proc rw - proc proc rw",
        "3 1 8:1 /appdata/reelarr /config rw - xfs /dev/md1 rw",
        f"4 1 0:60 / {esc} rw - fuse.shfs shfs rw",
        "5 1 8:1 /x /etc/hosts rw - xfs /dev/md1 rw",
        "6 1 0:70 / /dev/shm rw - tmpfs shm rw",
        "7 1 8:1 /etc/resolv.conf /etc/resolv.conf rw - xfs /dev/md1 rw",
    ])
    assert setup.container_mounts(info) == [str(media)]
    # a user folder that merely starts like a system one isn't filtered
    lib = Path("/library-test-reelarr")
    try:
        lib.mkdir(exist_ok=True)
        assert setup.container_mounts(f"8 1 8:1 / {lib} rw - xfs /dev/md1 rw") == [str(lib)]
    except PermissionError:
        pass
    finally:
        try:
            lib.rmdir()
        except OSError:
            pass


def test_empty_media_mount_is_explained(tmp_path, monkeypatch):
    fresh_app(tmp_path)
    setup = importlib.import_module("app.setup")
    paths = importlib.import_module("app.paths")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(paths, "in_container", lambda: True)
    monkeypatch.setattr(setup, "roots", lambda: [str(media)])
    assert setup.media_problem() == "empty"
    (media / ".DS_Store").write_text("")
    assert setup.media_problem() == "empty"            # hidden files don't count
    (media / "music").mkdir()
    assert setup.media_problem() == ""
    monkeypatch.setattr(setup, "roots", lambda: [])
    assert setup.media_problem() == "no_mount"
    monkeypatch.setattr(paths, "in_container", lambda: False)
    assert setup.media_problem() == ""                 # bare metal sees the whole disk
    assert "REELARR_MEDIA_DIR=/mnt/user" in setup.MOUNT_HELP

"""Pre-release hardening: headers, folder browser limits, secrets out of
logs, .torrent parsing limits, archive extraction limits."""
import importlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app, offline  # noqa: E402
from test_auth import client, setup_admin  # noqa: E402


def test_security_headers_everywhere(tmp_path):
    c = client(tmp_path)
    for path in ("/login", "/api/health", "/static/style.css"):
        h = c.get(path).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"], path
        assert "script-src 'self'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "same-origin"
    assert c.get("/api/health").headers["cache-control"] == "no-store"


def test_folder_browser_never_shows_the_config_folder(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    cfg = tmp_path / "config"
    assert c.get("/api/setup/browse", params={"path": str(cfg)}).status_code == 400
    assert c.get("/api/setup/browse", params={"path": str(cfg / "backups")}).status_code == 400
    listing = c.get("/api/setup/browse", params={"path": str(tmp_path)}).json()
    assert "config" not in [d["name"] for d in listing["dirs"]]
    assert c.post("/api/setup/mkdir", json={"path": str(cfg / "x")}).status_code == 400
    assert c.post("/api/setup/mkdir", json={"path": "relative/dir"}).status_code == 400


def test_secrets_never_reach_the_log(tmp_path):
    m = fresh_app(tmp_path)
    db = m["database"]
    db.init()
    db.log("warn", "torrent", "can't reach http://admin:hunter2@deluge:8112/json — refused")
    db.log("warn", "x", "GET /api/thing?apikey=abc123&x=1 password=letmein token=t0k")
    text = " ".join(r["detail"] for r in db.recent_log())
    assert "hunter2" not in text and "abc123" not in text and "letmein" not in text and "t0k" not in text
    assert "http://***@deluge:8112" in text and "apikey=***" in text


@pytest.mark.parametrize("data", [
    b"d" + b"l" * 100000 + b"e" * 100000 + b"e",           # absurdly deep
    b"d4:infod4:name999999999:xee",                          # length past the end
    b"d4:info",                                              # truncated
    b"d4:infoi12",                                           # unterminated int
    b"d" + b"9" * 40 + b":x",                                # silly length digits
])
def test_hostile_torrents_fail_cleanly(tmp_path, data):
    fresh_app(tmp_path)
    base = importlib.import_module("app.clients.base")
    with pytest.raises(ValueError):
        base.torrent_infohash(data)
    assert base.torrent_name(data) == ""


def test_valid_torrent_still_hashes(tmp_path):
    fresh_app(tmp_path)
    base = importlib.import_module("app.clients.base")
    t = b"d8:announce3:url4:infod6:lengthi5e4:name4:showee"
    assert len(base.torrent_infohash(t)) == 40 and base.torrent_name(t) == "show"


def test_archive_limits_and_unsafe_files(tmp_path):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False},
                                     "watcher": {"max_extract_gb": 1}}))
    m["database"].init()
    pipeline, fileops = m["pipeline"], m["fileops"]
    cfg = importlib.import_module("app.config").load()
    show = tmp_path / "watch" / "gd1977-05-08"
    show.mkdir(parents=True)
    with zipfile.ZipFile(show / "bomb.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.wav", b"\0" * (200 * 2**20))
    with zipfile.ZipFile(show / "ok.zip", "w") as z:
        z.writestr("d1t01.flac", b"fLaC")
        z.writestr("crack.exe", b"MZ")
    (show / "link").symlink_to("/etc/passwd")
    logs = []
    b = fileops.Batch("test", kind="test", dry_run=False)
    pipeline.prep_folder(show, cfg, lambda lvl, ev, msg: logs.append(msg), b)
    names = {p.name for p in show.iterdir()}
    assert "bomb.zip" in names and "big.wav" not in names          # refused, left as is
    assert "d1t01.flac" in names and "crack.exe" not in names       # program trashed
    assert "link" not in names
    assert any("zip bomb" in x for x in logs) and any("crack.exe" in x for x in logs)
    assert list((tmp_path / "watch" / ".reelarr-trash").rglob("crack.exe"))


def test_redaction_covers_headers_json_and_tricky_urls(tmp_path):
    fresh_app(tmp_path)
    db = importlib.import_module("app.database")
    cases = [
        "GET failed: Authorization: Bearer abc.def.ghi",
        "X-Api-Key: k3y-value-123",
        '{"username": "me", "password": "hunter2"}',
        "password: letmein",
        "http://user:p@ss@host:8080/path",
        "bot_token=MTIzNDU2.abc",
    ]
    out = " ".join(db.redact(c) for c in cases)
    for secret in ("abc.def.ghi", "k3y-value-123", "hunter2", "letmein", "p@ss", "MTIzNDU2"):
        assert secret not in out, secret
    assert "http://***@host:8080/path" in out and '"username": "me"' in out
    assert db.redact("monkey=banana key=x") == "monkey=banana key=x"     # not a secret


def test_config_folder_reached_through_another_mount_is_still_refused(tmp_path):
    import subprocess
    fresh_app(tmp_path)
    setup = importlib.import_module("app.setup")
    other = tmp_path / "mnt_user" / "appdata" / "reelarr"
    other.mkdir(parents=True)
    r = subprocess.run(["mount", "--bind", str(tmp_path / "config"), str(other)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("can't bind-mount here")
    try:
        assert setup.inside_config(other) and setup.inside_config(other / "backups")
        assert setup.list_dir(str(other)).get("error")
        assert not setup.check_folders(str(tmp_path / "watch"), str(other))["ok"]
    finally:
        subprocess.run(["umount", str(other)])


def test_tar_and_shared_budget(tmp_path):
    import io
    import tarfile
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}, "watcher": {"max_extract_gb": 0.0005}}))
    m["database"].init()
    cfg = importlib.import_module("app.config").load()
    show = tmp_path / "watch" / "show"
    show.mkdir(parents=True)
    with tarfile.open(show / "big.tar.gz", "w:gz") as t:
        data = b"\0" * 800_000                       # 0.8 MB > 0.5 MB budget
        ti = tarfile.TarInfo("big.wav")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    for n in ("a", "b"):                              # 0.3 MB each: fine alone, not together
        with zipfile.ZipFile(show / f"{n}.zip", "w") as z:
            z.writestr(f"{n}.flac", b"fLaC" + b"x" * 300_000)
    logs = []
    b = m["fileops"].Batch("t", kind="t", dry_run=False)
    m["pipeline"].prep_folder(show, cfg, lambda lvl, ev, msg: logs.append(msg), b)
    names = {p.name for p in show.iterdir()}
    assert "big.wav" not in names and "big.tar.gz" in names
    assert ("a.flac" in names) != ("b.flac" in names)     # exactly one fit in the budget
    assert sum("size limit" in x for x in logs) == 2


def test_bad_time_zone_never_stops_startup(tmp_path):
    from test_auth import client, setup_admin
    c = client(tmp_path)
    setup_admin(c)
    assert c.put("/api/settings", json={"timezone": "Bogus/Zone"}).status_code == 400
    sched = importlib.import_module("app.scheduler")
    assert sched.valid_timezone("Bogus/Zone") == sched.DEFAULT_TZ
    assert sched.valid_timezone("") == sched.DEFAULT_TZ
    assert sched.valid_timezone("Europe/Amsterdam") == "Europe/Amsterdam"


def test_odd_torrent_shapes(tmp_path):
    fresh_app(tmp_path)
    base = importlib.import_module("app.clients.base")
    with pytest.raises(ValueError):
        base.torrent_infohash(b"d4:infodl1:aei1eee")       # a list used as a key

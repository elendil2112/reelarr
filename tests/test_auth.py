"""Phase 1b: login, API key, secret masking, provider plug-in.

Run:  pip install pytest httpx fastapi mutagen apscheduler && python -m pytest tests -q
"""
import importlib
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


def client(tmp_path, settings=None, providers=""):
    os.environ["REELARR_PROVIDERS"] = providers
    fresh_app(tmp_path, settings)
    main = importlib.import_module("app.main")
    c = TestClient(main.app, base_url="http://reelarr.test")
    c.__enter__()            # runs startup (migrations, scheduler)
    return c


def setup_admin(c, user="admin", pw="correct horse"):
    r = c.post("/api/auth/setup", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    os.environ.pop("REELARR_PROVIDERS", None)


def test_fresh_install_demands_setup(tmp_path):
    c = client(tmp_path)
    r = c.get("/api/shows")
    assert r.status_code == 401 and r.json()["setup_required"] is True
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/auth/status").json()["setup_required"] is True


def test_setup_creates_session_and_only_runs_once(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    assert c.get("/api/shows").status_code == 200
    r = c.post("/api/auth/setup", json={"username": "mallory", "password": "x" * 12})
    assert r.status_code == 409
    c.post("/api/auth/logout")
    assert c.get("/api/shows").status_code == 401
    assert c.get("/api/auth/status").json()["setup_required"] is False


def test_password_rules_and_login_throttle(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/auth/setup", json={"username": "admin", "password": "short"}).status_code == 400
    setup_admin(c)
    c.post("/api/auth/logout")
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse"})
    assert r.status_code == 429          # locked out even with the right password


def test_login_and_password_change_signs_out_everyone(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": "ADMIN", "password": "correct horse"}).status_code == 200
    r = c.post("/api/security/password", json={"current": "correct horse", "new": "battery staple"})
    assert r.status_code == 200
    assert c.get("/api/shows").status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin", "password": "battery staple"}).status_code == 200


def test_api_key(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    key = c.get("/api/security").json()["api_key"]
    c.post("/api/auth/logout")
    assert c.get("/api/shows", headers={"X-Api-Key": key}).status_code == 200
    assert c.get(f"/api/shows?apikey={key}").status_code == 200
    assert c.get("/api/shows", headers={"X-Api-Key": "0" * 32}).status_code == 401


def test_cross_site_post_refused(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    r = c.post("/api/safety/dry-run", json={"on": True},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/safety/dry-run", json={"on": True},
               headers={"Origin": "http://reelarr.test"})
    assert r.status_code == 200


def test_no_login_must_be_deliberate(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/auth/setup", json={"mode": "none"}).status_code == 400
    assert c.post("/api/auth/setup", json={"mode": "none", "confirm": "no login"}).status_code == 200
    assert c.get("/api/shows").status_code == 200
    c.cookies.clear()
    assert c.get("/api/shows").status_code == 200


def test_secrets_are_masked_and_preserved(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    c.put("/api/settings", json={"torrents": {"password": "hunter2"},
                                 "sources": {"setlistfm_api_key": "abc123"}})
    s = c.get("/api/settings").json()
    assert s["torrents"]["password"] == "••••••"
    assert s["sources"]["setlistfm_api_key"] == "••••••"
    assert "hunter2" not in c.get("/api/settings").text
    # the browser sends the placeholder back unchanged → stored value kept
    c.put("/api/settings", json={"torrents": {"password": "••••••", "label": "shows"}})
    cfg = importlib.import_module("app.config").load()
    assert cfg["torrents"]["password"] == "hunter2" and cfg["torrents"]["label"] == "shows"
    # a general save can't switch off the login or dry-run
    c.put("/api/settings", json={"auth": {"mode": "none"}, "safety": {"dry_run": False}})
    cfg = importlib.import_module("app.config").load()
    assert cfg["auth"]["mode"] == "forms" and cfg["safety"]["dry_run"] is True
    raw = (tmp_path / "config" / "settings.json").stat().st_mode & 0o777
    assert raw == 0o600


FAKE_PROVIDER = '''
from pathlib import Path
from fastapi import APIRouter
from app.providers import Provider

router = APIRouter()

@router.get("/ping")
def ping():
    return {"pong": True}

provider = Provider(name="fake", label="Fake Store",
                    settings_defaults={"enabled": True, "token": ""},
                    secret_fields=("token",), router=router,
                    status=lambda: {"ok": 1})
'''


def test_provider_plugs_in_and_absent_one_is_invisible(tmp_path):
    c = client(tmp_path)
    setup_admin(c)
    assert c.get("/api/providers").json() == {"providers": [], "errors": {}}

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "reelarr_fake.py").write_text(textwrap.dedent(FAKE_PROVIDER))
    sys.path.insert(0, str(mod_dir))
    try:
        (tmp_path / "two").mkdir()
        c = client(tmp_path / "two", providers="reelarr_fake,not_installed_anywhere")
        setup_admin(c)
        p = c.get("/api/providers").json()
        assert [x["name"] for x in p["providers"]] == ["fake"] and p["errors"] == {}
        assert c.get("/api/providers/fake/ping").json() == {"pong": True}
        c.put("/api/settings", json={"fake": {"token": "s3cret"}})
        assert c.get("/api/settings").json()["fake"]["token"] == "••••••"
        assert c.get("/api/status").json()["providers"]["fake"] == {"ok": 1}
        c.post("/api/auth/logout")
        assert c.get("/api/providers/fake/ping").status_code == 401   # behind the login too
    finally:
        sys.path.remove(str(mod_dir))
        sys.modules.pop("reelarr_fake", None)

"""0.2.0 defaults: a new install sends a new artist's first show to Review;
an existing install keeps filing new artists itself."""
import json

from test_safety import fresh_app


def _settings(tmp_path, data=None):
    p = tmp_path / "config" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        p.write_text(json.dumps(data))
    return p


def test_new_install_reviews_new_artists(tmp_path):
    _settings(tmp_path)                                    # no settings file yet
    m = fresh_app(tmp_path)
    m["database"].init()
    assert m["config"].load()["filing"]["new_artist_policy"] == "review"


def test_existing_install_keeps_auto(tmp_path):
    _settings(tmp_path, {"_schema": 3, "safety": {"dry_run": False}})
    m = fresh_app(tmp_path)
    m["database"].init()
    assert m["config"].load()["filing"]["new_artist_policy"] == "auto"
    assert json.loads(_settings(tmp_path).read_text())["_schema"] == 4


def test_existing_choice_is_kept(tmp_path):
    _settings(tmp_path, {"_schema": 3, "filing": {"new_artist_policy": "review"}})
    m = fresh_app(tmp_path)
    m["database"].init()
    assert m["config"].load()["filing"]["new_artist_policy"] == "review"

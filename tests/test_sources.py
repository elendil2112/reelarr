"""setlist.fm, archive.org and MusicBrainz lookups, against canned replies.
Nothing here touches the network."""
import io
import json
import urllib.error

import pytest

from test_safety import fresh_app, offline


@pytest.fixture
def src(tmp_path, monkeypatch):
    m = fresh_app(tmp_path, offline())
    m["database"].init()
    import app.sources as sources
    sources._warned.clear()
    sources._genre_cache.clear()
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    calls = []

    def answer(replies):
        def urlopen(req, timeout=0):
            calls.append(req)
            url = req.full_url
            for frag, reply in replies.items():
                if frag in url:
                    if isinstance(reply, Exception):
                        raise reply
                    return io.BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
            raise AssertionError(f"unexpected request: {url}")
        monkeypatch.setattr(sources.urllib.request, "urlopen", urlopen)
    sources.answer, sources.calls, sources.db = answer, calls, m["database"]
    return sources


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


SETLIST = {"setlist": [{
    "artist": {"name": "Grateful Dead"}, "eventDate": "08-05-1977",
    "venue": {"name": "Barton Hall, Cornell University",
              "city": {"name": "Ithaca", "stateCode": "NY", "country": {"code": "US"}}},
    "sets": {"set": [
        {"song": [{"name": "Minglewood Blues"}, {"name": "Scarlet Begonias", "segue": ">"},
                  {"name": "Fire on the Mountain"}]},
        {"encore": 1, "song": [{"name": "One More Saturday Night"}, {"name": ""}]}]}}]}


def test_setlistfm_parsed(src):
    src.answer({"setlist.fm": SETLIST})
    m = src.fetch_setlistfm("Grateful Dead", "1977-05-08", "key")
    assert (m.artist, m.date, m.venue, m.city, m.state) == \
        ("Grateful Dead", "1977-05-08", "Barton Hall, Cornell University", "Ithaca", "NY")
    assert [t["title"] for t in m.tracks] == ["Minglewood Blues", "Scarlet Begonias",
                                              "Fire on the Mountain", "One More Saturday Night"]
    assert m.tracks[1]["segue"] and "segue" not in m.tracks[0]
    assert [t["disc"] for t in m.tracks] == [1, 1, 1, 99]
    req = src.calls[0]
    assert "artistName=Grateful+Dead" in req.full_url and "date=08-05-1977" in req.full_url
    assert req.headers["X-api-key"] == "key" and req.headers["User-agent"].startswith("Reelarr/")


def test_setlistfm_outside_us_uses_the_country(src):
    data = json.loads(json.dumps(SETLIST))
    data["setlist"][0]["venue"]["city"] = {"name": "Munich", "stateCode": "BY",
                                           "country": {"code": "DE", "name": "Germany"}}
    src.answer({"setlist.fm": data})
    assert src.fetch_setlistfm("Grateful Dead", "1977-05-08", "key").state == "Germany"


@pytest.mark.parametrize("args", [("", "1977-05-08", "k"), ("GD", "", "k"),
                                  ("GD", "1977-05-08", ""), ("GD", "May 8", "k")])
def test_setlistfm_needs_everything(src, args):
    src.answer({})
    assert src.fetch_setlistfm(*args) is None and src.calls == []


def test_no_setlist_that_day_is_quiet(src):
    src.answer({"setlist.fm": _http(404)})
    assert src.fetch_setlistfm("GD", "1977-05-08", "k") is None
    assert not [r for r in src.db.recent_log(5) if r["event"] == "sources"]


def test_bad_key_logged_once(src):
    src.answer({"setlist.fm": _http(401)})
    for _ in range(3):
        assert src.fetch_setlistfm("GD", "1977-05-08", "k") is None
    logs = [r["detail"] for r in src.db.recent_log(20) if r["event"] == "sources"]
    assert len(logs) == 1 and "API key" in logs[0]


@pytest.mark.parametrize("reply,words", [
    (urllib.error.URLError("Name or service not known"), "couldn't reach"),
    (TimeoutError("timed out"), "couldn't reach"),
    (b"<html>maintenance</html>", "isn't JSON"),
    (_http(429), "rate-limiting"),
])
def test_failures_are_soft_but_logged(src, reply, words):
    src.answer({"setlist.fm": reply})
    assert src.fetch_setlistfm("GD", "1977-05-08", "k") is None
    assert any(words in r["detail"] for r in src.db.recent_log(5))


def _sl(venue, songs, city="Ithaca"):
    return {"artist": {"name": "Band"}, "eventDate": "08-05-1977",
            "venue": {"name": venue, "city": {"name": city, "stateCode": "NY",
                                              "country": {"code": "US"}}},
            "sets": {"set": [{"song": [{"name": s} for s in songs]}]}}


def test_reverse_matches_the_venue(src):
    src.answer({"setlist.fm": {"setlist": [_sl("The State Theatre", ["A"]),
                                           _sl("Barton Hall", ["B"])]}})
    m = src.fetch_setlistfm_reverse("1977-05-08", "Ithaca", "Barton Hall", "k")
    assert m.venue == "Barton Hall" and "cityName=Ithaca" in src.calls[0].full_url


def test_reverse_wont_guess_a_different_venue(src):
    src.answer({"setlist.fm": {"setlist": [_sl("The State Theatre", ["A"])]}})
    assert src.fetch_setlistfm_reverse("1977-05-08", "Ithaca", "Barton Hall", "k") is None


def test_reverse_without_venue_needs_shared_songs(src):
    src.answer({"setlist.fm": {"setlist": [_sl("X", ["Loser", "El Paso", "Sugaree"]),
                                           _sl("Y", ["Other", "Songs"])]}})
    m = src.fetch_setlistfm_reverse("1977-05-08", "Ithaca", "", "k",
                                    known_titles=["Loser", "El Paso"])
    assert m.venue == "X"
    assert src.fetch_setlistfm_reverse("1977-05-08", "Ithaca", "", "k",
                                       known_titles=["Loser"]) is None


def test_reverse_two_shows_same_venue_no_titles_is_ambiguous(src):
    src.answer({"setlist.fm": {"setlist": [_sl("Barton Hall", ["A"]), _sl("Barton Hall", ["B"])]}})
    assert src.fetch_setlistfm_reverse("1977-05-08", "Ithaca", "Barton Hall", "k") is None


IA = {"metadata": {"creator": "Grateful Dead", "date": "1977-05-08",
                   "venue": "Barton Hall", "coverage": "Ithaca, NY",
                   "source": "SBD > Reel > DAT", "description": "x" * 900},
      "files": [{"name": "t02.flac", "format": "Flac", "title": "Loser", "track": "2"},
                {"name": "t01.flac", "format": "Flac", "title": "Minglewood", "track": "1"},
                {"name": "t01.mp3", "format": "VBR MP3", "title": "Minglewood", "track": "1"},
                {"name": "t03.flac", "format": "Flac", "title": "", "track": "3"},
                {"name": "info.txt", "format": "Text", "title": "notes"}]}


def test_archive_org_item(src):
    src.answer({"archive.org/metadata/gd1977-05-08.sbd": IA})
    m = src.fetch_ia("gd1977-05-08.sbd")
    assert (m.artist, m.date, m.venue, m.city, m.state, m.source_type) == \
        ("Grateful Dead", "1977-05-08", "Barton Hall", "Ithaca", "NY", "SBD")
    assert [t["title"] for t in m.tracks] == ["Minglewood", "Loser"]
    assert len(m.notes) == 500


@pytest.mark.parametrize("name", ["", "my show folder", "short", "gd 1977-05-08 (sbd)"])
def test_archive_org_skips_non_identifiers(src, name):
    src.answer({})
    assert src.fetch_ia(name) is None and src.calls == []


def test_musicbrainz_genre_skips_junk_tags_and_caches(src):
    src.answer({"ws/2/artist?query": {"artists": [{"id": "abc"}]},
                "ws/2/artist/abc": {"genres": [{"name": "seen live"}, {"name": "jam band"}]}})
    assert src.fetch_mb_genre("Grateful Dead") == "Jam Band"
    assert src.fetch_mb_genre("grateful dead") == "Jam Band"
    assert len(src.calls) == 2


def test_musicbrainz_outage_not_cached(src):
    src.answer({"musicbrainz": urllib.error.URLError("down")})
    assert src.fetch_mb_genre("Goose") == ""
    src.answer({"ws/2/artist?query": {"artists": [{"id": "g"}]},
                "ws/2/artist/g": {"tags": [{"name": "rock"}]}})
    assert src.fetch_mb_genre("Goose") == "Rock"

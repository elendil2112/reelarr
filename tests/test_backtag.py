"""Library re-check (Maintenance → Re-check the library against setlist.fm).

It rewrites tags and renames folders in a library people can't replace, so
every path that writes is covered here on real FLAC files."""
import json
import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC

from test_safety import fresh_app, make_audio, offline

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

ALBUM = "1977-05-08 Barton Hall, Ithaca, NY [SBD 12345]"


def _app(tmp_path, **filing):
    s = offline({"sources": {"use_setlistfm": True, "setlistfm_api_key": "k",
                             "setlistfm_delay_seconds": 0},
                 "safety": {"dry_run": False}, "filing": filing})
    m = fresh_app(tmp_path, s)
    m["database"].init()
    m["backtag"] = __import__("importlib").import_module("app.backtag")
    return m


def _show(lib: Path, titles, album=ALBUM, artist="Grateful Dead", year=True,
          seconds=None, prefix="gd"):
    parent = lib / artist / ("1977" if year else "")
    show = parent / f"{prefix}{album}"
    seconds = seconds or [200] * len(titles)
    for i, (t, sec) in enumerate(zip(titles, seconds), 1):
        f = show / f"gd77-05-08d1t{i:02d}.flac"
        make_audio(f, seconds=sec)
        a = FLAC(f)
        a["ARTIST"], a["ALBUM"], a["TITLE"] = artist, album, t
        a.save()
    return show


def _setlist(m, venue="Barton Hall, Cornell University", city="Ithaca", state="NY",
             songs=("Minglewood Blues", "Loser", "El Paso")):
    meta = m["pipeline"].ShowMeta()
    meta.artist, meta.date = "Grateful Dead", "1977-05-08"
    meta.venue, meta.city, meta.state = venue, city, state
    meta.tracks = [{"num": i, "title": t, "disc": 1, "set": 1} for i, t in enumerate(songs, 1)]
    return meta


def _titles(folder):
    return [FLAC(f)["TITLE"][0] for f in sorted(folder.rglob("*.flac"))]


def test_artist_comes_from_the_artist_folder_not_the_year_folder(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    show = _show(lib, ["Track 01", "Track 02", "Track 03"])
    asked = []
    m["backtag"].sources.fetch_setlistfm = lambda a, d, k: asked.append((a, d)) or None
    for f in show.glob("*.flac"):                 # no ARTIST tag: folder decides
        a = FLAC(f)
        del a["ARTIST"]
        a.save()
    m["backtag"]._propose(show, m["config"].load())
    assert asked == [("Grateful Dead", "1977-05-08")]


def test_junk_titles_fixed_and_counts_respected(tmp_path):
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Track 01", "Track 02", "Track 03"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m, venue="Barton Hall")
    p = m["backtag"]._propose(show, m["config"].load())
    assert p["verdict"] == "apply"
    assert [t["to"] for t in p["title_fixes"]] == ["Minglewood Blues", "Loser", "El Paso"]
    assert m["backtag"]._apply(p, m["config"].load()) is True
    assert _titles(show) == ["Minglewood Blues", "Loser", "El Paso"]


def test_tuning_track_does_not_shift_titles(tmp_path):
    """Four files, three songs, a 20-second tuning track first: the old
    index-based pairing put every title one file early."""
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Track 01", "Track 02", "Track 03", "Track 04"],
                 seconds=[20, 200, 190, 210])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m, venue="Barton Hall")
    p = m["backtag"]._propose(show, m["config"].load())
    fixes = {t["file"]: t["to"] for t in p["title_fixes"]}
    assert "gd77-05-08d1t01.flac" not in fixes
    assert fixes["gd77-05-08d1t02.flac"] == "Minglewood Blues"
    assert fixes["gd77-05-08d1t04.flac"] == "El Paso"


def test_setlist_that_doesnt_pair_goes_to_review_without_title_changes(tmp_path):
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Track 01", "Track 02"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(
        m, venue="Barton Hall", songs=[f"Song {i}" for i in range(1, 12)])
    p = m["backtag"]._propose(show, m["config"].load())
    assert p["verdict"] == "brig" and p["title_fixes"] == []
    assert "pair" in p["reason"]


def test_good_titles_never_overwritten(tmp_path):
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Minglewood Blues", "Loser", "El Paso"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(
        m, venue="Barton Hall", songs=("Other", "Songs", "Here"))
    p = m["backtag"]._propose(show, m["config"].load())
    assert p["verdict"] == "skip"


def test_venue_fix_retags_and_renames_keeping_the_prefix(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    show = _show(lib, ["Minglewood Blues", "Loser", "El Paso"])
    m["database"].upsert_show(str(show), status="filed", filed_path=str(show),
                              current_path=str(show), folder_name=show.name)
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    p = m["backtag"]._propose(show, m["config"].load())
    assert p["verdict"] == "apply" and p["changes"]["venue"]["to"] == "Barton Hall, Cornell University"
    assert m["backtag"]._apply(p, m["config"].load()) is True
    new = lib / "Grateful Dead" / "1977" / "gd1977-05-08 Barton Hall, Cornell University, Ithaca, NY [SBD 012345]"
    assert new.is_dir() and not show.exists()
    assert {FLAC(f)["ALBUM"][0] for f in new.glob("*.flac")} == \
        {"1977-05-08 Barton Hall, Cornell University, Ithaca, NY [SBD 012345]"}
    row = m["database"].get_show_by_filed(str(new))
    assert row and json.loads(row["meta"])["venue"] == "Barton Hall, Cornell University"


def test_name_style_folders_keep_their_artist_prefix(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    show = _show(lib, ["Minglewood Blues", "Loser", "El Paso"], prefix="Grateful Dead ", year=False)
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    p = m["backtag"]._propose(show, m["config"].load())
    assert m["backtag"]._apply(p, m["config"].load())
    assert (lib / "Grateful Dead" /
            "Grateful Dead 1977-05-08 Barton Hall, Cornell University, Ithaca, NY [SBD 012345]").is_dir()


def test_folder_not_ending_in_its_album_is_retagged_but_not_renamed(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    show = _show(lib, ["Minglewood Blues", "Loser", "El Paso"])
    odd = show.parent / "my own folder name"
    show.rename(odd)
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    p = m["backtag"]._propose(odd, m["config"].load())
    assert m["backtag"]._apply(p, m["config"].load())
    assert odd.is_dir()
    assert "Cornell" in FLAC(next(odd.glob("*.flac")))["ALBUM"][0]


def test_venue_mismatch_goes_to_review(tmp_path):
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Minglewood Blues", "Loser", "El Paso"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m, venue="Madison Square Garden",
                                                               city="New York")
    p = m["backtag"]._propose(show, m["config"].load())
    assert p["verdict"] == "brig" and "venue" in p["reason"]


def test_a_failed_write_is_reported_and_folder_left_alone(tmp_path, monkeypatch):
    m = _app(tmp_path)
    show = _show(tmp_path / "music", ["Minglewood Blues", "Loser", "El Paso"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    p = m["backtag"]._propose(show, m["config"].load())

    def boom(f, album, location):
        raise OSError("disk full")
    monkeypatch.setattr(m["backtag"], "_write_album", boom)
    assert m["backtag"]._apply(p, m["config"].load()) is False
    assert show.is_dir()                                   # not renamed
    logs = [r["detail"] for r in m["database"].recent_log(50)]
    assert any("failed" in x and "disk full" in x for x in logs)


def test_undo_restores_tags_and_folder(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    show = _show(lib, ["Track 01", "Track 02", "Track 03"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    p = m["backtag"]._propose(show, m["config"].load())
    assert m["backtag"]._apply(p, m["config"].load())
    assert not show.exists()
    batch = m["fileops"].list_batches(5)[0]
    m["fileops"].undo(batch["id"])
    assert show.is_dir()
    assert _titles(show) == ["Track 01", "Track 02", "Track 03"]
    assert {FLAC(f)["ALBUM"][0] for f in show.glob("*.flac")} == {ALBUM}


def test_global_dry_run_forces_a_preview(tmp_path):
    m = _app(tmp_path)
    m["config"].save({"safety": {"dry_run": True}})
    show = _show(tmp_path / "music", ["Track 01", "Track 02", "Track 03"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    r = m["backtag"].run_backtag(dry_run=False)
    assert r["dry_run"] and r["forced_preview"] and r["applied"] == 0
    assert len(r["proposals"]) == 1 and show.is_dir()
    assert _titles(show) == ["Track 01", "Track 02", "Track 03"]


def test_full_run_applies_and_reports(tmp_path):
    m = _app(tmp_path)
    _show(tmp_path / "music", ["Track 01", "Track 02", "Track 03"])
    m["backtag"].sources.fetch_setlistfm = lambda *a: _setlist(m)
    r = m["backtag"].run_backtag(dry_run=False)
    assert (r["total"], r["applied"], r["errors"]) == (1, 1, 0)


def test_needs_a_setlistfm_key(tmp_path):
    m = _app(tmp_path)
    m["config"].save({"sources": {"setlistfm_api_key": ""}})
    assert "API key" in m["backtag"].run_backtag()["error"]


@pytest.mark.parametrize("name,new,want", [
    ("gd1977-05-08 A, B, NY [SBD 12345]", "1977-05-08 C, B, NY [SBD 012345]",
     "gd1977-05-08 C, B, NY [SBD 012345]"),
    ("Grateful Dead 1977-05-08 A [AUD]", "1977-05-08 C/D [AUD]",
     "Grateful Dead 1977-05-08 CD [AUD]"),
    ("something else", "1977-05-08 C [AUD]", ""),
])
def test_renamed_folder(name, new, want):
    from app.backtag import renamed_folder
    assert renamed_folder(name, "1977-05-08", new) == want

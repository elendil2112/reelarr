"""Track titles vs files: tuning, crowd and break tracks are set aside before
the setlist is paired with the files, so titles don't shift by one."""
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import align  # noqa: E402


def info(*files):
    """files: (name, seconds[, own label]) — what describe() would read."""
    out = []
    for f in files:
        name, secs, label = (f + ("",))[:3]
        p = Path("/show") / name
        out.append({"path": p, "name": name, "seconds": secs,
                    "label": label or align.own_label(p), "disc": align.disc_of(p)})
    return out


def setlist(*titles, encore_from=None, sets=None):
    out = []
    for i, t in enumerate(titles, 1):
        d = {"num": i, "title": t, "disc": 99 if encore_from and i >= encore_from else 1}
        if sets:
            d["set"] = sets[i - 1]
        out.append(d)
    return out


def titles(p):
    return [e["title"] for e in p["entries"]]


SONGS = setlist("Bertha", "Loser", "El Paso", "Sugaree")


def test_a_short_tuning_track_at_the_start():
    p = align.plan([], SONGS, info=info(("d1t01.flac", 41), ("d1t02.flac", 380), ("d1t03.flac", 420),
                                        ("d1t04.flac", 270), ("d1t05.flac", 610)))
    assert p["ok"] and p["how"] == "short"
    assert titles(p) == ["Tuning", "Bertha", "Loser", "El Paso", "Sugaree"]
    assert p["extras"] == ["d1t01.flac"]


def test_crowd_at_the_end():
    p = align.plan([], SONGS, info=info(("t01.flac", 380), ("t02.flac", 420), ("t03.flac", 270),
                                        ("t04.flac", 610), ("t05.flac", 35)))
    assert p["ok"] and titles(p) == ["Bertha", "Loser", "El Paso", "Sugaree", "Crowd"]


def test_named_tracks_keep_their_own_label_wherever_they_are():
    p = align.plan([], SONGS, info=info(("01 Bertha.flac", 380), ("02 Tuning & Banter.flac", 150),
                                        ("03 Loser.flac", 420), ("04 El Paso.flac", 270),
                                        ("05 Sugaree.flac", 610)))
    assert p["ok"] and p["how"] == "named"
    assert titles(p) == ["Bertha", "Tuning & Banter", "Loser", "El Paso", "Sugaree"]


def test_tuning_and_crowd_both_named_from_tags():
    p = align.plan([], SONGS, info=info(("a.flac", 200, "tuning"), ("b.flac", 380), ("c.flac", 420),
                                        ("d.flac", 270), ("e.flac", 610), ("f.flac", 60, "Crowd")))
    assert p["ok"] and titles(p) == ["Tuning", "Bertha", "Loser", "El Paso", "Sugaree", "Crowd"]


def test_set_break_and_encore_break():
    tracks = setlist("Bertha", "Loser", "Playing in the Band", "Uncle John's Band", "U.S. Blues",
                     encore_from=5, sets=[1, 1, 2, 2, 3])
    p = align.plan([], tracks, info=info(("gd77d1t01.flac", 380), ("gd77d1t02.flac", 420),
                                         ("gd77d1t03.flac", 40),                     # end of disc 1
                                         ("gd77d2t01.flac", 900), ("gd77d2t02.flac", 600),
                                         ("gd77d2t03.flac", 30),                     # end of disc 2
                                         ("gd77d3t01.flac", 330)))
    assert p["ok"], p["note"]
    assert titles(p) == ["Bertha", "Loser", "Set Break", "Playing in the Band", "Uncle John's Band",
                         "Encore Break", "U.S. Blues"]


def test_two_short_tracks_for_one_extra_is_held():
    p = align.plan([], SONGS, info=info(("t01.flac", 25), ("t02.flac", 380), ("t03.flac", 420),
                                        ("t04.flac", 270), ("t05.flac", 610), ("t06.flac", 30)))
    assert p["ok"] and titles(p) == ["Tuning", "Bertha", "Loser", "El Paso", "Sugaree", "Crowd"]  # 2 extra, 2 short
    p = align.plan([], SONGS, info=info(("t01.flac", 25), ("t02.flac", 380), ("t03.flac", 420),
                                        ("t04.flac", 610), ("t05.flac", 30)))
    assert not p["ok"] and "more than one short track" in p["note"]


def test_no_short_track_means_no_guess():
    p = align.plan([], SONGS, info=info(*[(f"t0{i}.flac", 300) for i in range(1, 6)]))
    assert not p["ok"] and "couldn't tell which file isn’t a song" in p["note"]


def test_a_song_called_intro_is_a_song():
    tracks = setlist("Intro", "Bertha", "Loser")
    p = align.plan([], tracks, info=info(("t01 Intro.flac", 70), ("t02 Bertha.flac", 380),
                                         ("t03 Loser.flac", 420), ("t04 Crowd.flac", 30)))
    assert p["ok"] and titles(p) == ["Intro", "Bertha", "Loser", "Crowd"]


def test_counts_that_match_pair_in_order():
    p = align.plan([], SONGS, info=info(("t01.flac", 30), ("t02.flac", 380), ("t03.flac", 420),
                                        ("t04.flac", 270)))
    assert p["ok"] and p["how"] == "count" and titles(p)[0] == "Bertha"


def test_fewer_files_than_songs_falls_back_to_real_track_numbers():
    p = align.plan([], SONGS, info=info(("gd77-05-08d1t02.flac", 300), ("gd77-05-08d1t04.flac", 300)))
    assert not p["ok"] and titles(p) == ["Loser", "Sugaree"]


def test_the_number_fallback_never_reads_a_disc_or_a_date():
    assert align.track_number(Path("gd77-05-08d1t05.flac")) == 5
    assert align.track_number(Path("gd77-05-08d2t01.flac")) == 1
    assert align.track_number(Path("05 Loser.flac")) == 5
    assert align.track_number(Path("Loser - 05.flac")) == 5
    assert align.track_number(Path("gd1977-05-08.flac")) is None
    assert align.disc_of(Path("gd77-05-08d2t01.flac")) == 2


def test_your_choice_in_review_wins():
    i = info(("t01.flac", 30), ("t02.flac", 380), ("t03.flac", 420), ("t04.flac", 270),
             ("t05.flac", 610))
    p = align.plan([], SONGS, extras=["t05.flac"], info=i)
    assert p["ok"] and p["how"] == "yours" and titles(p) == ["Bertha", "Loser", "El Paso", "Sugaree", "Crowd"]
    p = align.plan([], SONGS, extras=[""], info=i)           # "none of these" — counts don't add up
    assert not p["ok"] and "mark or unmark" in p["note"]


@pytest.mark.parametrize("label,want", [
    ("Tuning", True), ("tuning/banter", True), ("Crowd & Tuning", True), ("Band Intros", True),
    ("Set Break", True), ("Encore Break", True), ("- Crowd -", True), ("Soundcheck", True),
    ("Space", False), ("Drums", False), ("Jam", False), ("Introduction to the Blues", False),
    ("Tuning Fork Blues", False), ("Crowd Pleaser", False), ("", False),
])
def test_what_counts_as_not_a_song(label, want):
    assert align.is_filler(label) is want


# ── through the pipeline, with real audio files ──────────────────────────────

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _show(tmp_path, secs, info_text, hold=False):
    from test_safety import fresh_app, make_audio, offline
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False},
                                     "filing": {"hold_uncertain_titles": hold}}))
    m["database"].init()
    show = tmp_path / "watch" / "gd1977-05-08.sbd.miller"
    for i, s in enumerate(secs, 1):
        make_audio(show / f"gd77-05-08d1t{i:02d}.flac", seconds=s, freq=300 + 40 * i)
    (show / "gd77-05-08.txt").write_text(
        "Grateful Dead\n1977-05-08\nBarton Hall, Cornell University\nIthaca, NY\nSource: SBD\n\n" + info_text)
    return m, show


SETLIST_TXT = "d1t01. New Minglewood Blues\nd1t02. Loser\nd1t03. El Paso\nd1t04. They Love Each Other\n"


def _titles(folder):
    from mutagen.flac import FLAC
    return [(FLAC(f).get("TITLE") or [""])[0] for f in sorted(Path(folder).rglob("*.flac"))]


@needs_ffmpeg
def test_filed_show_with_a_tuning_track(tmp_path):
    m, show = _show(tmp_path, [20, 200, 190, 210, 200], SETLIST_TXT)
    r = m["pipeline"].process_show(show)
    assert _titles(r["dest"]) == ["Tuning", "New Minglewood Blues", "Loser", "El Paso",
                                  "They Love Each Other"]
    logged = [x["detail"] for x in m["database"]._conn().execute("SELECT detail FROM log WHERE event='titles'")]
    assert any("d1t01.flac (0:20) → Tuning" in x for x in logged)


@needs_ffmpeg
def test_ambiguous_show_is_held_and_fixed_in_review(tmp_path):
    from test_auth import client, setup_admin
    m, show = _show(tmp_path, [20, 200, 190, 210, 25], SETLIST_TXT, hold=True)
    c = client(tmp_path)          # same app, a fresh import — reuse the folders it set up
    setup_admin(c)
    importlib.import_module("app.config").set_key(["filing", "hold_uncertain_titles"], True)
    importlib.import_module("app.config").set_key(["sources"], {
        **importlib.import_module("app.config").load()["sources"],
        "use_internet_archive": False, "use_setlistfm": False, "use_musicbrainz_genre": False})
    importlib.import_module("app.config").set_key(["safety", "dry_run"], False)
    r = importlib.import_module("app.pipeline").process_show(show)
    assert r["status"] == "review"
    row = importlib.import_module("app.database")._conn().execute(
        "SELECT id, notes FROM shows ORDER BY id DESC LIMIT 1").fetchone()
    assert "Titles need a look" in row["notes"] and "more than one short track" in row["notes"]
    t = c.post(f"/api/show/{row['id']}/tracks", json={}).json()
    assert not t["ok"] and len(t["entries"]) == 5
    t = c.post(f"/api/show/{row['id']}/tracks", json={"extras": ["", "gd77-05-08d1t01.flac"]}).json()
    assert t["ok"] and [e["title"] for e in t["entries"]][0] == "Tuning"
    bad = c.post(f"/api/show/{row['id']}/approve",
                 json={"extras": ["", "gd77-05-08d1t01.flac", "gd77-05-08d1t05.flac"]})
    assert bad.status_code == 400 and "3 files left as songs" in bad.json()["error"]
    a = c.post(f"/api/show/{row['id']}/approve", json={"extras": ["", "gd77-05-08d1t01.flac"]}).json()
    assert a.get("status") == "filed", a
    assert _titles(a["dest"]) == ["Tuning", "New Minglewood Blues", "Loser", "El Paso",
                                  "They Love Each Other"]
    stored = json.loads(importlib.import_module("app.database").get_show(row["id"])["meta"])
    assert stored["extras"] == ["", "gd77-05-08d1t01.flac"]


@needs_ffmpeg
def test_filler_titles_are_not_learned_as_songs(tmp_path):
    m, show = _show(tmp_path, [20, 200, 190, 210, 200], SETLIST_TXT)
    m["pipeline"].process_show(show)
    li = importlib.import_module("app.library_index")
    assert li.song_known("Grateful Dead", "Loser")
    assert not li.song_known("Grateful Dead", "Tuning")


@needs_ffmpeg
def test_archive_org_titles_win_when_they_cover_every_file(tmp_path, monkeypatch):
    m, show = _show(tmp_path, [20, 200, 190, 210, 200], SETLIST_TXT)
    cfg = importlib.import_module("app.config")
    cfg.set_key(["sources", "use_internet_archive"], True)
    src = importlib.import_module("app.sources")
    md = importlib.import_module("app.metadata")
    ia = md.ShowMeta(artist="Grateful Dead", date="1977-05-08", tracks=[
        {"num": i, "title": t, "disc": 1} for i, t in enumerate(
            ["Crowd & Tuning", "New Minglewood Blues", "Loser", "El Paso", "They Love Each Other"], 1)])
    monkeypatch.setattr(src, "is_ia_identifier", lambda name: True)
    monkeypatch.setattr(src, "fetch_ia", lambda ident: ia)
    r = m["pipeline"].process_show(show)
    assert _titles(r["dest"])[:2] == ["Crowd & Tuning", "New Minglewood Blues"]

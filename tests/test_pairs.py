"""Early and late shows of one night, filed as one show: disc 1 and disc 2."""
import importlib
import json
import sys
from pathlib import Path

import pytest
from mutagen.flac import FLAC

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app, make_audio, offline, tree  # noqa: E402

pytestmark = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")

EARLY = ["Easy to Slip", "Bombs Away", "This Time Forever"]
LATE = ["intro", "New New Minglewood Blues", "C.C. Rider", "Easy To Slip"]


def _bwb(watch: Path, early_name="bwb1978-03-25.sbd.tinydancer.82964.sbeok.flac",
         late_name="Bob Weir Band 1978-03-25 San Francisco,CA.late.ev.re10.cm.grner1.flac1644",
         early_says="Early Show"):
    early, late = watch / early_name, watch / late_name
    for i, _t in enumerate(EARLY, 1):
        make_audio(early / f"bwb1978-03-25d1t{i:02d}.flac", seconds=1, freq=300 + i * 20)
    for i, t in enumerate(LATE, 1):
        make_audio(late / f"{i:02d} {t}.flac", seconds=1, freq=500 + i * 20)
    (early / "bwb1978-03-25.txt").write_text(
        f"Bob Weir Band\n1978-03-25\n{early_says}\nThe Old Waldorf\nSan Francisco, CA\n\n"
        "Source: SBD > Reel > DAT\nTransfer: TinyDancer\n\n"
        + "\n".join(f"{i}. {t}" for i, t in enumerate(EARLY, 1)))
    (late / "info.txt").write_text(
        "Bob Weir Band\n1978-03-25\nThe Old Waldorf\nSan Francisco, CA\n\nSource: AUD\nTaper: Miller\n\n"
        + "\n".join(f"{i:02d}. {t}" for i, t in enumerate(LATE, 1)))
    return early, late


def _app(tmp_path, **settings):
    m = fresh_app(tmp_path, offline({"filing": {"auto_file_threshold": 50}, **settings}))
    m["database"].init()
    importlib.import_module("app.setup").mark_done()
    return m


ALBUM = "1978-03-25 The Old Waldorf, San Francisco, CA [SBD 082964 TinyDancer & AUD Miller]"


def test_part_detection_and_artist_matching(tmp_path):
    fresh_app(tmp_path)
    pairs = importlib.import_module("app.pairs")
    for name, want in [("gd1977-05-08.early.sbd", "early"), ("Late Show 1978", "late"),
                       ("phish1997.lateshow.flac", "late"), ("x.earlyset", "early"),
                       ("Billy Strings 2023-04-21", ""), ("Elated Fans 2020", ""),
                       ("early and late 1978", "")]:
        d = tmp_path / name
        d.mkdir()
        assert pairs.part_of(d) == want, name
    d = tmp_path / "plain"
    d.mkdir()
    (d / "info.txt").write_text("Bob Weir Band\n1978-03-25 — late show\n")
    assert pairs.part_of(d) == "late"
    (d / "info.txt").write_text("both the early show and the late show\n")
    assert pairs.part_of(d) == ""
    assert pairs.same_artist("bwb", "Bob Weir Band") and pairs.same_artist("Bob Weir Band", "bob weir band")
    assert not pairs.same_artist("Bob Weir", "Bob Weir Band") and not pairs.same_artist("", "x")


def test_two_folders_become_one_show_with_two_discs(tmp_path):
    _app(tmp_path, safety={"dry_run": False})
    _bwb(tmp_path / "watch")
    r = importlib.import_module("app.watcher").scan(force=True)
    assert r["filed"] == 1 and r["review"] == 0, r
    shows = [p for p in (tmp_path / "music").rglob("*") if p.is_dir() and "[" in p.name]
    assert len(shows) == 1 and shows[0].name.endswith("[SBD 082964 TinyDancer & AUD Miller]")
    subs = sorted(p.name for p in shows[0].iterdir() if p.is_dir())
    assert len(subs) == 2                                    # both originals kept, as they were
    tags = {}
    for f in (p for p in shows[0].rglob("*.flac") if p.is_file()):
        a = FLAC(f)
        assert a["ALBUM"] == [ALBUM]
        tags[(a["DISCNUMBER"][0], int(a["TRACKNUMBER"][0]))] = a["TITLE"][0]
    assert tags[("1", 1)] == "Easy to Slip" and tags[("1", 3)] == "This Time Forever"
    assert tags[("2", 1)] == "intro" and tags[("2", 4)] == "Easy To Slip"
    assert not [p for p in (tmp_path / "watch").iterdir() if not p.name.startswith(".")]


def test_dry_run_plans_the_combination_and_moves_nothing(tmp_path):
    m = _app(tmp_path, safety={"dry_run": True})
    early, late = _bwb(tmp_path / "watch")
    before = tree(tmp_path / "watch")
    r = importlib.import_module("app.watcher").scan(force=True)
    assert r["planned"] == 1, r
    assert tree(tmp_path / "watch") == before
    rows = {Path(x["current_path"]).name: x for x in m["database"].list_shows(limit=10)}
    assert "would combine" in rows[early.name]["notes"] and ALBUM.split("[")[0].strip() in rows[early.name]["plan"]
    assert rows[late.name]["status"] == "planned" and "late show" in rows[late.name]["notes"]
    assert json.loads(rows[early.name]["plan"])["dest"].endswith("[SBD 082964 TinyDancer & AUD Miller]")


def test_only_a_clear_pair_is_combined(tmp_path):
    _app(tmp_path, safety={"dry_run": False})
    pairs = importlib.import_module("app.pairs")
    cfg = importlib.import_module("app.config").load()
    early, late = _bwb(tmp_path / "watch", early_says="")      # the early one says nothing
    assert pairs.match([early, late], cfg["artists"]["aliases"]) == []
    other = tmp_path / "watch" / "bwb1978-03-25.late.aud2"
    make_audio(other / "t01.flac", seconds=1)
    (other / "i.txt").write_text("Bob Weir Band\n1978-03-25\n")
    (early / "bwb1978-03-25.txt").write_text("Bob Weir Band\n1978-03-25\nearly show\n")
    assert pairs.match([early, late, other], cfg["artists"]["aliases"]) == []   # two lates: not guessing


def test_a_dropped_folder_holding_both_is_combined_too(tmp_path):
    _app(tmp_path, safety={"dry_run": False})
    holder = tmp_path / "watch" / "bwb1978-03-25 both shows"
    holder.mkdir()
    _bwb(holder)
    r = importlib.import_module("app.watcher").scan(force=True)
    assert r["filed"] == 1, r
    f = next((tmp_path / "music").rglob("01 intro.flac"))
    assert FLAC(f)["DISCNUMBER"] == ["2"] and FLAC(f)["ALBUM"] == [ALBUM]


def test_review_then_approve_keeps_the_discs(tmp_path):
    m = _app(tmp_path, safety={"dry_run": False}, filing={"auto_file_threshold": 101})
    _bwb(tmp_path / "watch")
    importlib.import_module("app.watcher").scan(force=True)
    row = [x for x in m["database"].list_shows(limit=10) if x["status"] == "review"][0]
    assert json.loads(row["meta"])["bracket_override"] == "SBD 082964 TinyDancer & AUD Miller"
    r = m["pipeline"].approve_show(row["id"], {"venue": "Old Waldorf"})
    assert r.get("status") == "filed" or r.get("dest"), r
    f = next((tmp_path / "music").rglob("bwb1978-03-25d1t02.flac"))
    a = FLAC(f)
    assert a["DISCNUMBER"] == ["1"] and a["TRACKNUMBER"] == ["2"]
    assert a["ALBUM"] == ["1978-03-25 Old Waldorf, San Francisco, CA [SBD 082964 TinyDancer & AUD Miller]"]


def test_band_names_and_passing_mentions_dont_count(tmp_path):
    fresh_app(tmp_path)
    pairs = importlib.import_module("app.pairs")
    for name in ("The Early November 2020-05-01", "Late Night Radio 2024-01-01", "Johnny Early 1999-01-01",
                 "Too Late Show 2001-01-01"):
        d = tmp_path / name
        d.mkdir()
        assert pairs.part_of(d) == "", name
    d = tmp_path / "Late Night Radio 2024-01-01 early show"
    d.mkdir()
    assert pairs.part_of(d) == "early"
    t = tmp_path / "t"
    t.mkdir()
    for text, want in [("The early show was not taped; this is the second set", ""),
                       ("Late Show", "late"), ("1978-03-25 (early show)", "early"),
                       ("1978-03-25 — late show", "late"), ("Early Show - 7:30pm", "early")]:
        (t / "i.txt").write_text(text)
        assert pairs.part_of(t) == want, text


def test_undo_separates_them_and_they_stay_apart(tmp_path):
    m = _app(tmp_path, safety={"dry_run": False}, filing={"auto_file_threshold": 101})
    early, late = _bwb(tmp_path / "watch")
    w = importlib.import_module("app.watcher")
    w.scan(force=True)                                        # combined, into Review
    pair_batch = [b for b in m["fileops"].list_batches() if b["kind"] == "pair"][0]
    m["fileops"].undo(pair_batch["id"])
    assert early.is_dir() and late.is_dir()
    rows = {r["current_path"]: r for r in m["database"].list_shows(limit=10)}
    assert rows[str(early)]["status"] == "review" and rows[str(late)]["status"] == "review"
    assert "Separated by Undo" in rows[str(early)]["notes"]
    combined = [r for r in m["database"].list_shows(limit=10) if "early + late" in r["folder_name"]][0]
    assert combined["status"] == "rejected"
    w.scan(force=True)                                        # not paired again behind your back
    assert early.is_dir() and late.is_dir()


def test_a_failed_move_is_contained(tmp_path, monkeypatch):
    m = _app(tmp_path, safety={"dry_run": False})
    early, late = _bwb(tmp_path / "watch")
    fileops = m["fileops"]
    real = fileops.Batch.move
    calls = []

    def flaky(self, src, dst, *a, **k):
        calls.append(src)
        if Path(src) == late:
            raise PermissionError("locked")
        return real(self, src, dst, *a, **k)
    monkeypatch.setattr(fileops.Batch, "move", flaky)
    r = importlib.import_module("app.watcher").scan(force=True)
    assert r["error"] == 1                                    # the sweep finished; nothing escaped
    assert early.is_dir() and late.is_dir()                   # the early folder was put back


def test_a_lone_half_waits_for_its_partner(tmp_path):
    _app(tmp_path, safety={"dry_run": False})
    early, late = _bwb(tmp_path / "watch")
    import shutil
    shutil.rmtree(late)                                       # only the early show has arrived
    w = importlib.import_module("app.watcher")
    r = w.scan(force=True)
    assert r["waiting"] == 1 and early.is_dir()
    importlib.import_module("app.config").save({"watcher": {"pair_wait_minutes": 0}})
    r = w.scan(force=True)
    assert r["filed"] + r["review"] == 1 and not early.exists()


def test_editing_the_source_in_review_replaces_the_joined_bracket(tmp_path):
    m = _app(tmp_path, safety={"dry_run": False}, filing={"auto_file_threshold": 101})
    _bwb(tmp_path / "watch")
    importlib.import_module("app.watcher").scan(force=True)
    row = [x for x in m["database"].list_shows(limit=10) if x["status"] == "review"][0]
    m["pipeline"].approve_show(row["id"], {"source_type": "MTX", "recorder": "Joe", "shnid": "", "official": ""})
    f = next(p for p in (tmp_path / "music").rglob("*.flac") if p.is_file())
    assert FLAC(f)["ALBUM"][0].endswith("[MTX Joe]")

"""Regressions from the October 2026 audit report (1,311 shows). Each test
names the real case it comes from."""
import importlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app, make_audio, offline  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _app(tmp_path, **settings):
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}, **settings}))
    m["database"].init()
    return m


def _tagged_show(folder: Path, artist: str, album: str, date: str, n=2):
    from mutagen.flac import FLAC
    for i in range(1, n + 1):
        f = folder / f"t{i:02d}.flac"
        make_audio(f, seconds=1, freq=300 + 40 * i)
        a = FLAC(f)
        a["ARTIST"], a["ALBUMARTIST"], a["ALBUM"], a["DATE"], a["TITLE"] = artist, artist, album, date, f"Song {i}"
        a.save()
    return folder


def _filed(m, folder_name, filed_path, meta):
    db = m["database"]
    sid = db.upsert_show(folder_name, folder_name=folder_name, status="filed", current_path=str(filed_path),
                         filed_path=str(filed_path), meta=meta)
    return sid


# ── A. the learning tool ─────────────────────────────────────────────────────

@needs_ffmpeg
def test_a_deleted_show_is_not_bound_to_another_artists_show_of_that_date(tmp_path):
    """toad1992-03-08 was attached to gd1992-03-08 and taught 'Toad → Grateful Dead'."""
    m = _app(tmp_path)
    lib = tmp_path / "music"
    gd = _tagged_show(lib / "Grateful Dead" / "gd1992" / "gd1992-03-08.141627.Capital Centre.Landover,MD.sbd",
                      "Grateful Dead", "1992-03-08 Capital Centre, Landover, MD [SBD]", "1992-03-08")
    gone = lib / "Toad the Wet Sprocket" / "toad1992-03-08 Mountain Stage, Charleston, WV [AUD]"   # deleted
    _filed(m, "toad1992-03-08", gone, {"artist": "Toad the Wet Sprocket", "date": "1992-03-08",
                                       "venue": "Mountain Stage", "city": "Charleston", "state": "WV"})
    r = importlib.import_module("app.audit").learn_from_corrections()
    assert r["missing_from_disk"] == 1 and r["artists_learned"] == {}
    li = importlib.import_module("app.library_index")
    assert li.correction_get("artist", "Toad the Wet Sprocket") is None
    row = m["database"]._conn().execute("SELECT * FROM shows WHERE folder_name='toad1992-03-08'").fetchone()
    assert row["status"] == "rejected" and "Grateful Dead" not in row["meta"]
    assert gd.exists()


@needs_ffmpeg
def test_a_show_you_moved_is_still_found_and_respellings_are_learned(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    name = "motorhead1981-03-27 Hammersmith Odeon, London, United Kingdom [AUD]"
    _tagged_show(lib / "Motörhead" / name, "Motörhead", "1981-03-27 Hammersmith Odeon, London, United Kingdom [AUD]",
                 "1981-03-27")
    _filed(m, "mh81", lib / "Motorhead" / name, {"artist": "Motorhead", "date": "1981-03-27",
                                                "venue": "Hammersmith Odeon"})
    r = importlib.import_module("app.audit").learn_from_corrections()
    assert r["artists_learned"] == {"Motorhead": "Motörhead"}


@needs_ffmpeg
def test_moving_a_show_to_an_unrelated_artist_is_not_a_rule(tmp_path):
    m = _app(tmp_path)
    lib = tmp_path / "music"
    name = "jg1975-03-17 Seattle Center Coliseum, Seattle, WA [SBD]"
    _tagged_show(lib / "Led Zeppelin" / name, "Led Zeppelin", "1975-03-17 Seattle Center Coliseum, Seattle, WA [SBD]",
                 "1975-03-17")
    _filed(m, "jg75", lib / "Jerry Garcia" / "jg1975" / name, {"artist": "Jerry Garcia", "date": "1975-03-17"})
    r = importlib.import_module("app.audit").learn_from_corrections()
    assert r["artists_learned"] == {} and r["reconciled"] == 1
    assert importlib.import_module("app.library_index").correction_get("artist", "Jerry Garcia") is None


def test_junk_venue_rules_are_refused_and_old_ones_ignored(tmp_path):
    _app(tmp_path)
    li = importlib.import_module("app.library_index")
    junk = {"venue": "The Fillmore", "city": "Benmont Tench - piano, organ, keyboards", "state": "backing vocals"}
    assert li.correction_set("venue", "Fillmore", junk) is False
    li._conn().execute("INSERT INTO corrections VALUES ('venue', 'fillmore', ?)", (json.dumps(junk),))
    assert li.correction_get("venue", "Fillmore") is None
    good = {"venue": "The Fillmore", "city": "San Francisco", "state": "CA"}
    assert li.correction_set("venue", "Fillmore SF", good) and li.correction_get("venue", "Fillmore SF") == good
    assert li.correction_set("venue", "x", {"venue": "75-06-06", "city": "San Rafael", "state": "CA"}) is False


def test_upgrade_removes_bad_rules_and_says_so(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    idx = sqlite3.connect(cfg / "library_index.db")
    idx.executescript(
        "CREATE TABLE corrections (kind TEXT, wrong_norm TEXT, right TEXT, PRIMARY KEY (kind, wrong_norm));"
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT, applied_at REAL, app_version TEXT);"
        "INSERT INTO schema_version VALUES (1, 'baseline', 0, '0.1.0');")
    rows = [("artist", "toadthewetsprocket", json.dumps("Grateful Dead")),
            ("artist", "jerrygarcia", json.dumps("Santana")),
            ("artist", "motorhead", json.dumps("Motörhead")),
            ("artist", "gdead", "Grateful Dead"),
            ("venue", "thefillmore", json.dumps({"venue": "The Fillmore", "city": "Benmont Tench - piano, organ, keyboards",
                                                 "state": "backing vocals"})),
            ("venue", "fillmoresf", json.dumps({"venue": "The Fillmore", "city": "San Francisco", "state": "CA"}))]
    idx.executemany("INSERT INTO corrections VALUES (?,?,?)", rows)
    idx.commit()
    idx.close()
    m = _app(tmp_path)
    left = {r[1] for r in sqlite3.connect(cfg / "library_index.db").execute("SELECT * FROM corrections")}
    assert left == {"motorhead", "gdead", "fillmoresf"}
    logged = [r["detail"] for r in m["database"]._conn().execute("SELECT detail FROM log WHERE event='learned'")]
    assert any("removed 3 learned rule" in x for x in logged)
    assert any("toadthewetsprocket → Grateful Dead" in x for x in logged)
    assert any(p.name.startswith("library_index-v1-") for p in (cfg / "backups").iterdir())


def test_mis_attached_records_are_pointed_out_once(tmp_path):
    m = _app(tmp_path)
    m["database"].kv_set("rebound_check_v1", "")
    _filed(m, "toad1992-03-08", "/lib/Grateful Dead/gd1992/gd1992-03-08.141627.Capital Centre",
           {"artist": "Grateful Dead", "host_artist": "Grateful Dead", "date": "1992-03-08"})
    _filed(m, "nrps1971-12-15", "/lib/Jerry Garcia/jg1971/jg1971-12-15 Hill Auditorium, Ann Arbor, MI [SBD]",
           {"artist": "New Riders of the Purple Sage", "host_artist": "Jerry Garcia", "date": "1971-12-15"})
    _filed(m, "SBD x8bvkya8nmuq5lw1", "/lib/Simon & Garfunkel/simongarfunkel1967-10-06",
           {"artist": "Simon & Garfunkel", "date": "1967-10-06"})
    assert importlib.import_module("app.audit").flag_rebound_records() == 1
    logged = [r["detail"] for r in m["database"]._conn().execute("SELECT detail FROM log WHERE event='check'")]
    assert any(x.startswith("toad1992-03-08 is recorded as Grateful Dead") for x in logged)


# ── B–F. identification, through the whole pipeline ──────────────────────────

def _show(tmp_path, folder, info=None, tags=None, n=3, info_name="info.txt", info_bytes=None):
    from mutagen.flac import FLAC
    show = tmp_path / "watch" / folder
    for i in range(1, n + 1):
        f = show / f"d1t{i:02d}.flac"
        make_audio(f, seconds=1, freq=300 + 40 * i)
        if tags:
            a = FLAC(f)
            for k, v in tags.items():
                a[k] = v.format(i=i) if isinstance(v, str) else v
            a.save()
    if info is not None:
        (show / info_name).write_text(info)
    if info_bytes is not None:
        (show / info_name).write_bytes(info_bytes)
    return show


def _run(m, show):
    r = m["pipeline"].process_show(show)
    row = m["database"]._conn().execute("SELECT * FROM shows ORDER BY id DESC LIMIT 1").fetchone()
    return r, json.loads(row["meta"] or "{}"), row["notes"] or ""


SETLIST = "\n1. Walk on the Ocean\n2. All I Want\n3. Way Away\n"


@needs_ffmpeg
def test_junk_info_lines_dont_become_the_artist(tmp_path):
    """Isaak1991-09-22.Toronto.FM: the info file's artist was 'faults: none'."""
    m = _app(tmp_path)
    show = _show(tmp_path, "Isaak1991-09-22.Toronto.FM.flac16",
                 "faults: none\nToronto, ON  --  Massey Hall\n1991-09-22\nSource: FM" + SETLIST)
    _r, meta, _n = _run(m, show)
    assert meta["artist"] == "Isaak"
    assert (meta["venue"], meta["city"], meta["state"]) == ("Massey Hall", "Toronto", "ON")


@needs_ffmpeg
def test_html_info_file_and_no_reverse_match_when_an_artist_is_named(tmp_path, monkeypatch):
    """toad1992-03-08: an archive.org HTML description; setlist.fm's search by
    date and city must not swap in the Grateful Dead."""
    m = _app(tmp_path, sources={"use_setlistfm": True, "setlistfm_api_key": "k"})
    (tmp_path / "music" / "Toad the Wet Sprocket").mkdir(parents=True)
    (tmp_path / "music" / "Grateful Dead").mkdir(parents=True)
    src = importlib.import_module("app.sources")
    monkeypatch.setattr(src, "fetch_setlistfm", lambda *a, **k: None)
    monkeypatch.setattr(src, "fetch_setlistfm_reverse", lambda *a, **k: pytest.fail("reverse lookup ran"))
    html = ("<div>TOAD THE WET SPROCKET 3/8/92</div><div>MOUNTAIN STAGE, CHARLESTON, WV</div><div><br /></div>"
            "<div>01 Way Away</div><div>02 Nightingale Song</div><div>03 Come Back Down</div>"
            "<div>GEAR USED:</div><div>trade Cassette Tape &gt; Tascam DR-07</div>")
    show = _show(tmp_path, "toad1992-03-08", html)
    _r, meta, _n = _run(m, show)
    assert meta["artist"] == "Toad the Wet Sprocket"
    assert (meta["venue"], meta["city"], meta["state"]) == ("Mountain Stage", "Charleston", "WV")


@needs_ffmpeg
def test_utf16_info_file_is_read(tmp_path):
    """2001-10-06 River of Tears: the artist came out as 'D\\x00O\\x00N…'."""
    m = _app(tmp_path)
    text = "Grateful Dead\n1977-05-08\nBarton Hall, Cornell University\nIthaca, NY\nSource: SBD" + SETLIST
    show = _show(tmp_path, "gd1977-05-08.sbd", info_bytes=text.encode("utf-16"))
    _r, meta, _n = _run(m, show)
    assert meta["artist"] == "Grateful Dead" and meta["venue"] == "Barton Hall, Cornell University"


@needs_ffmpeg
def test_the_folder_date_beats_the_info_files(tmp_path):
    """JEFF BECK 2006-09-06 CLEARWATER was filed as 1989-11-22."""
    m = _app(tmp_path)
    show = _show(tmp_path, "JEFF BECK 2006-09-06 Ruth Eckerd Hall, Clearwater, FL",
                 "Jeff Beck\n1989-11-22\nRuth Eckerd Hall\nClearwater, FL\nSource: AUD" + SETLIST)
    _r, meta, _n = _run(m, show)
    assert meta["date"] == "2006-09-06"


@needs_ffmpeg
def test_transfer_dates_are_not_show_dates(tmp_path):
    m = _app(tmp_path)
    show = _show(tmp_path, "pf.aud.flac16",
                 "Pink Floyd\nTransferred 2024-08-24 by somebody\nThe Omni\nAtlanta, GA\n1977-04-26\nSource: AUD" + SETLIST)
    _r, meta, _n = _run(m, show)
    assert meta["date"] == "1977-04-26"


@needs_ffmpeg
def test_folder_shnid_and_matrix_win(tmp_path):
    """rh1982-10-22.early.179587 was filed as 141855; mmj2011-06-29…matrix as SBD."""
    m = _app(tmp_path)
    show = _show(tmp_path, "rh1982-10-22.early.179587.mtx.knudsen.flac24",
                 "Robert Hunter\n1982-10-22\nThe Stone\nSan Francisco, CA\nSource: SBD\nSHNID: 141855" + SETLIST)
    _r, meta, _n = _run(m, show)
    assert meta["shnid"].lstrip("0") == "179587" and meta["source_type"] == "MTX"


@needs_ffmpeg
def test_a_bad_venue_rule_doesnt_overrule_your_own_album_tag(tmp_path):
    """MMJ 2026-10-11: 'The Fillmore, Benmont Tench - piano, …, backing vocals'."""
    m = _app(tmp_path)
    li = importlib.import_module("app.library_index")
    li._conn().execute("INSERT INTO corrections VALUES ('venue', 'thefillmore', ?)", (json.dumps(
        {"venue": "The Fillmore", "city": "Benmont Tench - piano, organ, keyboards", "state": "backing vocals"}),))
    li._conn().commit()
    album = "2026-10-11 The Fillmore, San Francisco, CA [SBD Nugs 50590]"
    show = _show(tmp_path, album, tags={"ARTIST": "My Morning Jacket", "ALBUM": album, "DATE": "2026-10-11",
                                        "TITLE": "Song {i}"})
    _r, meta, _n = _run(m, show)
    assert (meta["venue"], meta["city"], meta["state"]) == ("The Fillmore", "San Francisco", "CA")


@needs_ffmpeg
@pytest.mark.parametrize("line,want", [
    ("Newport, RI @ Ft. Adams State Park - Newport Jazz Fest - Quad Stage", ("Ft. Adams State Park", "Newport", "RI")),
    ("Recorded live at The Grande Ballroom, Detroit (MI), United States, October 15, 1967",
     ("The Grande Ballroom", "Detroit", "MI")),
])
def test_more_location_layouts(tmp_path, line, want):
    m = _app(tmp_path)
    show = _show(tmp_path, "cream1967-10-15", f"Cream\n1967-10-15\n{line}\nFormat: 2 x CD\nSource: SBD" + SETLIST)
    _r, meta, _n = _run(m, show)
    assert (meta["venue"], meta["city"], meta["state"]) == want


@needs_ffmpeg
def test_placeholder_titles_dont_beat_a_real_setlist(tmp_path):
    """Kingfish 1986-08-14: 31 of 31 tracks kept 'Track NN'."""
    m = _app(tmp_path)
    album = "1986-08-14 The Studio Club, Vail, CO [SBD 108069]"
    show = _show(tmp_path, "kingfish1986-08-14.108069.sbd",
                 "Kingfish\n1986-08-14\nThe Studio Club\nVail, CO\nSource: SBD" + SETLIST,
                 tags={"ARTIST": "Kingfish", "ALBUM": album, "DATE": "1986-08-14", "TITLE": "Track 0{i}"})
    r, _meta, _n = _run(m, show)
    from mutagen.flac import FLAC
    titles = [FLAC(f)["TITLE"][0] for f in sorted(Path(r["dest"]).rglob("*.flac"))]
    assert titles == ["Walk on the Ocean", "All I Want", "Way Away"]


@needs_ffmpeg
def test_an_official_album_is_held_not_filed(tmp_path):
    """'Orgasmatron (Expanded Edition)' became the venue of a Motörhead 'show'."""
    m = _app(tmp_path)
    show = _show(tmp_path, "SBD idob8ywrr44f4xcl", tags={"ARTIST": "Motörhead", "ALBUM": "Orgasmatron (Expanded Edition)",
                                                         "TITLE": "Song {i}"})
    r, meta, notes = _run(m, show)
    assert r["status"] == "review" and not meta.get("venue")
    assert "looks like an official album" in notes


@needs_ffmpeg
def test_a_tag_that_contradicts_the_folder_isnt_trusted(tmp_path):
    """jg1977-12-06.083740 carried an album tag for 2007-03-27 '…, CW Post College'."""
    m = _app(tmp_path)
    album = "2007-03-27 Dome Auditorium, The Dome, CW Post College [SBD 083740 Tetzeli-Edited]"
    show = _show(tmp_path, "jg1977-12-06.083740.jgb.sbd.tetzeli-edited",
                 "Jerry Garcia Band\n1977-12-06\nDome Auditorium, C.W. Post College\nGreenvale, NY\nSource: SBD" + SETLIST,
                 tags={"ARTIST": "Jerry Garcia Band", "ALBUM": album, "DATE": "2007-03-27", "TITLE": "Song {i}"})
    _r, meta, _n = _run(m, show)
    assert meta["date"] == "1977-12-06" and meta["state"] == "NY"


@needs_ffmpeg
def test_folder_prefix_of_another_artist_holds_the_show(tmp_path):
    """toad1992-07-28 was filed as Dave Matthews Band."""
    m = _app(tmp_path)
    for a in ("Toad the Wet Sprocket", "Dave Matthews Band"):
        (tmp_path / "music" / a).mkdir(parents=True)
    show = _show(tmp_path, "toad1992-07-28",
                 "Dave Matthews Band\n1992-07-28\nTrax Nightclub\nCharlottesville, VA\nSource: SBD" + SETLIST)
    r, meta, notes = _run(m, show)
    assert r["status"] == "review" and "toad" in notes.lower()


# ── unit-level checks on the report's strings ────────────────────────────────

@pytest.mark.parametrize("line", [
    "faults: none", "Santa Monica, California.", "Banned tracks removed.",
    "; generated on November 14, 2009, at 19:17:00", "Shared by Axel(a.k.a. RoryGallagher), Friesland, Eurpope",
    "reward for all the work involved in this project.", "'Magic Man'. But hey, it's better than nothing!",
    "Here's the Zappateers' tribute to Frank Zappa ...", "Bean Blossom, Indiana", "Gathering Of The Vibes - Bridgeport, CT",
    "The Monterey Pop Festival was held June 16", "April, 26th, 1977, Atlanta, Georgia, The Omni",
    "Gregg Allman1986-04-15.nak300.NewHaven,CT info"])
def test_not_artists(tmp_path, line):
    _app(tmp_path)
    assert not importlib.import_module("app.metadata").plausible_artist(line)


@pytest.mark.parametrize("name", [
    "R.E.M.", "moe.", "Crosby, Stills, Nash & Young", "Doc Watson, Jack Lawrence & T. Michael Coleman",
    "Earth, Wind & Fire", "Blood, Sweat & Tears", "Dr. Dog", "The 1975", "Enjoy", "Gregg Allman/ Dickey Betts Band"])
def test_still_artists(tmp_path, name):
    _app(tmp_path)
    assert importlib.import_module("app.metadata").plausible_artist(name)


@pytest.mark.parametrize("venue,city,state,want", [
    ("House Of Blues - Chicago", "Chicago", "IL", ("House Of Blues", "Chicago", "IL")),
    ("Dane County Coliseum, Madison, Wisconsin", "Madison", "WI", ("Dane County Coliseum", "Madison", "WI")),
    ("75-06-06", "San Rafael", "CA", ("", "San Rafael", "CA")),
    ("06/14/08 (Sat)  Boulton Center For The Arts", "Bay Shore", "NY", ("Boulton Center For The Arts", "Bay Shore", "NY")),
    ("175488.John Cipollina's Home Studio", "Mill Valley", "CA", ("John Cipollina's Home Studio", "Mill Valley", "CA")),
    ("SBD", "Pentaluma", "CA", ("", "Pentaluma", "CA")),
    ("THE PAGEANT THEATRE", "ST. LOUIS", "MO", ("The Pageant Theatre", "St. Louis", "MO")),
    ("WXRT 93.1 FM", "CHICAGO", "IL", ("WXRT 93.1 FM", "Chicago", "IL")),
    ("Fillmore East, NYC", "New York", "NY", ("Fillmore East", "New York", "NY")),
    ("Abbey Pub", "Chicago", "Chicago", ("Abbey Pub", "Chicago", "")),
    ("Otis Spann Memorial Field", "Ann Arbor", "MI rm", ("Otis Spann Memorial Field", "Ann Arbor", "MI")),
    ("Music Hall Kansas City", "Kansas City", "MO", ("Music Hall Kansas City", "Kansas City", "MO")),
    ("The Matrix", "San Francisco", "CA", ("The Matrix", "San Francisco", "CA")),
])
def test_location_tidying(tmp_path, venue, city, state, want):
    _app(tmp_path)
    md = importlib.import_module("app.metadata")
    meta = md.ShowMeta(venue=venue, city=city, state=state)
    md.sanitize_location(meta)
    assert (meta.venue, meta.city, meta.state) == want


@pytest.mark.parametrize("city,state,want", [
    ("Boston", "Boston", "MA"), ("Chicago", "Chicago", "IL"), ("Werchter", "BE.VB", "Belgium"),
    ("Basel", "BS", "Switzerland"), ("Dover", "DE", "DE"), ("Nowhereville", "TN", "TN")])
def test_state_resolution(tmp_path, city, state, want):
    _app(tmp_path)
    assert importlib.import_module("app.places").resolve(city, state) == want


@pytest.mark.parametrize("folder,want", [
    ("Isaak1991-09-22.Toronto.FM.flac16", ("Isaak", "1991-09-22", "", "Toronto", "", "")),
    ("Herbie Hancock 1972-03-25 De Doelen.Rotterdam,NL.fm.ldb.flac1644", ("Herbie Hancock", "1972-03-25", "De Doelen", "Rotterdam", "NL", "")),
    ("NRPS 1978-02-09.139205.Bodega,CA.sbd.spitalny.flac1644", ("Nrps", "1978-02-09", "", "Bodega", "CA", "139205")),
    ("Frank.Zappa.1988.05.01.Stockholm.SE.146.28.MSC.flac16", ("Frank Zappa", "1988-05-01", "", "Stockholm", "SE", "")),
    ("EmmylouHarris.1992-07-26.Ellicottville.SBD.flac16", ("Emmylou Harris", "1992-07-26", "", "", "", "")),
    ("DelMcCoury2010-07-16Nak300CP4.Flac2496", ("Del McCoury", "2010-07-16", "", "", "", "")),
    ("MMW 2012-12-16.127164.Blue Note Jazz Club, New York, NY.Early.Edirol",
     ("Mmw", "2012-12-16", "Blue Note Jazz Club", "New York", "NY", "127164")),
    ("RHCP1989-10-08 - Minneapolis, MN - First Avenue Club (SBD)", ("Rhcp", "1989-10-08", "First Avenue Club", "Minneapolis", "MN", "")),
    ("R.E.M. - 2001-05-07 Studio 105, Paris, France (FM, Black Session)", ("R.E.M", "2001-05-07", "Studio 105", "Paris", "France", "")),
])
def test_folder_names(tmp_path, folder, want):
    _app(tmp_path)
    md = importlib.import_module("app.metadata")
    m = md.parse_folder_name(folder, {})
    md.sanitize_location(m)
    assert (m.artist, m.date, m.venue, m.city, m.state, m.shnid) == want

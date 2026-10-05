"""Places: US states and Canadian provinces as codes; everywhere else the
country, in full, where the state would go."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_safety import fresh_app  # noqa: E402


@pytest.fixture
def places(tmp_path):
    fresh_app(tmp_path)
    importlib.import_module("app.database").init()
    return importlib.import_module("app.places")


@pytest.mark.parametrize("city,state,want", [
    # abroad → the country, spelled out
    ("Amsterdam", "NL", "Netherlands"),
    ("Amsterdam", "Holland", "Netherlands"),
    ("Berlin", "DE", "Germany"),
    ("Munich", "Bavaria", "Germany"),
    ("Barcelona", "Catalonia", "Spain"),
    ("London", "England", "United Kingdom"),
    ("London", "UK", "United Kingdom"),
    ("Mumbai", "IN", "India"),
    ("Tokyo", "JP", "Japan"),
    ("Koeln", "Deutschland", "Germany"),
    # the US and Canada keep their codes — even when the code is also a country's
    ("Dover", "DE", "DE"),
    ("Indianapolis", "IN", "IN"),
    ("St. John's", "NL", "NL"),
    ("London", "ON", "ON"),
    ("Paris", "TX", "TX"),
    ("Montreal", "Quebec", "QC"),
    ("Toronto", "Canada", "ON"),
    ("Toronto", "Ontario", "ON"),
    ("Boston", "USA", "MA"),
    ("Oak Hill", "WV", "WV"),
    # no state at all: filled when the city is unambiguous…
    ("Toronto", "", "ON"),
    ("Cologne", "", "Germany"),
    ("Zürich", "", "Switzerland"),
    ("Reykjavik", "", "Iceland"),
    ("Mexico City", "", "Mexico"),
    # …and left for you when it isn't
    ("Portland", "", ""),
    ("Springfield", "", ""),
    ("Vancouver", "", ""),
])
def test_resolve(places, city, state, want):
    assert places.resolve(city, state) == want


def test_unknown_small_town_keeps_its_state(places):
    assert places.resolve("Nowhereville", "TN") == "TN"      # not Tunisia
    assert places.resolve("Nowhereville", "Narnia") == "Narnia"


def test_your_library_settles_ambiguous_cities(places, tmp_path):
    li = importlib.import_module("app.library_index")
    c = li._conn()
    for v in ("Merriweather Post Pavilion", "Cumberland County Civic Center", "State Theatre"):
        li._upsert_venue(c, v, "Portland", "ME")
    c.commit()
    li._city_cache["at"] = 0
    assert places.resolve("Portland", "") == "ME"


def test_country_spellings_can_be_changed(places):
    cfg = importlib.import_module("app.config")
    cfg.save({"places": {"country_names": {"United Kingdom": "England"}}})
    assert places.resolve("London", "UK") == "England"
    assert places.resolve("London", "") == "England"
    assert places.is_country("England")
    # removing the line removes the rename (saved maps replace, not merge)
    cfg.save({"places": {"country_names": {}}})
    assert places.resolve("London", "UK") == "United Kingdom"


def test_switching_off_leaves_states_alone(places):
    cfg = importlib.import_module("app.config")
    md = importlib.import_module("app.metadata")
    cfg.save({"places": {"enabled": False}})
    m = md.ShowMeta(city="Amsterdam", state="NL")
    assert places.apply(m) is False and m.state == "NL"


def test_album_strings_with_countries_parse(places):
    md = importlib.import_module("app.metadata")
    m, complete = md.parse_album_string("2024-06-01 Ziggo Dome, Amsterdam, Netherlands [SBD]")
    assert (m.venue, m.city, m.state, complete) == ("Ziggo Dome", "Amsterdam", "Netherlands", True)
    m, _ = md.parse_album_string("1990-04-01 Olympiahalle, Munich, Bavaria [AUD]")
    assert m.state == "Bavaria"          # parsing alone doesn't guess; the pipeline resolves it
    m, _ = md.parse_album_string("2025-03-03 Some Hall, Sarajevo, Bosnia and Herzegovina [AUD]")
    assert m.state == "Bosnia and Herzegovina"
    assert md.normalise_state("Ontario") == "ON" and md.normalise_state("England") == "United Kingdom"
    assert md.normalise_state("DE") == "DE"                  # needs the city to decide


def test_setlistfm_abroad_uses_the_country(places):
    src = importlib.import_module("app.sources")
    assert src._setlistfm_state({"stateCode": "BY", "country": {"code": "DE", "name": "Germany"}}) == "Germany"
    assert src._setlistfm_state({"stateCode": "ON", "country": {"code": "CA"}}) == "ON"
    assert src._setlistfm_state({"stateCode": "CO", "country": {"code": "US"}}) == "CO"
    assert src._setlistfm_state({"stateCode": "ENG", "country": {"code": "GB"}}) == "United Kingdom"


def test_audit_accepts_country_names(places):
    audit = importlib.import_module("app.audit")
    src = Path(audit.__file__).read_text()
    assert "places.is_country" in src
    assert places.is_country("United Kingdom") and places.is_country("Czech Republic")
    assert not places.is_country("NY")


def _foreign_show(watch: Path, name: str, album: str, info_loc: str) -> Path:
    from mutagen.flac import FLAC
    from test_safety import make_audio
    show = watch / name
    for i, title in enumerate(["Scarlet Begonias", "Fire on the Mountain"], 1):
        f = show / f"t{i:02d}.flac"
        make_audio(f, freq=300 + 50 * i)
        a = FLAC(f)
        a["ARTIST"] = "Grateful Dead"
        a["ALBUM"] = album
        a["TITLE"] = title
        a.save()
    (show / "info.txt").write_text(f"Grateful Dead\n1972-05-10\nConcertgebouw\n{info_loc}\n"
                                   "Source: SBD\n\n1. Scarlet Begonias\n2. Fire on the Mountain\n")
    return show


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("album,info_loc", [
    ("1972-05-10 Concertgebouw, Amsterdam, NL [SBD]", "Amsterdam, NL"),
    ("1972-05-10 Concertgebouw, Amsterdam, Holland [SBD]", "Amsterdam, Holland"),
    ("1972-05-10 Concertgebouw, Amsterdam [SBD]", "Amsterdam"),
])
def test_a_european_show_is_tagged_with_the_country(tmp_path, album, info_loc):
    from test_safety import offline
    m = fresh_app(tmp_path, offline({"safety": {"dry_run": False}}))
    m["database"].init()
    show = _foreign_show(tmp_path / "watch", "gd1972-05-10.sbd", album, info_loc)
    r = m["pipeline"].process_show(show)
    from mutagen.flac import FLAC
    dest = Path(r.get("dest") or show)
    tags = FLAC(next(dest.rglob("*.flac")))
    assert tags["ALBUM"][0].startswith("1972-05-10 Concertgebouw, Amsterdam, Netherlands"), (r, tags["ALBUM"])


def test_official_labels_are_a_setting(places):
    md = importlib.import_module("app.metadata")
    cfg = importlib.import_module("app.config")
    m, _ = md.parse_album_string("2024-06-01 Hall, Town, NY [SBD Official 44351]")
    assert m.official == "Official" and m.shnid == "44351"
    m, _ = md.parse_album_string("2024-06-01 Hall, Town, NY [SBD Storefront 44351]")
    assert m.official == "" and m.shnid == "044351"          # unknown name: an etree SHNID
    cfg.save({"filing": {"official_labels": "Storefront, Label Two"}})
    m, _ = md.parse_album_string("2024-06-01 Hall, Town, NY [SBD storefront 44351]")
    assert m.official == "Storefront" and m.shnid == "44351"
    assert md.extract_official("this is not an official release", allow_generic=False) == ""
    assert md.extract_official("bought from Label Two", allow_generic=False) == "Label Two"


@pytest.mark.parametrize("text,venue", [
    ("Goose\n2026-09-28\nParadiso\nAmsterdam, NL\nSource: AUD\n", "Paradiso"),
    ("Phish\n1985-03-04\nNectar's\nBurlington, VT\n", "Nectar's"),
    ("Phish\n1985-03-04\ntaped from row 12\nBurlington, VT\n", ""),
    ("Phish\n1985-03-04\nThis was a great night.\nBurlington, VT\n", ""),
    ("Phish\n1985-03-04\nBurlington, VT\nNectar's\n", ""),          # only the line ABOVE counts
])
def test_venue_without_a_keyword_above_city(places, tmp_path, text, venue):
    md = importlib.import_module("app.metadata")
    f = tmp_path / "info.txt"
    f.write_text(text)
    m = md.parse_info_file(f)
    assert m.venue == venue and m.city == "Burlington" or m.city == "Amsterdam"


@pytest.mark.parametrize("name,venue", [
    ("jgb1990-11-17 show", ""), ("Goose 2024-06-01 Early Show", ""),
    ("Phish 1997-11-22 Hampton Coliseum", "Hampton Coliseum"), ("Dead 1977-05-08 Show Barn", "Show Barn"),
])
def test_filler_words_are_not_venues(places, name, venue):
    md = importlib.import_module("app.metadata")
    m = md.parse_folder_name(name, {})
    md.sanitize_location(m)
    assert m.venue == venue


def test_review_regressions(places, tmp_path):
    md = importlib.import_module("app.metadata")
    cfg = importlib.import_module("app.config")
    m = md.ShowMeta(venue="The Matrix", city="San Francisco", state="CA")
    md.sanitize_location(m)
    assert m.venue == "The Matrix"
    for line in ("Fall Tour", "with John Kahn"):
        f = tmp_path / "i.txt"
        f.write_text(f"Jerry Garcia Band\n1977-11-01\n{line}\nBerkeley, CA\n")
        assert md.parse_info_file(f).venue == ""
    cfg.save({"filing": {"official_labels": "LMA+, (Official)"}})
    assert md.extract_official("bought from LMA+ today", allow_generic=False) == "LMA+"

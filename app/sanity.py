"""Small sanity checks shared by identification and the learning tool.

Each answers one question about a value that came from somewhere untrusted
(an info file, an album tag, a learned rule): is this a real state? could
this be a city? are these two names the same act?
"""
import difflib
import re

_SMALL = {"the", "and", "band", "a", "of", "&", "with", "his", "her", "their", "y", "et", "und"}
_INSTRUMENTS = re.compile(
    r"\b(piano|organ|keyboards?|vocals?|guitars?|bass|drums?|percussion|synth\w*|"
    r"saxophone|sax|trumpet|violin|fiddle|banjo|mandolin|harmonica|dobro|pedal steel)\b",
    re.IGNORECASE)
_SOURCE_WORDS = re.compile(
    r"^(sbd|aud|fm|pre-?fm|mtx|flac\d*|shn|mp3|wav|dat|cd|cdr|soundboard|audience|"
    r"master|lineage|source|unknown|n/?a|tba|tbd|rm|-)\d*$", re.IGNORECASE)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _SMALL}


def initials(s: str) -> str:
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _SMALL)


def _initial_forms(s: str) -> set:
    """'Grateful Dead' → gd, gdead · 'New Riders of the Purple Sage' → nrps, nrotps…"""
    out = set()
    ws_all = re.findall(r"[a-z0-9]+", (s or "").lower())
    for ws in (ws_all, [w for w in ws_all if w not in _SMALL],
               [w for w in ws_all if w not in ("the", "and", "of", "a")]):
        for k in range(1, len(ws) + 1):          # initials, then the rest in full
            out.add("".join(w[0] for w in ws[:k]) + "".join(ws[k:]))
    return out - {""}


def names_related(a: str, b: str) -> bool:
    """Could these be the same act, spelled differently? 'Jerry Garcia' ~
    'Jerry Garcia Band', 'nrps' ~ 'New Riders of the Purple Sage',
    'CSNY' ~ 'Crosby, Stills, Nash & Young' — but not 'Toad the Wet Sprocket'
    ~ 'Grateful Dead'. Works on normalised keys too ('toadthewetsprocket')."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or (min(len(na), len(nb)) >= 4 and (na in nb or nb in na)):
        return True
    if words(a) & words(b):
        return True
    if na in _initial_forms(b) or nb in _initial_forms(a):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6


def valid_state(state: str) -> bool:
    """A US state or Canadian province code, or a country spelled out."""
    from . import places
    s = (state or "").strip()
    if not s:
        return False
    if s.upper() in places.US_STATES or s.upper() in places.CA_PROVINCES:
        return True
    return places.is_country(s)


def plausible_city(city: str) -> bool:
    """Could this be a city name? Rejects instrument lists, sentences, dates,
    source words and 'venue - city' or '@' composites."""
    c = (city or "").strip()
    if len(c) < 2 or len(c) > 40:
        return False
    if re.search(r"[:@;|<>{}\[\]]|\s[-–—]\s|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", c):
        return False
    if _INSTRUMENTS.search(c) or _SOURCE_WORDS.match(c):
        return False
    if sum(ch.isdigit() for ch in c) > 1:
        return False
    if len(c.split()) > 5:
        return False
    return bool(re.search(r"[A-Za-z]{2}", c))


def plausible_location(city: str, state: str) -> bool:
    return plausible_city(city) and valid_state(state)


def is_source_word(s: str) -> bool:
    return bool(_SOURCE_WORDS.match((s or "").strip()))


_VENUE_JUNK = re.compile(
    r"\b(quoted|thanks|setlist|shared by|uploaded|this is|banned|format:|recorded by|"
    r"lineage|transferr?ed|generated|torrent|seeded|tracklist|bonus tracks?|disc \d)\b", re.IGNORECASE)


def plausible_venue(venue: str) -> bool:
    """Could this be a venue's name? Not a sentence, a date, a label or a source word."""
    v = (venue or "").strip()
    if len(v) < 2 or len(v) > 90 or is_source_word(v):
        return False
    if re.match(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", v) or v[:1] in "(\"'[":
        return False
    if _VENUE_JUNK.search(v) or ": " in v:
        return False
    words_ = v.split()
    lower = [w for w in words_ if w[:1].islower() and w.lower() not in
             ("of", "the", "and", "at", "on", "in", "de", "la", "le", "du", "des", "am", "an", "der", "&", "-", "y")]
    return not (len(words_) > 5 and len(lower) >= 3)

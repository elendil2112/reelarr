"""Early and late shows: two sets on one night, filed as one show.

When two folders in the watch folder have the same artist and date, and one
is the early show and the other the late show — said in the folder name
("…late.ev.re10…", "Early Show") or as "early show" / "late show" in the
info file — Reelarr files them together:

    Bob Weir/bw1978-03-25 The Old Waldorf, San Francisco, CA [SBD 082964 TinyDancer & AUD Miller]/
        bwb1978-03-25.sbd.tinydancer.82964.sbeok.flac/      ← early: disc 1
        Bob Weir Band 1978-03-25 San Francisco,CA.late…/     ← late:  disc 2

Each part keeps its own folder, source and setlist. Every track carries the
same album, its sources joined early-then-late, and disc 1 / disc 2 with
track numbers starting again on each disc.

A folder you drop in that already holds an early and a late subfolder is
treated the same way.
"""
import re
from pathlib import Path

from .metadata import (AUDIO_EXTS, ShowMeta, parse_best_info, parse_folder_name,
                       read_embedded_tags)

_FOLDER_PART = re.compile(r"(?<![a-z])(early|late)(?:[\s._-]*(?:show|set|sh))?(?![a-z])", re.IGNORECASE)
# a date in a folder name: 1978-03-25 · 1978.03.25 · gd77-05-08
_DATE = re.compile(r"(?:19|20)\d\d[-._]\d\d[-._]\d\d|(?<!\d)\d\d[-._]\d\d[-._]\d\d(?!\d)")
# an info-file line that SAYS which show this is: "Late Show", "1978-03-25 (early show)",
# "Early Show — 7:30pm". Not a sentence that mentions the other one.
_TEXT_LINE = re.compile(r"^[\W\d\s]*(?:the\s+)?(early|late)\s+show\b[^a-z]*(?:\d{1,2}(?::\d\d)?\s*[ap]\.?m\.?)?[^a-z]*$",
                        re.IGNORECASE)
_TEXT_DATE_LINE = re.compile(r"(?:19|20)\d\d[-/.]\d{1,2}[-/.]\d{1,2}.{0,6}[(\[\-–—]\s*(early|late)\s+show\s*[)\]]?\s*$",
                             re.IGNORECASE)
_TEXT_SUFFIXES = (".txt", ".nfo", ".md")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def part_of(folder: Path) -> str:
    """'early', 'late' or '' — the folder name decides; otherwise an info file
    that mentions exactly one of 'early show' / 'late show'."""
    name = folder.name
    d = _DATE.search(name)
    if d:
        name = name[d.end():]            # only after the date: "The Early November 2020-…" isn't a part
    found = {m.group(1).lower() for m in _FOLDER_PART.finditer(name)}
    if len(found) == 1:
        return found.pop()
    if found:
        return ""                                   # both words in one name: can't tell
    said = set()
    try:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES and p.stat().st_size < 512 * 1024:
                for line in p.read_text(errors="replace").splitlines()[:40]:
                    line = line.strip()
                    if len(line) > 48:
                        continue
                    m = _TEXT_LINE.match(line) or _TEXT_DATE_LINE.search(line)
                    if m:
                        said.add(m.group(1).lower())
    except OSError:
        return ""
    return said.pop() if len(said) == 1 else ""


def _initials(name: str) -> str:
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", (name or "").lower()))


def same_artist(a: str, b: str) -> bool:
    """'Bob Weir Band' = 'bob weir band' = 'bwb' (etree-style initials)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return (len(na) <= 5 and na == _initials(b)) or (len(nb) <= 5 and nb == _initials(a))


def quick_identity(folder: Path, aliases: dict) -> tuple:
    """(artist, date) from what's on disk — tags, info file, folder name —
    without any lookups. Enough to spot a pair."""
    try:
        tags, _curated, _ = read_embedded_tags(folder)
    except Exception:
        tags = ShowMeta()
    try:
        info, _ = parse_best_info(folder)
    except Exception:
        info = ShowMeta()
    name = parse_folder_name(folder.name, aliases)
    artist = tags.artist or info.artist or name.artist
    date = tags.date or name.date or info.date
    return artist, date


def _has_audio(folder: Path) -> bool:
    return any(p.suffix.lower() in AUDIO_EXTS for p in folder.rglob("*") if p.is_file())


def match(folders: list, aliases: dict) -> list:
    """[(early, late), …] among these folders. A folder joins at most one
    pair, and only an unambiguous early/late couple counts: two lates and an
    early on the same night are left alone for you to sort out."""
    info = []
    for f in folders:
        part = part_of(f)
        if not part or not _has_audio(f):
            continue
        artist, date = quick_identity(f, aliases)
        if artist and date:
            info.append((f, part, artist, date))
    pairs, used = [], set()
    for f, part, artist, date in info:
        if f in used or part != "early":
            continue
        night = [x for x in info if x[3] == date and same_artist(x[2], artist) and x[0] not in used]
        earlies = [x for x in night if x[1] == "early"]
        lates = [x for x in night if x[1] == "late"]
        if len(earlies) == 1 and len(lates) == 1:
            pairs.append((f, lates[0][0]))
            used |= {f, lates[0][0]}
    return pairs


def split(show_dir: Path, aliases: dict):
    """(early, late) if this folder holds exactly an early and a late show
    as its two subfolders, else None."""
    try:
        subs = [p for p in show_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))]
    except OSError:
        return None
    if len(subs) != 2 or any(p.suffix.lower() in AUDIO_EXTS for p in show_dir.iterdir() if p.is_file()):
        return None
    found = match(subs, aliases)
    return found[0] if found else None


def combine(early: dict, late: dict, early_dir: Path, late_dir: Path) -> dict:
    """One identify() result from the two halves'."""
    e, l_ = early["meta"], late["meta"]
    surer, other = (e, l_) if early["confidence"] >= late["confidence"] else (l_, e)
    m = ShowMeta()
    for f in ("artist", "host_artist", "date", "venue", "city", "state", "genre"):
        setattr(m, f, getattr(surer, f) or getattr(other, f) or "")
    m.source_type = e.source_type or l_.source_type
    m.official = e.official if e.official and l_.official else ""
    m.parts = [
        {"dir": early_dir.name, "disc": 1, "label": "early show", "bracket": e.bracket,
         "tracks": e.tracks},
        {"dir": late_dir.name, "disc": 2, "label": "late show", "bracket": l_.bracket,
         "tracks": l_.tracks},
    ]
    brackets = [p["bracket"] for p in m.parts if p["bracket"]]
    m.bracket_override = " & ".join(dict.fromkeys(brackets)) if brackets else ""
    m.tracks = ([{**t, "disc": 1} for t in e.tracks] + [{**t, "disc": 2} for t in l_.tracks])
    m.notes = "early + late show"
    prov = {k: f"{v} (early)" for k, v in early["provenance"].items()}
    for k, v in late["provenance"].items():
        prov[k] = prov.get(k, "") + (" · " if k in prov else "") + f"{v} (late)"
    from .pipeline import REQUIRED_FIELDS
    return {"meta": m, "provenance": prov,
            "confidence": min(early["confidence"], late["confidence"]),
            "missing": [f for f in REQUIRED_FIELDS if not getattr(m, f)],
            "curated": early["curated"] and late["curated"],
            "sidecar": early["sidecar"] or late["sidecar"],
            "artist_in_library": early["artist_in_library"] or late["artist_in_library"],
            "titles_uncertain": early.get("titles_uncertain") or late.get("titles_uncertain") or "",
            "combined": True}

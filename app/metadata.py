"""
Reelarr metadata engine.

Local extraction sources (network sources live in sources.py):
  - embedded audio tags            official/curated releases carry the answer already
  - info.txt / *.txt / *.md files
  - etree-style folder names        gd1977-05-08.sbd.hicks.4982.flac16
  - descriptive folder names        Billy Strings 2022-10-29 Arena, Asheville, NC [SBD]
  - .reelarr.json sidecars          provenance dropped by a provider add-on
                                    (older sidecar names are read too)

Every value carries provenance so the merger can score cross-source agreement.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile

AUDIO_EXTS = {".flac", ".mp3", ".shn", ".wav", ".m4a", ".ogg"}
TAGGABLE_EXTS = {".flac", ".mp3"}

_MOJIBAKE_RE = re.compile(r"[\u00c3\u00c2\u00e2][\u0080-\u00ff\u20ac\u2122\u0153\u2018\u2019\u201c\u201d\u2026]")
_TYPOGRAPHIC = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"',
                0x2013: "-", 0x2014: "-", 0x00a0: " ", 0x2026: "...",
                0xfeff: None, 0x200b: None, 0x200e: None, 0x200f: None}


def fix_text(s: str) -> str:
    """Repair mojibake (UTF-8 read as cp1252: 'Dawg\u00e2\u20ac\u2122s' -> "Dawg's")
    and normalize curly quotes/dashes to plain ASCII."""
    if not s:
        return s
    out = s
    for _ in range(2):                       # double-encoded needs two passes
        if not _MOJIBAKE_RE.search(out):
            break
        try:
            out = out.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
    return out.translate(_TYPOGRAPHIC).strip()


# ── track-title hygiene ──
_TITLE_DURATION_RE = re.compile(r"\s*[>\-]*\s*\d{1,3}:\d{2}(?:[.:]\d{1,3})?\s*$")
_JUNK_TITLE_RE = re.compile(
    r"^(source|source matrix|lineage|transfer|matrix|recorded|recording|"
    r"taper|setlist|set list|tracklist|track list|notes?|comments?)\b", re.IGNORECASE)


def clean_track_title(t: str) -> str:
    """'Blow Away 6:23' -> 'Blow Away' · 'Slipknot > 8:04' -> 'Slipknot' · fix mojibake."""
    t = fix_text(t)
    t = _TITLE_DURATION_RE.sub("", t)
    t = re.sub(r"\s*[-=]?>+\s*$", "", t)
    t = re.sub(r"[\s*&+~#^@]+$", "", t)     # taper footnote markers: * & etc
    t = t.strip()
    m = re.match(r"^(.{3,}?)[\s\-]+\1$", t, re.IGNORECASE)   # "X X" → "X"
    if m:
        t = m.group(1)
    return t.strip()


def dedupe_track_list(tracks: list) -> list:
    """Reject DEGENERATE track lists — every title identical, or one title
    covering nearly the whole show. Legit repeats (Intro x2, Jam reprise,
    Drums/Space) survive; mass-tag junk does not."""
    if not tracks or len(tracks) <= 3:
        return tracks
    from collections import Counter
    counts = Counter(t["title"].strip().lower() for t in tracks)
    top_title, top_n = counts.most_common(1)[0]
    if len(counts) == 1:
        return []
    if len(tracks) >= 6 and top_n / len(tracks) >= 0.8:
        return []
    return tracks


def is_junk_track_title(t: str) -> bool:
    """Metadata labels and tool-report rows that leak into setlists."""
    s = t.strip()
    if _JUNK_TITLE_RE.match(s) or s.endswith(":"):
        return True
    if re.search(r"\.(flac|shn|wav|mp3|ape|aiff?|m4a)\b", s, re.IGNORECASE):
        return True    # shntool/ffp rows carry filenames
    digits = sum(c.isdigit() for c in s)
    return bool(s) and digits / len(s) > 0.5   # byte counts, checksum soup



# ── Date extraction ───────────────────────────────────────────────────────────
# Liberal in recognition, strict in acceptance: every candidate must be a real
# calendar date (datetime validation — no Feb 30, no month 13) in 1900-2099,
# and the output is ALWAYS zero-padded YYYY-MM-DD.

import datetime as _dt

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
                r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")


def _valid(y, m, d) -> str:
    """Real-calendar validation; returns zero-padded ISO or ''."""
    try:
        dt = _dt.date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return ""
    if not (1900 <= dt.year <= 2099):
        return ""
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def _month_num(name: str) -> int:
    return _MONTH_MAP.get(name.lower().rstrip(".")[:4].rstrip("t")
                          if name.lower().startswith("sept")
                          else name.lower().rstrip(".")[:3], 0)


# (pattern, converter) — ordered most reliable first. Separator classes use a
# backreference so "2021 3 21" matches but mixed junk like "2021-3 21" doesn't.
_DATE_PATTERNS = [
    # "March 21, 2021" · "Mar. 21st 2021"
    (re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
                rf"((?:19|20)\d{{2}})\b", re.IGNORECASE),
     lambda m: _valid(m.group(3), _month_num(m.group(1)), m.group(2))),
    # "21 March 2021" · "21st Mar, 2021"
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+"
                rf"((?:19|20)\d{{2}})\b", re.IGNORECASE),
     lambda m: _valid(m.group(3), _month_num(m.group(2)), m.group(1))),
    # "2021-03-21" · "2021_3_1" · "2021 03 21" · "2021*03*21" · "2021.3.9"
    (re.compile(r"\b((?:19|20)\d{2})([-/._* ])(\d{1,2})\2(\d{1,2})(?!\d)"),
     lambda m: _valid(m.group(1), m.group(3), m.group(4))),
    # "3-21-2021" · "03/21/2021" · "3 21 2021" · "3_21_2021"
    (re.compile(r"(?<!\d)(\d{1,2})([-/._ ])(\d{1,2})\2((?:19|20)\d{2})\b"),
     lambda m: _valid(m.group(4), m.group(1), m.group(3))),
    # etree 2-digit year: "77-05-08" / "gd77.05.08" (yy-mm-dd, then mm-dd-yy)
    (re.compile(r"(?<!\d)(\d{2})([-.])(\d{2})\2(\d{2})(?!\d)"),
     lambda m: (_valid(("19" if int(m.group(1)) > 40 else "20") + m.group(1),
                       m.group(3), m.group(4))
                or _valid(("19" if int(m.group(4)) > 40 else "20") + m.group(4),
                          m.group(1), m.group(3)))),
]


def extract_date(text: str) -> str:
    if not text:
        return ""
    for pattern, conv in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                d = conv(m)
            except Exception:
                continue
            if d:
                return d
    return ""


# ── Source-type / provenance helpers (proven logic from bootleg_tagger) ──────

_AUD_MIC_RE = re.compile(
    r"\b(dpa|neumann|schoeps|akg|sennheiser|mbho|rode|core sound|"
    r"nak|nakamichi|cardioid|omni|nt4|mk4|mk5|at853|"
    r"cmc|km8[45]|mke2|mke3|binaural|in-?ear)\b", re.IGNORECASE)


def normalise_source_type(raw: str) -> str:
    t = raw.upper()
    if re.search(r"\bFM\b|BROADCAST|SIMULCAST", t):
        return "SBD.FM"
    if re.search(r"\bMATRIX\b|\bMTX\b", t):
        return "MTX"
    if re.search(r"\bD?SBD\b|\bSDB\b|SOUNDBOARD|SOUND\s*BOARD|BOARD\b", t):
        return "SBD"          # DSBD = digital soundboard; SDB = transposition typo
    if re.search(r"\bFOB\b", t):
        return "AUD.FOB"      # front-of-board: an audience recording taped at the desk
    if re.search(r"\bAUD\b|AUDIENCE|TAPER\b", t) or _AUD_MIC_RE.search(raw):
        return "AUD"
    return ""


# ── Whole-text source inference (info files) ─────────────────────────────────
# Actual microphone hardware — deliberately excludes playback/transfer decks
# (Nakamichi, Sony, Tascam appear in SBD lineages constantly).
_MIC_HW_RE = re.compile(
    r"\b(dpa|neumann|schoeps|akg\s*c?\d*|sennheiser|mbho|rode|core\s*sound|"
    r"church\s*audio|sound\s*professionals|sp-?cmc|sp-?bmc|at\s?853|at\d{3,4}|"
    r"km\s?1[48]\d|mk\s?[45]\d?|cmc\d|mke\s?2|nt[45]|b3|4061|4022|c414|"
    r"cardioids?|omnis?|hypercardioids?|subcardioids?|binaural)\b", re.IGNORECASE)
_AUD_WORD_RE = re.compile(
    r"\baud\b|\baudience\s+(recording|tape|source|master|mics?|section|pull)\b|"
    r"mics?\s+(?:mounted|clamped|at|on|in)|taping\s+rig", re.IGNORECASE)
_SBD_WORD_RE = re.compile(r"\b(d?sbd|sdb|soundboard|sound\s*board|board\s*(feed|tape|patch)|"
                          r"house\s*mix|monitor\s*mix|desk\s*feed)\b", re.IGNORECASE)
_FM_WORD_RE = re.compile(r"\b(fm|radio|tv)\s*broadcast\b|\bbroadcast(ed)?\b|\bsimulcast\b|"
                         r"\brebroadcast\b|\bpre-?fm\b|\bair\s*check\b|\baircheck\b|"
                         r"\b(wbcn|wnew|kqed|ksan|kpfa|wfmu|wmmr|kmet|wlir|wxrt|kfog|"
                         r"wneu|wgbh|npr|bbc)\b", re.IGNORECASE)
_CALLSIGN_RE = re.compile(r"\b[KW][A-Z]{2,3}(-?(FM|AM))\b")   # WNEW-FM style
_MTX_WORD_RE = re.compile(r"(?<!the )\bmatrix\b|\bmtx\b", re.IGNORECASE)  # not the SF venue


def infer_source_type(text: str) -> str:
    """Combine evidence across a whole info file:
    mics → AUD · soundboard → SBD · both → MTX · broadcast/station → SBD.FM
    · FOB (front-of-board audience) → AUD.FOB"""
    if not text:
        return ""
    if _FM_WORD_RE.search(text) or _CALLSIGN_RE.search(text):
        return "SBD.FM"
    if _MTX_WORD_RE.search(text):
        return "MTX"
    has_sbd = bool(_SBD_WORD_RE.search(text))
    has_fob = bool(re.search(r"\bFOB\b", text, re.IGNORECASE))
    has_aud = bool(_MIC_HW_RE.search(text) or _AUD_WORD_RE.search(text) or has_fob)
    if has_sbd and has_aud:
        return "MTX"
    if has_sbd:
        return "SBD"
    if has_fob:
        return "AUD.FOB"      # front-of-board is a specific audience position
    if has_aud:
        return "AUD"
    return ""


def official_labels() -> list:
    """Names of official-release sources, from Settings → Filing policy
    ("official labels", comma-separated — e.g. the stores you buy from).
    The generic word "Official" is always understood in [brackets]."""
    try:
        from . import config
        raw = (config.load().get("filing", {}) or {}).get("official_labels", "") or ""
    except Exception:
        raw = ""
    items = raw if isinstance(raw, list) else re.split(r"[,\n]", raw)
    return [x.strip() for x in items if x.strip() and x.strip().lower() != "official"]


def extract_official(text: str, allow_generic: bool = True) -> str:
    """Return the official-source label found in text, if any, spelled the
    way it is in your settings.

    allow_generic=False is for FREE TEXT (info files, loose folder words):
    only your named labels count there — the bare word "official" appears in
    phrases like "not an official release" and must never tag a bootleg as
    Official. Generic "Official" is honoured only in structured contexts:
    [brackets] and sidecars."""
    labels = official_labels()
    names = [re.escape(x) for x in labels] + (["official"] if allow_generic else [])
    if not names:
        return ""
    m = re.search(r"(?<!\w)(" + "|".join(names) + r")(?!\w)", text or "", re.IGNORECASE)
    if not m:
        return ""
    tok = m.group(1).lower()
    return next((x for x in labels if x.lower() == tok), "Official")


def _pad_shnid(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(6) if digits else ""


_SHNID_RE = re.compile(r"(?:shnid|shn\s*id|shn[-_\s]*#?)\s*[=:#]?\s*(\d{4,7})", re.IGNORECASE)


def extract_shnid(text: str) -> str:
    m = _SHNID_RE.search(text)
    return _pad_shnid(m.group(1)) if m else ""


_TAPER_LINE_RE = re.compile(
    r"^\s*(?:taper|recorded\s+by|taped\s+by|recorder|recordist)\s*:?\s*(.+)$", re.IGNORECASE)
# "Transfer: TinyDancer" names the person who transferred an SBD — but
# "Transfer: DAT > CDR > EAC" is gear, so only a bare name counts here.
_TRANSFER_NAME_RE = re.compile(
    r"^\s*(?:transfer(?:red)?|xfer)(?:\s+by)?\s*:?\s*([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*)?)\s*$",
    re.IGNORECASE)
_GEAR_VALUE_RE = re.compile(
    r"^(dpa|neumann|schoeps|akg|sennheiser|mbho|rode|core\s+sound|nak|nakamichi|"
    r"sony|tascam|marantz|edirol|zoom|at\d|km\d|mk\d|cmc|mke|nt4|binaural|unknown|n/?a)\b",
    re.IGNORECASE)


def extract_recorder(text: str) -> str:
    m = _TAPER_LINE_RE.match(text.strip())
    if not m:
        t = _TRANSFER_NAME_RE.match(text.strip())
        if t and not _GEAR_VALUE_RE.match(t.group(1)) \
                and not re.match(r"^(dat|cdr?|wav|flac|eac|cd|tape|reel|cassette|pc|computer|master|"
                                 r"analog|digital|audacity|wavelab|soundforge|cdwave|foobar|sbd|aud|"
                                 r"unknown|none|n/?a)\b",
                                 t.group(1), re.IGNORECASE):
            return name_case(t.group(1).split()[-1])
        return ""
    value = m.group(1).strip()
    if _GEAR_VALUE_RE.match(value):
        return ""
    value = re.sub(r"\S+@\S+", "", value)
    value = re.sub(r"\(.*?\)", "", value).strip()
    tokens = value.split()
    return name_case(tokens[-1].strip(".,;")) if tokens else ""


def name_case(tok: str) -> str:
    """'miller' → 'Miller', but a name someone typed in mixed case keeps it:
    'TinyDancer', 'McGee', 'dEvo'."""
    if tok and tok != tok.lower() and tok != tok.upper():
        return tok
    return tok.title()


# ── ShowMeta ─────────────────────────────────────────────────────────────────

@dataclass
class ShowMeta:
    artist: str = ""
    date: str = ""            # YYYY-MM-DD
    venue: str = ""
    city: str = ""
    state: str = ""           # ST code or country name
    source_type: str = ""     # SBD | AUD | MTX | SBD.FM
    official: str = ""        # a platform name | PD###### | Official
    shnid: str = ""
    recorder: str = ""
    genre: str = ""
    host_artist: str = ""     # headliner: drives Album Artist, folder, and prefix
    notes: str = ""
    tracks: list = field(default_factory=list)   # [{num, title, disc}]
    # An early + late show filed as one: each part keeps its own folder,
    # source and setlist, and the album's [bracket] joins the two sources.
    parts: list = field(default_factory=list)    # [{dir, disc, label, bracket, tracks}]
    bracket_override: str = ""
    extras: list = field(default_factory=list)   # file names you marked "not a song" in Review

    FIELDS = ("artist", "host_artist", "date", "venue", "city", "state",
              "source_type", "official", "shnid", "recorder", "genre")

    def to_dict(self):
        d = {**{f: getattr(self, f) for f in self.FIELDS},
             "notes": self.notes, "tracks": self.tracks}
        if self.parts:
            d["parts"] = self.parts
            d["bracket_override"] = self.bracket_override
        if self.extras:
            d["extras"] = self.extras
        return d

    @property
    def bracket(self) -> str:
        if self.bracket_override:
            return self.bracket_override
        parts = [p for p in
                 (self.source_type, self.official, self.shnid, self.recorder) if p]
        return " ".join(parts)

    @property
    def album_title(self) -> str:
        """YYYY-MM-DD Venue, City, ST [SBD Official 012345 Miller]"""
        parts = []
        if self.date:
            parts.append(self.date)
        loc = [p for p in (self.venue, self.city, self.state) if p]
        if loc:
            parts.append(", ".join(loc))
        head = " ".join(parts) if parts else "Live"
        return f"{head} [{self.bracket}]" if self.bracket else head

    @property
    def album_artist(self) -> str:
        """Host artist if known (collabs/guests), else umbrella rules
        (Garcia / Trey / Weir side projects roll up)."""
        if self.host_artist:
            return self.host_artist
        a = self.artist.lower()
        gd = {"grateful dead", "the grateful dead"}
        if a not in gd and "garcia" in a:
            return "Jerry Garcia"
        if a != "phish" and ("trey" in a or "anastasio" in a or a == "oysterhead"):
            return "Trey Anastasio"
        if a not in gd and ("weir" in a or "ratdog" in a or "wolf bros" in a):
            return "Bob Weir"
        return self.artist


_FIELD_LABEL_RE = re.compile(
    r"^\s*(venue|location|place|city|site|where|held at)\s*[:\-\u2013]\s*",
    re.IGNORECASE)
_MIC_POSITION_RE = re.compile(
    r"\b(dfc|fob|dead center|on stage|stage lip|soundboard|taper'?s? section|"
    r"section \w+|row [\w\d]+|\d+ ?(feet|ft|meters|m) )", re.IGNORECASE)


def strip_field_label(s: str) -> str:
    """'Venue: Fox Theatre' → 'Fox Theatre'. Label words never belong in tags."""
    return _FIELD_LABEL_RE.sub("", s or "").strip()


_HYPHEN_CITY = re.compile(
    r"\b(winston-salem|wilkes-barre|ho-ho-kus|"
    r"lake-in-the-hills|croton-on-hudson|hastings-on-hudson|"
    r"saddle-brook|point-pleasant|sault-ste-marie|coeur-d-alene|"
    r"port-au-prince|stratford-upon-avon|newcastle-upon-tyne|"
    r"stoke-on-trent|weston-super-mare)\b", re.IGNORECASE)


def sanitize_location(meta: "ShowMeta") -> None:
    """Heal composed/mangled locations, in place:
    city 'Portland Meadows - Portland' with venue 'Portland Meadows' → city 'Portland'
    city that merely repeats the venue → dropped
    city carrying ', ST' when state is empty → split out"""
    def n(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    meta.venue = strip_field_label(fix_text(meta.venue))
    meta.city = strip_field_label(fix_text(meta.city))

    if re.match(r"^(unknown|n/?a|tba|\?+)$", (meta.venue or "").strip(),
                re.IGNORECASE):
        meta.venue = ""
    # words people tack onto folder names that aren't places
    if re.match(r"^(the\s+)?(show|concert|gig|live|set|full show|complete( show)?|early( show)?|"
                r"late( show)?|matinee|night|soundcheck|bonus|audience|soundboard|"
                r"remaster(ed)?|new|fixed|final|tracks?|disc\s*\d*|cd\s*\d*)$",
                (meta.venue or "").strip(), re.IGNORECASE):
        meta.venue = ""

    # leading dates stuck inside venue/city — bare or slugged ("zero1987-06-05")
    _lead_date = r"^\s*\w{0,24}?(?:19|20)\d{2}[-/._ ]\d{1,2}[-/._ ]\d{1,2}\s*[-\u2013,]*\s*"
    for _ in range(2):
        meta.venue = re.sub(_lead_date, "", meta.venue)
        meta.city = re.sub(_lead_date, "", meta.city)

    # per-segment cleanup inside a multi-part venue:
    # "Terminal 5, 2009-09-19 Terminal 5" → "Terminal 5"
    if meta.venue and "," in meta.venue:
        segs = [re.sub(_lead_date, "", x).strip()
                for x in meta.venue.split(",")]
        dedup = []
        for x in (s for s in segs if s):
            if not dedup or n(x) != n(dedup[-1]):
                dedup.append(x)
        meta.venue = ", ".join(dedup)

    # lineage/gear segments riding along in the venue:
    # "(Stage) Neumann TLM-170 > PCM Master, Sweetwater Saloon" → "Sweetwater Saloon"
    if meta.venue and ("," in meta.venue or ">" in meta.venue):
        segs = [x.strip() for x in meta.venue.split(",") if x.strip()]
        keep = [x for x in segs if not re.search(
            r"\b(neumann|schoeps|dpa|akg|sennheiser|mbho|nak\w*|sony|tascam|"
            r"pcm|dat|reel|cassette|master|transfer|lineage|wav|flac)\b|>",
            x, re.IGNORECASE)]
        if len(keep) < len(segs):
            meta.venue = ", ".join(keep)    # may be "" if it was ALL gear

    # junk phrases posing as city/state ("Setlist:Complete No", "Complete Broadcast")
    _junk_loc = re.compile(r"^(set\s*list|setlist|tracklist|lineage|notes?|"
                           r"complete|partial|broadcast|incomplete)\b", re.IGNORECASE)
    if _junk_loc.match(meta.city or ""):
        meta.city = ""
    if _junk_loc.match(meta.state or ""):
        meta.state = ""

    # country suffixes: "NY USA" → "NY" · "U.S.A." alone → ""
    if meta.state:
        s = re.sub(r"[.\s]*(u\.?s\.?a?\.?|united states)[.\s]*$", "",
                   meta.state, flags=re.IGNORECASE).strip(" ,.")
        meta.state = normalise_state(s) if s else ""

    # state must look like a code or a place name (countries can be long:
    # "Bosnia and Herzegovina") — not a sentence
    if meta.state and not re.match(r"^[^\W\d_][^\W\d_ .'\-]*(?:[ .'\-]+[^\W\d_]+){0,4}\.?$", meta.state):
        meta.state = ""

    if meta.city and not meta.state and "," in meta.city:
        c, s = meta.city.rsplit(",", 1)
        s = s.strip()
        if len(s) <= 3 or s.lower() in _US_STATES:
            meta.city, meta.state = c.strip(), normalise_state(s)

    if meta.city and "," in meta.city and meta.state:
        left, right = meta.city.rsplit(",", 1)
        if not meta.venue:
            meta.venue, meta.city = left.strip(), right.strip()

    if meta.city and " - " in meta.city:
        left, right = [x.strip() for x in meta.city.split(" - ", 1)]
        if right and (not meta.venue or n(left) == n(meta.venue)):
            if not meta.venue:
                meta.venue = left
            meta.city = right

    # tight-dash venue-city with no spaces: "Chevy Court-Syracuse" → venue
    # "Chevy Court", city "Syracuse". Only when venue is empty and the shape
    # looks like <multi-word venue>-<single-word city>, so genuinely hyphenated
    # names (Winston-Salem, Wilkes-Barre) are left intact.
    if meta.city and not meta.venue and "-" in meta.city and " - " not in meta.city:
        left, right = [x.strip() for x in meta.city.split("-", 1)]
        left_words = left.split()
        right_words = right.split()
        # venue side has 2+ words (a name), city side is 1-2 words, and neither
        # side is itself a hyphenated-city fragment we recognize as one place
        looks_like_venue_city = (len(left_words) >= 2 and 1 <= len(right_words) <= 2
                                 and "-" not in right and len(right) >= 3)
        if looks_like_venue_city and not _HYPHEN_CITY.search(meta.city):
            meta.venue = left
            # the city side may carry ",ST" (Syracuse,NY) — peel the state off
            if "," in right:
                cpart, spart = right.rsplit(",", 1)
                st = normalise_state(spart.strip())
                if st:
                    meta.city = cpart.strip()
                    if not meta.state:
                        meta.state = st
                else:
                    meta.city = right
            else:
                meta.city = right

    # city carrying a redundant ", ST" when state already holds it
    if meta.city and meta.state and "," in meta.city:
        head, tail = meta.city.rsplit(",", 1)
        if n(normalise_state(tail.strip())) == n(meta.state):
            meta.city = head.strip()

    # venue name repeated inside the city ("Terminal 5" / "Terminal 5, NY")
    if meta.city and meta.venue and n(meta.city).startswith(n(meta.venue)) \
            and n(meta.city) != n(meta.venue):
        stripped = re.sub(re.escape(meta.venue), "", meta.city, count=1,
                          flags=re.IGNORECASE).strip(" ,-")
        if stripped:
            meta.city = stripped

    if meta.venue and "," in meta.venue and (meta.city or meta.state):
        vsegs = [x.strip() for x in meta.venue.split(",")]
        while vsegs and (n(vsegs[-1]) == n(meta.city)
                         or n(vsegs[-1]) == n(meta.state)):
            vsegs.pop()
        if vsegs:
            meta.venue = ", ".join(vsegs)

    if meta.city and n(meta.city) == n(meta.venue):
        meta.city = ""
    if meta.venue and " - " in meta.venue and not meta.city:
        left, right = [x.strip() for x in meta.venue.split(" - ", 1)]
        if left and right:
            meta.venue, meta.city = left, right


# ── Album-string parser ──────────────────────────────────────────────────────
# Parses the house convention: "YYYY-MM-DD Venue, City, ST [SBD Miller 012345]"
# Returns (meta, complete) — complete=True means date+venue+city+state+bracket
# all present, i.e. someone already curated this show.

def parse_album_string(album: str) -> tuple:
    meta = ShowMeta()
    if not album:
        return meta, False
    text = album.strip()

    text = re.sub(r"\]+\s*$", "]", text)          # "[AUD 44,1/16]]" stray brackets
    m = re.search(r"\[([^\]]*)\]\s*$", text)
    bracket = ""
    if m:
        bracket = m.group(1)
        # sample-rate / bit-depth tokens aren't provenance
        bracket = re.sub(r"\b\d{1,3}[,.]?\d?\s*/\s*\d{1,2}\b|\b(16|24)\s*-?bit\b|"
                         r"\b(44|48|88|96|176|192)(\.\d)?\s*k?hz\b", "",
                         bracket, flags=re.IGNORECASE).strip()
        text = text[:m.start()].strip().rstrip(",")
        meta.source_type = normalise_source_type(bracket)
        meta.official = extract_official(bracket)
        shn = re.search(r"\b(\d{4,7})\b", bracket)
        if shn:
            # official release ids stay verbatim;
            # etree shnids get the traditional zero-pad
            meta.shnid = shn.group(1) if meta.official else _pad_shnid(shn.group(1))
        # last capitalised word that isn't a known token = recorder
        _NOT_RECORDERS = {"unknown", "pcm", "dat", "flac", "wav", "shn",
                          "master", "remaster", "mono", "stereo", "khz", "bit"}
        for tok in reversed(bracket.split()):
            if (re.match(r"^[A-Za-z][A-Za-z\-']+$", tok)
                    and tok.lower() not in _NOT_RECORDERS
                    and not normalise_source_type(tok)
                    and not extract_official(tok)):
                meta.recorder = name_case(tok)
                break

    d = extract_date(text)
    if d:
        meta.date = d
        # strip everything up to and including the date expression
        text = re.sub(r"^.*?" + re.escape(text[text.find(d.split('-')[0]):][:10]), "", text, count=1) \
            if d[:4] in text else text
        # remove leading date tokens — repeatedly, legacy tags doubled them
        for _ in range(3):
            new_text = re.sub(r"^\s*(?:19|20)\d{2}[-/._ ]\d{1,2}[-/._ ]\d{1,2}[,\s]*",
                              "", text).strip()
            if new_text == text:
                break
            text = new_text

    parts = [p.strip() for p in text.split(",") if p.strip()]
    # legacy tags from the old tagger doubled the location: "Venue, City, ST, City, ST"
    while len(parts) >= 5 and \
            [x.lower().strip(".") for x in parts[-2:]] == \
            [x.lower().strip(".") for x in parts[-4:-2]]:
        parts = parts[:-2]
    # per-part country suffixes so duplicates become visible: "MA USA" → "MA"
    parts = [re.sub(r"^([A-Za-z]{2})[\s.]+(u\.?s\.?a?\.?|usa)\s*$", r"\1", x,
                    flags=re.IGNORECASE) for x in parts]
    # drop junk parts entirely: pure country tokens ("USA (Late Show)"),
    # lineage/broadcast phrases, slug-dates ("zero1987-06-05")
    _junk_part = re.compile(
        r"^(usa|u\.s\.a\.?|us)([\s(].*)?$|"
        r"^(fm broadcast|unknown( lineage)?|lineage|broadcast|setlist|tracklist|"
        r"notes?|complete|partial|incomplete|n/?a|tba)\b|"
        r"^\w{0,24}?(19|20)\d{2}[-/._]\d{1,2}[-/._]\d{1,2}$",
        re.IGNORECASE)
    parts = [x for x in parts if not _junk_part.match(x.strip())]
    # full US state names → codes so "Ohio" and "OH" dedupe against each other
    parts = [_US_STATES.get(x.strip().strip(".").lower(), x) for x in parts]
    # global keep-first de-duplication: catches "Cleveland, OH, Cleveland, OH"
    # and non-adjacent repeats like "…, Madison, WI, …, Madison, WI"
    seen, dedup = set(), []
    for x in parts:
        k = re.sub(r"[^a-z0-9]", "", x.lower())
        if k and k in seen:
            continue
        seen.add(k)
        dedup.append(x)
    parts = dedup
    if len(parts) >= 3:
        meta.venue = ", ".join(parts[:-2]) if len(parts) > 3 else parts[0]
        if len(parts) == 3:
            meta.venue, meta.city, meta.state = parts[0], parts[1], parts[2]
        else:
            meta.venue = ", ".join(parts[:-2])
            meta.city, meta.state = parts[-2], parts[-1]
    elif len(parts) == 2:
        if re.match(r"^[A-Z]{2}\.?$", parts[1]) or \
                parts[1].strip(".").lower() in _US_STATES:
            if _VENUE_KEYWORDS.search(parts[0]):
                meta.venue, meta.state = parts[0], parts[1]
            else:
                meta.city, meta.state = parts[0], parts[1]
        elif _VENUE_KEYWORDS.search(parts[0]):
            meta.venue, meta.city = parts[0], parts[1]
        else:
            meta.city, meta.state = parts[0], parts[1]
    elif len(parts) == 1:
        meta.venue = parts[0]

    meta.venue = meta.venue.strip()
    meta.city = meta.city.strip().strip(".")
    meta.state = normalise_state(meta.state)
    sanitize_location(meta)

    # a state must look like a code or a country word, not a sentence
    if meta.state and (len(meta.state) > 40 or extract_date(meta.state)):
        meta.state = ""

    complete = bool(meta.date and meta.venue and meta.city and meta.state and bracket)
    return meta, complete


# ── Embedded-tag reader (new source) ─────────────────────────────────────────

_GENERIC_VALUES = {"", "unknown", "unknown artist", "various", "various artists",
                   "untitled", "no artist", "track", "audio"}


def read_embedded_tags(show_dir: Path) -> tuple:
    """
    Read tags from audio files in the folder.
    Returns (meta, curated, track_titles) where curated=True means the ALBUM
    tag parses completely to the house convention (trustworthy, pre-tagged show).
    """
    meta = ShowMeta()
    curated = False
    titles = []
    audio = sorted(p for p in show_dir.rglob("*")
                   if p.suffix.lower() in TAGGABLE_EXTS and p.is_file())
    if not audio:
        return meta, curated, titles

    # scan the first few files until one actually carries tags —
    # track 1 is often an untagged intro/tuning file
    first = None
    for cand in audio[:6]:
        try:
            f = MutagenFile(cand, easy=True)
        except Exception:
            continue
        if f and ((f.get("artist") or [""])[0].strip()
                  or (f.get("album") or [""])[0].strip()):
            first = f
            break
        if f and first is None:
            first = f
    if not first:
        return meta, curated, titles

    def tag(name):
        v = first.get(name)
        return v[0].strip() if v else ""

    artist = tag("artist")
    if artist.lower() not in _GENERIC_VALUES and plausible_artist(artist):
        meta.artist = clean_artist(artist)
    raw_date = tag("date")
    if raw_date:
        meta.date = extract_date(raw_date) or (raw_date[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw_date) else "")
        if not meta.date and re.match(r"^\d{4}$", raw_date):
            pass  # bare year isn't a show date
    meta.genre = tag("genre") if tag("genre").lower() not in {"", "live", "other"} else ""

    album = tag("album")
    if album:
        album_meta, complete = parse_album_string(album)
        curated = complete
        for f in ("date", "venue", "city", "state", "source_type",
                  "official", "shnid", "recorder"):
            if not getattr(meta, f) and getattr(album_meta, f):
                setattr(meta, f, getattr(album_meta, f))

    # collect track titles for later (never used to overwrite good titles)
    for i, p in enumerate(audio, 1):
        try:
            f = MutagenFile(p, easy=True)
            t = (f.get("title") or [""])[0].strip() if f else ""
        except Exception:
            t = ""
        num = i
        try:
            num = int(str((f.get("tracknumber") or [i])[0]).split("/")[0])
        except Exception:
            pass
        if t and t.lower() not in _GENERIC_VALUES:
            titles.append({"num": num, "title": t, "disc": 1})

    if titles:
        distinct = {t["title"].lower() for t in titles}
        if len(titles) > 3 and len(distinct) == 1:
            titles = []          # mass-tagged junk: same title on every track
    if titles:
        meta.tracks = titles
    return meta, curated, titles


# ── Info-file parser (adapted from bootleg_tagger) ───────────────────────────

_TRACK_RE = re.compile(r"^\s*(?:d\d+t?)?(\d{1,3})[.:\s\-\)]+(.+)$", re.IGNORECASE)
_SET_RE = re.compile(r"^\s*(set\s*(\d+|one|two|three|four)|encore|e\d*)\s*:?\s*$", re.IGNORECASE)
_SOURCE_RE = re.compile(r"^\s*(?:source|lineage|transfer|recording)\s*:?\s*(.+)$", re.IGNORECASE)
_VENUE_KEYWORDS = re.compile(
    r"\b(hall|theater|theatre|arena|stadium|club|bar|garden|center|centre|"
    r"house|field|park|pavil\w*|ballroom|coliseum|auditorium|amphith\w*|"
    r"bowl|shed|grove|barn|inn|lounge|room|stage|grounds|lawn|"
    r"tabernacle|cathedral|chapel|church|temple|palace|winterland|fillmore|"
    r"speedway|raceway|fairgrounds|expo|convention|gorge|red rocks|sphere)\b",
    re.IGNORECASE)
_CITY_STATE_RE = re.compile(r"^(.*?),\s*([A-Z]{2}|[A-Za-z][A-Za-z .']{2,18})\s*$")

# Section headers and label-lines that must never be mistaken for an artist
_SECTION_HEADER_RE = re.compile(
    r"^(set\s*list|setlist|track\s*list|tracklist|tracks|songs|notes|comments?|"
    r"lineage|source|transfer|taper|recorded|recording|band|personnel|musicians|"
    r"venue|location|date|show|info|disc\s*\d*|cd\s*\d*|set\s*\d+|encore)\s*:?\s*$",
    re.IGNORECASE)

_JUNK_ARTIST_VALUES = {"setlist", "set list", "tracklist", "track list", "tracks",
                       "lineage", "source", "notes", "info", "band", "live",
                       "unknown", "various", "bootleg", "show", "venue"}


def plausible_artist(name: str) -> bool:
    """Reject section headers, labels, markup, and junk grabbed by heuristics."""
    n = (name or "").strip().strip("\ufeff")
    if len(n) < 2 or len(n) > 60 or n.endswith(":") or n.isdigit():
        return False
    if sum(c.isalpha() for c in n) < 2:
        return False                      # "-----", "24", "%%%"
    if re.search(r"\b(tagged version|shnid|special series|transfer|remaster|"
                 r"\d+\s*bit|16bit|24bit)\b", n, re.IGNORECASE):
        return False
    if re.search(r'[<>={}?"\\]|xml\s*version|encoding=', n, re.IGNORECASE):
        return False   # markup / XML declarations masquerading as tags
    if re.search(r"\b\d+\.\d+(\.\d+)?\b", n):
        return False   # version numbers: "shntool 3.0.4", "v2.1"
    if re.search(r"\b(shntool|shorten|exact audio copy|\beac\b|xact|foobar2000|"
                 r"audacity|cool edit|sound ?forge|cd ?wave|trader'?s little helper|"
                 r"flac frontend|dbpoweramp|audiochecker)\b", n, re.IGNORECASE):
        return False   # ripping/tagging tool signatures
    return re.sub(r"[^a-z ]", "", n.lower()).strip() not in _JUNK_ARTIST_VALUES


_LIVE_TAIL_RE = re.compile(
    r"\b(live|in session|in concert|unplugged|acoustic|bootleg|broadcast|"
    r"radio|sessions?|show|tour|at |on |from )", re.IGNORECASE)


def clean_artist(name: str) -> str:
    """Strip release-title debris from an artist string:
    'The Eels - Live In Session On KCRW 2008' → 'The Eels'
    'Artist- Stevie Ray Vaughan' → 'Stevie Ray Vaughan'
    'JOHN MAYER 3-30-01' → 'John Mayer' · '1-31-05 - Steve Kimock Band' → same"""
    n = fix_text((name or "").strip())
    n = re.sub(r"^\s*(artist|band|performer)s?\s*[:\-\u2013]\s*", "", n,
               flags=re.IGNORECASE)                          # label prefixes
    n = re.sub(r"\s*[(\[].*?[)\]]\s*$", "", n).strip()      # trailing (...) / [...]
    # dates glued to the front or back of the name
    n = re.sub(r"^\s*\d{1,4}[-/._]\d{1,2}[-/._]\d{1,4}\s*[-\u2013]*\s*", "", n)
    n = re.sub(r"\s*[-\u2013]*\s*\d{1,4}[-/._]\d{1,2}[-/._]\d{1,4}\s*$", "", n)
    # ALL-CAPS names read better title-cased
    if n.isupper() and len(n) > 4:
        n = n.title()
    if " - " in n:
        head, tail = n.split(" - ", 1)
        if _LIVE_TAIL_RE.search(tail) or extract_date(tail) \
                or re.search(r"\b(19|20)\d{2}\b", tail):
            n = head.strip()
    n = re.sub(r"\s+\b(19|20)\d{2}\b$", "", n).strip()      # trailing bare year
    return n.rstrip(" -–·|") or name.strip()


_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def _looks_like_venue_name(s: str) -> bool:
    """A short line of words, not a sentence, a date, a source or a setlist."""
    s = (s or "").strip()
    if not (2 <= len(s) <= 60) or extract_date(s) or s.endswith((".", ":", "?", "!")):
        return False
    if re.match(r"^(with|w/|feat\.?|featuring|and|plus|special guests?)\b", s, re.IGNORECASE) \
            or re.search(r"\btour\b", s, re.IGNORECASE):
        return False
    if re.search(r"\b(set ?\d|encore|disc|cd ?\d|source|lineage|taper|transfer|recorded|"
                 r"mics?|row|section|seat|flac|wav|shn|sbd|aud|bit|khz)\b", s, re.IGNORECASE):
        return False
    return len(s.split()) <= 7 and sum(c.isdigit() for c in s) <= 3


def normalise_state(raw: str) -> str:
    """'California.' → 'CA' · 'Ontario' → 'ON' · 'NY' → 'NY' ·
    'Deutschland' → 'Germany' · 'England' → 'United Kingdom'.
    Two-letter codes stay as they are; places.resolve() decides those once
    the city is known."""
    s = (raw or "").strip().strip(".").strip()
    if s.lower() in _US_STATES:
        return _US_STATES[s.lower()]
    try:
        from . import places
        return places.normalise(s)
    except Exception:
        return s


_SKIP_INFO_SUFFIXES = re.compile(r"\.(md5|ffp|sfv|m3u|cue|log|accurip)(\.|$)", re.IGNORECASE)


_INFO_SKIP_STEM = re.compile(
    r"(shntool|shn[-_ ]?len|_len\b|\bffp\b|md5|st5|checksums?|fingerprints?|"
    r"audiochecker|accurip|dr\d*[-_ ]?analysis|foobar)", re.IGNORECASE)


def parse_best_info(show_dir: Path, known_venues: frozenset = frozenset()):
    """Parse every plausible info file and keep the richest result.
    Alphabetical order used to pick 'x.shntool_len.txt' over the real
    'x.txt' — tool reports are now excluded and candidates are ranked."""
    for name in ("info.txt", "README.txt", "readme.txt"):
        pth = show_dir / name
        if pth.exists():
            return parse_info_file(pth, known_venues), pth
    best, best_path, best_score = ShowMeta(), None, 0.0
    try:
        candidates = sorted(show_dir.iterdir())
    except OSError:
        return best, None
    for pth in candidates:
        if not (pth.is_file() and pth.suffix.lower() in (".txt", ".md", ".nfo")):
            continue
        if _SKIP_INFO_SUFFIXES.search(pth.name) or _INFO_SKIP_STEM.search(pth.stem):
            continue
        m = parse_info_file(pth, known_venues)
        score = ((2.0 if m.date else 0) + (2.0 if m.venue else 0)
                 + (1.0 if m.city else 0) + (1.0 if m.artist else 0)
                 + (1.0 if m.source_type else 0) + min(len(m.tracks), 10) * 0.5)
        if score > best_score:
            best, best_path, best_score = m, pth, score
    return best, best_path


def find_info_file(show_dir: Path) -> Optional[Path]:
    for name in ("info.txt", "README.txt", "readme.txt"):
        p = show_dir / name
        if p.exists():
            return p
    for p in sorted(show_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in (".txt", ".md", ".nfo") \
                and not _SKIP_INFO_SUFFIXES.search(p.name):
            return p
    return None


_PURE_CITY_RE = re.compile(r"^[A-Za-z .'\-]+,\s*[A-Z]{2}\.?$")


def _strict_venue_line(s: str, known_venues: frozenset) -> bool:
    """Only lines we're CONFIDENT are venues block artist detection:
    a known venue from the library index, or venue-keyword + ', ST' suffix.
    'The Decemberists and the Grant Park Symphony Orchestra' has the keyword
    'park' but is NOT a venue line — the old keyword-only test ate it."""
    if re.sub(r"[^a-z0-9]", "", s.lower()) in known_venues:
        return True
    return bool(_VENUE_KEYWORDS.search(s) and re.search(r",\s*[A-Z]{2}\.?$", s))


def parse_info_file(path: Path, known_venues: frozenset = frozenset()) -> ShowMeta:
    meta = ShowMeta()
    try:
        raw_text = path.read_text(errors="replace")
        lines = raw_text.splitlines()
    except Exception:
        return meta

    header = [l for l in lines[:15] if l.strip()]
    current_disc = 1

    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not meta.date:
            meta.date = extract_date(s)
        src = _SOURCE_RE.match(s)
        if src:
            raw = src.group(1).strip()
            if not meta.source_type:
                meta.source_type = normalise_source_type(raw)
            if not meta.notes:
                meta.notes = raw
        if not meta.shnid:
            meta.shnid = extract_shnid(s)
        if not meta.recorder:
            meta.recorder = extract_recorder(s)
        if not meta.official:
            meta.official = extract_official(s, allow_generic=False)

    # The classic info-file layout is Artist / Date / Venue / City, ST. A venue
    # with no tell-tale word ("Paradiso", "Nectar's") is still the line just
    # above the City, ST line, so remember the last line nothing else claimed.
    loose = (-2, "")
    for idx, s in enumerate(l.strip() for l in header):
        if extract_date(s) == meta.date and meta.date and len(s) < 14:
            continue
        if _SOURCE_RE.match(s) or _TRACK_RE.match(s) or _SECTION_HEADER_RE.match(s):
            continue
        if re.match(r"^\s*mics?\s*(location|position|placement)?\s*[:\-\u2013]",
                    s, re.IGNORECASE) or _MIC_POSITION_RE.search(s):
            continue          # taper mic-placement notes are never geography
        # explicitly labeled lines: "Venue: X" / "Location: City, ST"
        lm = re.match(r"^(venue|place|site)\s*[:\-\u2013]\s*(.+)$", s, re.IGNORECASE)
        if lm:
            if not meta.venue:
                val = lm.group(2).strip()
                parts = [x.strip() for x in val.split(",")]
                if len(parts) >= 3 and re.match(r"^[A-Z]{2}\.?$", parts[-1]):
                    meta.venue = ", ".join(parts[:-2])
                    meta.city, meta.state = parts[-2], normalise_state(parts[-1])
                else:
                    meta.venue = val
            continue
        lm = re.match(r"^(location|city|where)\s*[:\-\u2013]\s*(.+)$", s, re.IGNORECASE)
        if lm:
            val = lm.group(2).strip()
            # "Location: Dead Center, Back of Second Floor Level" is a MIC
            # position, not geography — only accept a real City, ST shape
            parts = [x.strip() for x in val.split(",")]
            plausible = (not _MIC_POSITION_RE.search(val)
                         and (re.match(r"^[A-Z]{2}\.?$", parts[-1])
                              or parts[-1].lower().strip(".") in _US_STATES))
            if plausible:
                if len(parts) >= 3 and not meta.venue:
                    meta.venue = ", ".join(parts[:-2])
                if len(parts) >= 2 and not meta.city:
                    meta.city, meta.state = parts[-2], normalise_state(parts[-1])
            continue
        if not meta.artist and len(s) < 60 and plausible_artist(s) \
                and not _strict_venue_line(s, known_venues) \
                and not _PURE_CITY_RE.match(s):
            meta.artist = s
            continue
        if not meta.venue and (_VENUE_KEYWORDS.search(s)
                               or re.sub(r"[^a-z0-9]", "", s.lower()) in known_venues):
            # venue line may embed city/state: "Barton Hall, Ithaca, NY"
            parts = [p.strip() for p in s.split(",")]
            if len(parts) >= 3 and re.match(r"^[A-Z]{2}$", parts[-1]):
                meta.venue = ", ".join(parts[:-2])
                meta.city, meta.state = parts[-2], parts[-1]
            else:
                meta.venue = s
            continue
        if not meta.city:
            m = _CITY_STATE_RE.match(s)
            if m and not _VENUE_KEYWORDS.search(s) and len(s) < 60:
                head, st = m.group(1).strip(), m.group(2).strip()
                if "," in head and not meta.venue:
                    # "Portland Meadows, Portland, OR" — venue lacks a keyword
                    v, c = head.rsplit(",", 1)
                    meta.venue, meta.city = v.strip(), c.strip()
                else:
                    meta.city = head
                    if not meta.venue and loose[0] == idx - 1 and _looks_like_venue_name(loose[1]):
                        meta.venue = loose[1]
                meta.state = st
                continue
        if s:
            loose = (idx, s)

    tracknum = 0
    set_no = 1
    for line in lines:
        s = line.strip()
        if _SET_RE.match(s):
            if "encore" in s.lower():
                current_disc = 99
            if meta.tracks:                  # a heading after songs starts the next set
                set_no += 1
            continue
        m = _TRACK_RE.match(s)
        if m and len(m.group(2).strip()) > 1:
            title = clean_track_title(m.group(2).strip())
            if not title or is_junk_track_title(title):
                continue
            tracknum += 1
            meta.tracks.append({"num": int(m.group(1)), "title": title,
                                "disc": current_disc, "set": set_no})

    # whole-file source inference: combines mic/soundboard/broadcast evidence
    # across every line — a "Taping Rig:" block implies AUD even with no
    # "Source:" line, and SBD + mics anywhere in the notes means MTX.
    meta.source_explicit = bool(meta.source_type)   # a Source:/Lineage: line said so
    inferred = infer_source_type(raw_text)
    if inferred:
        meta.source_type = inferred
        if not meta.source_explicit:
            meta.source_inferred = True
    return meta


# ── Folder-name parsers ──────────────────────────────────────────────────────

_ETREE_RE = re.compile(
    r"^([a-zA-Z][a-zA-Z\- ]*?)[-_.]?(\d{4}|\d{2})[-._](\d{2})[-._](\d{2})\b")
_SKIP_TOKENS = re.compile(
    r"^(sbd|aud|matrix|mtx|fm|flac|flac16|flac24|shnf?|mp3|ogg|"
    r"set[12]|d[12]|t-?flac|sbeok|sbefail|unknown|complete|"
    r"patch|fixed|remaster|retrack|remix|converted|transfer|"
    r"mono|stereo|partial|incomplete|[a-z]{1,2})$", re.IGNORECASE)


def parse_folder_name(folder_name: str, aliases: dict) -> ShowMeta:
    """Handles both etree dot-style and descriptive names."""
    meta = ShowMeta()
    name = folder_name.strip()

    meta.official = extract_official(name, allow_generic=False)
    if not meta.source_type:
        m_src = re.search(r"\b(d?sbd|sdb|soundboard|matrix|mtx|fob|aud|audience|fm)\b",
                          name, re.IGNORECASE)
        if m_src:
            meta.source_type = normalise_source_type(m_src.group(1))

    # bracket suffix on descriptive names
    bm = re.search(r"\[([^\]]*)\]\s*$", name)
    if bm:
        meta.source_type = normalise_source_type(bm.group(1))
        if not meta.official:
            meta.official = extract_official(bm.group(1))
        shn = re.search(r"\b(\d{4,7})\b", bm.group(1))
        if shn:
            meta.shnid = _pad_shnid(shn.group(1))
        name = name[:bm.start()].strip()

    m = _ETREE_RE.match(name)
    if not m:
        # date may appear later: "Some Show - 1994-07-16 ..."
        meta.date = extract_date(name)
        return meta

    raw_artist = m.group(1).replace("-", " ").replace("_", " ").strip()
    key = re.sub(r"[^a-z0-9]", "", raw_artist.lower())
    meta.artist = aliases.get(key, raw_artist.title() if raw_artist else "")

    year = m.group(2)
    if len(year) == 2:
        year = ("19" if int(year) > 40 else "20") + year
    meta.date = f"{year}-{m.group(3)}-{m.group(4)}"

    remainder = name[m.end():].lstrip("-_. ")

    if "." in remainder and "," not in remainder:
        # etree dot fields: source.taper.shnid.filetype
        recorder_candidates = []
        for fld in (f for f in remainder.split(".") if f):
            if not meta.source_type and normalise_source_type(fld):
                meta.source_type = normalise_source_type(fld)
                continue
            if not meta.shnid and re.match(r"^\d{4,7}$", fld):
                meta.shnid = _pad_shnid(fld)
                continue
            if _SKIP_TOKENS.match(fld):
                continue
            if re.match(r"^[a-zA-Z][a-zA-Z\-']{1,}$", fld):
                recorder_candidates.append(fld)
        if recorder_candidates and not meta.recorder:
            meta.recorder = name_case(recorder_candidates[-1])
    elif remainder:
        # descriptive: "Venue, City, ST" — but a hybrid name can carry trailing
        # etree dot-fields ("Chevy Court-Syracuse,NY.fob.schoeps.mk4.dat...").
        # Cut those off at the first dotted source/format token so the location
        # parser sees only the "Venue, City, ST" part.
        loc_part = remainder
        dot_tail = re.search(r"\.(fob|d?sbd|sdb|aud|mtx|matrix|soundboard|fm|shn|"
                             r"flac\d*|wav|mp3|shnf|16bit|24bit|\d{2,3}k)\b",
                             remainder, re.IGNORECASE)
        if dot_tail:
            # pull source/recorder/shnid from the dotted tail, keep loc clean
            tail = remainder[dot_tail.start():]
            loc_part = remainder[:dot_tail.start()].strip()
            for fld in (f for f in tail.split(".") if f):
                if not meta.source_type and normalise_source_type(fld):
                    meta.source_type = normalise_source_type(fld)
                elif not meta.shnid and re.match(r"^\d{4,7}$", fld):
                    meta.shnid = _pad_shnid(fld)
        loc, _ = parse_album_string(loc_part)
        if loc.venue and _SKIP_TOKENS.match(loc.venue.replace(" ", "")):
            loc.venue = ""           # format tokens (flac16, shnf...) aren't venues
        meta.venue, meta.city, meta.state = loc.venue, loc.city, loc.state
        if not meta.venue and not meta.city and loc_part \
                and not _SKIP_TOKENS.match(loc_part.replace(" ", "")):
            meta.venue = loc_part
    return meta


# ── Filename source ──────────────────────────────────────────────────────────
# Track filenames carry real metadata: "gd77-05-08.sbd.miller.d1t01.flac"
# gives artist/date/source/taper; "02 - Roll Over Beethoven.flac" gives titles.

def parse_filenames(show_dir: Path, aliases: dict) -> ShowMeta:
    import os
    stems = [p.stem for p in sorted(
        q for q in show_dir.rglob("*")
        if q.suffix.lower() in AUDIO_EXTS and q.is_file())]
    if not stems:
        return ShowMeta()

    # artist / date / source tokens from the shared stem prefix
    meta = parse_folder_name(stems[0], aliases)
    meta.venue = meta.city = meta.state = ""    # filenames don't carry locations reliably

    # per-track titles from the varying part of each name
    common = os.path.commonprefix(stems) if len(stems) > 1 else ""
    titles = []
    for i, s in enumerate(stems, 1):
        rem = s[len(common):] if common and len(common) < len(s) else s
        rem = re.sub(r"^[\s\-_.]*(?:d\d{1,2})?[\s\-_.]*t?\d{1,3}[\s\-_.)]*",
                     "", rem, flags=re.IGNORECASE)
        t = clean_track_title(fix_text(rem.replace("_", " ").strip(" -–_.")))
        if (len(t) >= 2 and re.search(r"[A-Za-z]{2}", t)
                and not is_junk_track_title(t) and not t.isdigit()
                and not re.match(r"^(track|audio|untitled)\b", t, re.IGNORECASE)):
            titles.append({"num": i, "title": t, "disc": 1})

    # only trust filename titles when EVERY file yields one (else numbering
    # misaligns) and they aren't all the same string
    if len(titles) == len(stems) and \
            len({t["title"].lower() for t in titles}) > 1:
        meta.tracks = titles
    return meta


# ── Sidecar ──────────────────────────────────────────────────────────────────

SIDECAR_NAMES = (".reelarr.json", ".barbosa.json")


def read_sidecar(show_dir: Path) -> dict:
    """Provenance a provider left beside a show it delivered, e.g.
    {"source": "SBD", "official": "Official", "release_id": "12345"}."""
    for name in SIDECAR_NAMES:
        p = show_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return {}
    return {}


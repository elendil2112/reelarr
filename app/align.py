"""Which audio file gets which song title.

A setlist (setlist.fm, an info file) lists songs. A recording has files —
and often more files than songs: a tuning track before the first song, crowd
noise at the end, a set break, an encore break. Pairing titles to files by
position alone shifts every title by one.

So before pairing, the files that aren't songs are set aside:

  1. a file whose own title or filename says so — "Tuning", "Crowd", "Set
     Break", "Banter"… (unless the setlist itself lists that title);
  2. otherwise, a short file (under 90 s, and well under a typical track
     here) at the start, at the end, or where one disc/set gives way to the
     next — and only when exactly that many such files are left over.

Songs are then paired with the remaining files in order — but only when the
counts come out exactly even. Anything less certain is reported as not
settled, and the show is held for Review instead of filed with shifted titles.
A file set aside gets a title by where it sits: Tuning at the start, Set Break
between sets, Encore Break before the encore, Crowd at the end — or keeps the
label it already had.
"""
import re
from pathlib import Path

SHORT_SECONDS = 90          # a non-song is shorter than this…
SHORT_FRACTION = 0.4        # …and shorter than this share of the median track
MAX_EXTRAS = 4              # more surplus files than this: the setlist is probably wrong

_FILLER = re.compile(
    r"^(?:the\s+)?(?:band\s+)?(?:"
    r"tun(?:e|ing)(?:[\s-]*up)?|intro(?:duction)?s?|crowd(?:\s+noise)?|applause|cheering|"
    r"(?:stage\s+)?banter|chatter|talk(?:ing)?|stage\s+announcements?|announcements?|"
    r"set\s*break|encore\s+break|encore\s+call|between\s+sets|"
    r"sound\s*check|warm[\s-]*up|noodl\w*|dead\s+air|mc|outro|pre[\s-]*show|post[\s-]*show|"
    r"silence|filler"
    r")(?:\b|$)"
    r"(?:\s*(?:[/&+,]|and|>)\s*"
    r"(?:tun(?:e|ing)|intro|crowd|banter|applause|talk|chatter|noodling|set\s*break|encore\s+break))*"
    r"\s*$",
    re.IGNORECASE)

# a file name's track number: d1t05, s2t05, t05, "track 05", or 05 at the start/end
_TRACKNUM = [
    re.compile(r"(?:d\d{1,2}|s\d|cd\d{1,2})t(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?:^|[^a-z])t(?:rack)?[\s._-]*(\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"^(\d{1,3})(?=[\s._-])"),
    re.compile(r"[\s_][-–]?\s*(\d{1,3})$"),            # "Loser - 05", not the 08 of a date
]
_DISC = re.compile(r"(?:^|[^a-z])(?:d|cd|disc\s*|s)(\d{1,2})t\d", re.IGNORECASE)


def is_filler(title: str) -> bool:
    """'Tuning', 'Crowd', 'Set Break', 'Tuning/Banter', 'Band Intros' — not songs."""
    return bool(title) and bool(_FILLER.match(title.strip(" -–—*>~")))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def own_label(path: Path, tag_title: str = "") -> str:
    """What the file itself says it is: its TITLE tag, else its name with the
    numbering stripped ('gd77-05-08d1t01 Tuning' → 'Tuning')."""
    if tag_title and tag_title.strip():
        return tag_title.strip()
    stem = path.stem
    stem = re.sub(r"^[a-z]{1,6}\d{2,4}[-._]\d{1,2}[-._]\d{1,2}", "", stem, flags=re.IGNORECASE)  # gd77-05-08
    stem = re.sub(r"(?:d|cd|s)\d{1,2}t\d{1,3}", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^\s*(?:(?:d|cd|s)\d{1,2})?(?:t|track\s*)?\d{1,3}(?:[\s._-]+|$)", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[._]+", " ", stem)
    return stem.strip(" -–")


def disc_of(path: Path) -> int:
    m = _DISC.search(path.stem)
    if m:
        return int(m.group(1))
    m = re.match(r"(?:cd|disc|disk|set)\s*(\d{1,2})$", path.parent.name, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def track_number(path: Path):
    for rx in _TRACKNUM:
        m = rx.search(path.stem)
        if m:
            return int(m.group(1))
    return None


def duration(path: Path):
    try:
        from mutagen import File as MutagenFile
        f = MutagenFile(path)
        if f is not None and f.info and getattr(f.info, "length", 0):
            return float(f.info.length)
    except Exception:
        pass
    try:
        from .audioprobe import _duration
        return _duration(path)
    except Exception:
        return None


def _tag_title(path: Path) -> str:
    try:
        from mutagen import File as MutagenFile
        f = MutagenFile(path, easy=True)
        if f and f.get("title"):
            return str(f.get("title")[0])
    except Exception:
        pass
    return ""


def describe(files: list) -> list:
    """[{path, name, seconds, label, disc}] for each file, read from disk."""
    out = []
    for p in files:
        out.append({"path": p, "name": p.name, "seconds": duration(p),
                    "label": own_label(p, _tag_title(p)), "disc": disc_of(p)})
    return out


def _median(xs):
    xs = sorted(x for x in xs if x)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _fmt(sec) -> str:
    if sec is None:
        return "?"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def plan(files: list, tracks: list, extras=None, info=None) -> dict:
    """Pair setlist titles with files.

    files   — audio paths in play order
    tracks  — the setlist: [{num, title, disc, set?}]
    extras  — file names (or relative paths) you've marked "not a song"
    info    — describe(files), if already read

    Returns {entries: [{name, seconds, kind: song|extra, title, track}],
             ok, note, extras: [names], how}.
    """
    info = info if info is not None else describe(files)
    n_files, n_songs = len(info), len(tracks)
    # extras: None = decide for me. A list = your choice in Review (it may be
    # just [""] — "none of these are extras").
    yours = extras is not None and len(extras) > 0
    mine = {str(x) for x in (extras or []) if str(x)}
    setlist_titles = {_norm(t.get("title", "")) for t in tracks}

    def result(extra_idx, ok, note, how):
        entries, songs = [], iter(tracks)
        for i, f in enumerate(info):
            if i in extra_idx:
                entries.append({"name": f["name"], "seconds": f["seconds"], "kind": "extra",
                                "title": "", "track": None})
            else:
                t = next(songs, None)
                entries.append({"name": f["name"], "seconds": f["seconds"],
                                "kind": "song" if t else "unmatched",
                                "title": t["title"] if t else "", "track": t})
        _label_extras(entries, info)
        return {"entries": entries, "ok": ok, "note": note, "how": how,
                "extras": [info[i]["name"] for i in sorted(extra_idx)]}

    if not tracks or not info:
        return {"entries": [{"name": f["name"], "seconds": f["seconds"], "kind": "unmatched",
                             "title": "", "track": None} for f in info],
                "ok": not tracks, "note": "", "how": "none", "extras": []}

    # your own choices in Review come first
    if yours:
        idx = {i for i, f in enumerate(info)
               if f["name"] in mine or str(f["path"]).endswith(tuple("/" + m for m in mine))}
        left = n_files - len(idx)
        if left == n_songs:
            return result(idx, True, "", "yours")
        return result(idx, False, f"{left} files left as songs but the setlist has {n_songs} — "
                                  "mark or unmark files until they match", "yours")

    if n_files == n_songs:
        return result(set(), True, "", "count")

    surplus = n_files - n_songs
    if surplus < 0:
        return _by_number(info, tracks, f"{n_files} files but the setlist has {n_songs} songs — "
                                        "the setlist may be for a different recording, or files are missing")
    if surplus > MAX_EXTRAS:
        return _by_number(info, tracks, f"{n_files} files but the setlist has only {n_songs} songs — "
                                        "it may be incomplete")

    # 1) files that say they aren't songs (and the setlist doesn't list them as songs)
    named = {i for i, f in enumerate(info)
             if is_filler(f["label"]) and _norm(f["label"]) not in setlist_titles}
    if len(named) == surplus:
        return result(named, True, "", "named")
    if len(named) > surplus:
        return result(set(sorted(named)[:surplus]), False,
                      f"{len(named)} files look like tuning/crowd tracks but only {surplus} "
                      f"{'is' if surplus == 1 else 'are'} extra — check which are songs", "named")

    # 2) short files where a non-song would sit: start, end, either side of a disc change
    need = surplus - len(named)
    med = _median(f["seconds"] for f in info)
    songlike = {i for i, f in enumerate(info) if _norm(f["label"]) in setlist_titles}
    edges = {0, n_files - 1}
    for i in range(1, n_files):
        if info[i]["disc"] != info[i - 1]["disc"]:
            edges |= {i - 1, i}
    # files already set aside move the edge inward: crowd → tuning → song
    for i in sorted(named):
        edges |= {i - 1, i + 1}
    short = [i for i in sorted(edges - named - songlike)
             if 0 <= i < n_files and info[i]["seconds"] is not None and med
             and info[i]["seconds"] < SHORT_SECONDS and info[i]["seconds"] < SHORT_FRACTION * med]
    if len(short) == need:
        return result(named | set(short), True, "", "short")
    names = ", ".join(f"{info[i]['name']} ({_fmt(info[i]['seconds'])})" for i in short) or "none"
    if len(short) > need:
        guess = sorted(short, key=lambda i: info[i]["seconds"])[:need]
        return result(named | set(guess), False,
                      f"{n_files} files vs {n_songs} songs — more than one short track could be the "
                      f"extra one ({names})", "short")
    return result(named | set(short), False,
                  f"{n_files} files vs {n_songs} songs — couldn't tell which "
                  f"{'file isn’t a song' if surplus == 1 else f'{surplus} files aren’t songs'}", "none")


def _by_number(info: list, tracks: list, note: str) -> dict:
    """Last resort when the counts can't be reconciled: a real track number in
    the file name (t05, '05 -', …) — never digits from a date or a disc."""
    by_num = {}
    for t in tracks:
        by_num.setdefault(t.get("num"), t)
    entries = []
    for f in info:
        n = track_number(f["path"])
        t = by_num.get(n) if n is not None else None
        entries.append({"name": f["name"], "seconds": f["seconds"],
                        "kind": "song" if t else "unmatched", "title": t["title"] if t else "",
                        "track": t})
    return {"entries": entries, "ok": False, "note": note, "how": "number", "extras": []}


def _label_extras(entries: list, info: list):
    """Title each non-song by where it sits, unless the file already says what it is."""
    n = len(entries)
    for i, e in enumerate(entries):
        if e["kind"] != "extra":
            continue
        own = info[i]["label"]
        if is_filler(own):
            e["title"] = _tidy(own)
            continue
        before = next((entries[j] for j in range(i - 1, -1, -1) if entries[j]["kind"] == "song"), None)
        after = next((entries[j] for j in range(i + 1, n) if entries[j]["kind"] == "song"), None)
        if before is None:
            e["title"] = "Tuning"
        elif after is None:
            e["title"] = "Crowd"
        else:
            bt, at = before["track"] or {}, after["track"] or {}
            if at.get("disc") == 99 and bt.get("disc") != 99:
                e["title"] = "Encore Break"
            elif (at.get("set", 0) != bt.get("set", 0)) or (at.get("disc", 1) != bt.get("disc", 1)) \
                    or info[i]["disc"] != info[max(i - 1, 0)]["disc"] \
                    or (i + 1 < n and info[i]["disc"] != info[i + 1]["disc"]):
                e["title"] = "Set Break"
            else:
                e["title"] = "Tuning"


def _tidy(label: str) -> str:
    s = label.strip(" -–—*>~")
    return s[:1].upper() + s[1:] if s.islower() else s


def summary(p: dict) -> str:
    """'gd77-05-08d1t01.flac (0:20) → Tuning' for the Activity log."""
    return "; ".join(f"{e['name']} ({_fmt(e['seconds'])}) → {e['title']}"
                     for e in p["entries"] if e["kind"] == "extra")

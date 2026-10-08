"""
Reelarr pipeline: identify → enrich → score → tag → file.

Metadata sources in trust order (per field), each tracked for provenance:
    sidecar > curated tags > info file > setlist.fm > internet archive > tags > folder

Confidence scoring (0-100):
    presence:  artist 20, date 20, venue 15, city 10, state 10, source_type 5, tracks 5
    bonuses:   curated album tag +10, official sidecar +10,
               date agreed by 2+ sources +5, artist agreed +3, venue agreed +5,
               external source confirmed artist+date +5, artist matched to library +5
Auto-file when score >= threshold AND artist/date/venue/city/state all present.
"""
import difflib
import json
import tarfile
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TPOS,
                         TRCK, TDRC, TCON)

from . import config, database as db, fileops, perms, sources, library_index, audioprobe
from .metadata import (ShowMeta, AUDIO_EXTS, TAGGABLE_EXTS, parse_folder_name, read_embedded_tags, read_sidecar,
                       parse_best_info, parse_filenames,
                       clean_artist,
                       normalise_state, fix_text, clean_track_title, sanitize_location,
                       infer_source_type,
                       is_junk_track_title)

REQUIRED_FIELDS = ("artist", "date", "venue", "city", "state")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_STOP_WORDS = {"the", "and", "band", "of", "a"}


def _norm_words(s: str) -> set:
    """Significant lowercase word tokens, dropping noise words like 'the' so
    two unrelated bands aren't judged similar just for both starting with it."""
    words = re.findall(r"[a-z0-9]+", (s or "").lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 1}


# ── Artist canonicalization against the real library ────────────────────────

def canonical_artist(name: str, library_dir: Path, cfg: dict):
    """Map a parsed artist name onto an existing library folder.
    Returns (canonical_name, matched_existing: bool)."""
    if not name:
        return name, False
    aliases = cfg["artists"]["aliases"]
    alias_hit = aliases.get(_norm(name))
    if alias_hit:
        name = alias_hit

    try:
        existing = [d.name for d in library_dir.iterdir() if d.is_dir()
                    and not d.name.startswith((".", "_"))]
    except Exception:
        existing = []
    by_norm = dict(library_index.known_artists())   # tags-derived names
    by_norm.update({_norm(d): d for d in existing})  # live folders win

    if _norm(name) in by_norm:
        return by_norm[_norm(name)], True
    # try with/without leading "the"
    n = _norm(name)
    for candidate in (n.removeprefix("the"), "the" + n):
        if candidate in by_norm:
            return by_norm[candidate], True
    # fuzzy
    match = difflib.get_close_matches(
        _norm(name), list(by_norm), n=1, cutoff=float(cfg["artists"]["match_cutoff"]))
    if match:
        return by_norm[match[0]], True
    return name, False


# ── Host-artist derivation for collabs & guests ─────────────────────────────
# "The Decemberists with the Atlanta Symphony Orchestra" → host "The Decemberists"
# Album Artist = host; the full credit stays in the Artist tag.

_UNCONDITIONAL_SEPS = [" with the ", " with ", " w/ ", " feat. ", " featuring ",
                       " feat ", " ft. ", " ft "]
_CONDITIONAL_SEPS = [" and the ", " & ", " + ", " and "]   # part of many band names
_GUEST_ENSEMBLE_RE = re.compile(
    r"\b(orchestra|symphony|philharmonic|philharmonia|choir|chorus|"
    r"string quartet|horns|big band|ensemble)\b", re.IGNORECASE)


def derive_host_artist(artist: str, library_dir: Path, cfg: dict):
    """Returns (host_artist, host_in_library). Empty host = no split applies."""
    if not artist:
        return "", False
    # the library already taught us this exact credit ("Phish w/ Billy Strings")
    learned = library_index.host_for(artist)
    if learned:
        return learned, True
    # the full name IS a known act ("Dead & Company") — never split it
    canon, hit = canonical_artist(artist, library_dir, cfg)
    if hit:
        return "", True

    low = artist.lower()

    def try_head(sep, require_confirmation):
        idx = low.find(sep)
        if idx < 3:
            return None
        head, tail = artist[:idx].strip(" -"), artist[idx + len(sep):]
        if len(head) < 3:
            return None
        c, in_lib = canonical_artist(head, library_dir, cfg)
        if in_lib:
            return c, True
        if not require_confirmation or _GUEST_ENSEMBLE_RE.search(tail):
            return head, False
        return None

    for sep in _UNCONDITIONAL_SEPS:
        r = try_head(sep, require_confirmation=False)
        if r:
            return r
    for sep in _CONDITIONAL_SEPS:
        r = try_head(sep, require_confirmation=True)
        if r:
            return r
    return "", False


# ── Merge with provenance ────────────────────────────────────────────────────

def merge(sources_by_name: dict) -> tuple:
    """sources_by_name: {source_name: ShowMeta}. Priority = dict insertion order.
    Returns (merged ShowMeta, provenance {field: source}, agreement {field: n})."""
    merged = ShowMeta()
    provenance = {}
    agreement = {}

    # Raw embedded DATE tags often carry rip/release dates, not the show date —
    # for the date field alone, the folder name outranks non-curated tags.
    _DATE_ORDER = ["sidecar", "curated_tags", "info_file", "setlistfm",
                   "archive_org", "folder_name", "filenames", "tags"]

    for f in ShowMeta.FIELDS:
        values = {}
        for src, meta in sources_by_name.items():
            v = getattr(meta, f, "")
            if v:
                values[src] = v
        if not values:
            continue
        if f == "date":
            first_src = next(s for s in _DATE_ORDER if s in values)
        else:
            first_src = next(iter(values))
        setattr(merged, f, values[first_src])
        if f == "recorder":
            # the same name from several places: keep the one typed with care
            # ("TinyDancer" over a folder name's "tinydancer")
            chosen = values[first_src]
            cased = [v for v in values.values() if v.lower() == chosen.lower()
                     and v not in (v.lower(), v.upper(), v.title())]
            if cased:
                setattr(merged, f, cased[0])
        provenance[f] = first_src
        norm_vals = [_norm(v)[:24] for v in values.values()]
        agreement[f] = max(norm_vals.count(x) for x in set(norm_vals))

    for src, meta in sources_by_name.items():
        if meta.tracks:
            merged.tracks = meta.tracks
            provenance["tracks"] = src
            break
    for src, meta in sources_by_name.items():
        if meta.notes and not merged.notes:
            merged.notes = meta.notes
    return merged, provenance, agreement


def score(meta: ShowMeta, agreement: dict, curated: bool, sidecar: dict,
          external_confirmed: bool, artist_in_library: bool,
          artist_strong: bool = True) -> int:
    s = 0
    # an artist nobody corroborates (single weak parse, unknown to the library,
    # unconfirmed externally) can't carry full weight — keeps junk under threshold
    s += (20 if artist_strong else 12) if meta.artist else 0
    s += 20 if meta.date else 0
    s += 15 if meta.venue else 0
    s += 10 if meta.city else 0
    s += 10 if meta.state else 0
    s += 5 if meta.source_type else 0
    s += 5 if meta.tracks else 0
    if curated:
        s += 10
    if sidecar.get("official"):
        s += 10
    if agreement.get("date", 0) >= 2:
        s += 5
    if agreement.get("artist", 0) >= 2:
        s += 3
    if agreement.get("venue", 0) >= 2:
        s += 5
    if external_confirmed:
        s += 5
    if artist_in_library:
        s += 5
    return min(s, 100)


# ── Junk-file scrub: torrents, json metadata, spectrograms ──────────────────

_SPECTRAL_RE = re.compile(r"spectr|spectrum|\bspek\b|spectral|\bsox\b|"
                          r"frequency|freq[-_. ]analysis", re.IGNORECASE)


def scrub_junk(show_dir: Path, log, b: "fileops.Batch") -> int:
    """Remove trade debris from a show folder: .torrent, .json, .json.gz,
    and spectrogram .png files. Never touches the provenance sidecar
    (still needed during identify) or album-art images."""
    audio_stems = set()
    for a in show_dir.rglob("*"):
        if a.suffix.lower() in AUDIO_EXTS and a.is_file():
            audio_stems.add(a.stem.lower())
            audio_stems.add(a.name.lower())          # "t01.flac" -> t01.flac.png
    removed = []
    for p in list(show_dir.rglob("*")):
        if not p.is_file():
            continue
        n = p.name.lower()
        if n in (".reelarr.json", ".barbosa.json"):
            continue
        kill = False
        if n.endswith((".torrent", ".json.gz", ".json")):
            kill = True
        elif n.endswith(".png") and n not in _ART_FILES:
            base = n[:-4]
            if _SPECTRAL_RE.search(n) or base in audio_stems \
                    or re.sub(r"\.(flac|shn|wav|mp3)$", "", base) in audio_stems:
                kill = True
        if kill:
            try:
                b.trash(p, "trade debris")
                removed.append(p.name)
            except OSError:
                pass
    if removed:
        log("info", "scrub",
            f"{'would clear' if b.dry_run else 'cleared'} {len(removed)} junk file(s) to trash: "
            + ", ".join(removed[:6]) + ("..." if len(removed) > 6 else ""))
    return len(removed)


# ── Format de-duplication: FLAC > WAV > SHN > OGG > MP3 > AFPK ───────────────

# Higher wins. Lossless first, then lossy, then fingerprint sidecars (.afpk is
# audio-fingerprint peak data, not audio — it's only ever a leftover next to a
# real track, so it ranks last and never survives a contest).
_FMT_RANK = {".flac": 6, ".wav": 5, ".shn": 4, ".ogg": 3, ".mp3": 2, ".afpk": 1}

_FMT_ORDER = "FLAC > WAV > SHN > OGG > MP3 > AFPK"

# Folder names that mean "the same tracks, in another format" — these are the
# only ones it's safe to match ACROSS, because the files inside are by
# definition the same performance. Anything else (Disc 1, Set II, Encore…) is
# a different part of the show and must never be cross-matched.
_FORMAT_DIR_RE = re.compile(
    r"^(flac|wav|shn|ogg|mp3|aac|m4a|alac|ape|wv|lossless|lossy|"
    r"flac\d{0,2}|shn\d{0,2}|mp3[\s._-]*(v[02]|\d{2,3})?|ogg[\s._-]*q?\d{0,3}|"
    r"\d{2}[\s._-]?bit(?:[\s._-]?\d{2,3}(\.\d)?k?)?|"
    r"v0|v2|320|256|192|160|128)$", re.IGNORECASE)


def _disc_context(p: Path, show_dir: Path) -> str:
    """The part of a file's path that identifies WHICH tracks it holds.

    Format-named folders are transparent — FLAC/ and MP3/ hold the same
    performance, so they collapse to the same context and their contents can
    be compared. Every other folder (Disc 1, Disc 2, Set I…) is kept, so
    track 01 of disc one is never confused with track 01 of disc two.
    """
    try:
        rel = p.parent.relative_to(show_dir)
    except ValueError:
        return str(p.parent)
    parts = [seg for seg in rel.parts if not _FORMAT_DIR_RE.match(seg)]
    return "/".join(parts).lower()


def _same_length(a: Path, b: Path) -> bool:
    """True unless two readable audio files clearly differ in duration.

    A last line of defence behind the folder rule: if both files can be read
    and their runtimes are more than a couple of seconds apart, they're
    different performances whatever they're called, so leave them alone.
    Unreadable formats (.shn, .afpk) can't be checked — those fall back to
    the folder rule alone.
    """
    try:
        from mutagen import File as MutagenFile
        la = MutagenFile(a)
        lb = MutagenFile(b)
        if not la or not lb or not getattr(la, "info", None) or not getattr(lb, "info", None):
            return True
        da, db = getattr(la.info, "length", 0), getattr(lb.info, "length", 0)
        if not da or not db:
            return True
        return abs(da - db) <= 2.0
    except Exception:
        return True


def dedupe_formats(show_dir: Path, log, b: "fileops.Batch") -> int:
    """When the same track exists in several formats, keep the best and
    move the rest to trash.

    Matching is by normalized filename stem WITHIN a disc context, so parallel
    FLAC/ and MP3/ folders are still de-duplicated, while identically numbered
    tracks in different disc folders are left alone — they're different songs.
    """
    groups = {}
    for p in show_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in _FMT_RANK:
            stem = re.sub(r"[^a-z0-9]", "", p.stem.lower())
            if stem:
                groups.setdefault((_disc_context(p, show_dir), stem), []).append(p)

    removed, culled = 0, []
    for files in groups.values():
        if len(files) < 2:
            continue
        files.sort(key=lambda f: _FMT_RANK[f.suffix.lower()], reverse=True)
        keeper = files[0]
        best = _FMT_RANK[keeper.suffix.lower()]
        for f in files[1:]:
            if _FMT_RANK[f.suffix.lower()] >= best:
                continue                      # tie on rank — keep both
            if not _same_length(keeper, f):
                log("info", "dedupe",
                    f"kept {f.name} — same name as {keeper.name} but a "
                    f"different length, so not a duplicate")
                continue
            try:
                b.trash(f, f"lower-quality copy of {keeper.name}")
                removed += 1
                if len(culled) < 12:
                    culled.append(f.name)
            except OSError:
                pass

    if removed:
        verb = "would trash" if b.dry_run else "trashed"
        detail = f"kept the best format per track — {verb} {removed} " \
                 f"lower-quality copy/copies ({_FMT_ORDER})"
        if culled:
            detail += ": " + ", ".join(culled) + ("…" if removed > len(culled) else "")
        log("info", "dedupe", detail)
        # prune folders the cull emptied (e.g. a parallel MP3/ dir). rmdir only
        # succeeds on empty directories, so nothing with content is touched.
        for d in [] if b.dry_run else sorted((x for x in show_dir.rglob("*") if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    return removed


# ── Folder prep ──────────────────────────────────────────────────────────────

def _probe_audio(path: Path) -> dict:
    """sample_fmt / bits / rate / duration via ffprobe ({} if unavailable)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=sample_fmt,bits_per_raw_sample,bits_per_sample,sample_rate,duration",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=60, text=True)
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        bits = int(st.get("bits_per_raw_sample") or 0) or int(st.get("bits_per_sample") or 0)
        dur = float(st.get("duration") or (data.get("format") or {}).get("duration") or 0)
        return {"sample_fmt": st.get("sample_fmt", ""), "bits": bits,
                "rate": int(st.get("sample_rate") or 0), "duration": dur}
    except Exception:
        return {}


def flac_command(src: Path, out: Path, target: str, probe: dict) -> list:
    """ffmpeg arguments for a WAV/SHN → FLAC conversion.

    preserve: same sample rate, same bit depth (FLAC is lossless; nothing is
              thrown away). Float or >24-bit PCM is the one exception — FLAC
              can't hold it portably, so it becomes 24-bit.
    cd:       16-bit / 44.1 kHz with triangular dither (how older installs converted).
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if target == "cd":
        cmd += ["-af", "aresample=resampler=swr:dither_method=triangular",
                "-ar", "44100", "-sample_fmt", "s16"]
    else:
        fmt = (probe.get("sample_fmt") or "").lower()
        bits = probe.get("bits") or 0
        if fmt.startswith(("flt", "dbl")) or bits > 24:
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    cmd += ["-map_metadata", "0", "-c:a", "flac", "-compression_level", "8", str(out)]
    return cmd


_ZIP_BOMB_RATIO = 200
UNSAFE_SUFFIXES = (".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".ps1", ".vbs", ".vbe", ".js",
                   ".jse", ".wsf", ".jar", ".msi", ".msp", ".dll", ".lnk", ".hta", ".cpl", ".reg",
                   ".apk", ".dmg", ".pkg", ".app", ".sh", ".command", ".deb", ".rpm")


MAX_ARCHIVE_FILES = 10000


def _size_refusal(total: int, count: int, left: int) -> str:
    if count > MAX_ARCHIVE_FILES:
        return f"{count} files is not a show"
    if left is not None and total > left:
        return f"it would unpack past the size limit ({total / 1e9:.1f} GB, {max(left, 0) / 1e9:.1f} GB left)"
    return ""


def _zip_refusal(zf: "zipfile.ZipFile", left) -> tuple:
    """(why not, bytes it unpacks to) for a zip."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    total = sum(i.file_size for i in infos)
    why = _size_refusal(total, len(infos), left)
    if why:
        return why, total
    packed = max(1, sum(i.compress_size for i in infos))
    if total > 100 * 2**20 and total / packed > _ZIP_BOMB_RATIO:
        return "it looks like a zip bomb", total
    return "", total


def _tar_refusal(tf: "tarfile.TarFile", left) -> tuple:
    members = tf.getmembers()
    total = sum(m.size for m in members if m.isfile())
    return _size_refusal(total, len(members), left), total


def _sevenzip_refusal(exe: str, arc: Path, left) -> tuple:
    """Look inside before extracting. Anything we can't list, we don't open."""
    try:
        r = subprocess.run([exe, "l", "-slt", "-ba", str(arc)], capture_output=True,
                           text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"couldn't look inside it ({e})", 0
    if r.returncode != 0:
        return "couldn't look inside it (damaged, or password-protected)", 0
    sizes = [int(x) for x in re.findall(r"^Size = (\d+)", r.stdout, re.MULTILINE)]
    why = _size_refusal(sum(sizes), len(sizes), left)
    if why:
        return why, sum(sizes)
    if any(n.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", n)
           for n in re.findall(r"^Path = (.+)$", r.stdout, re.MULTILINE)):
        return "it contains paths outside its own folder", 0
    if re.search(r"^Symbolic Link = \S", r.stdout, re.MULTILINE) \
            or re.search(r"^Attributes = .*\bl[rwx-]{9}", r.stdout, re.MULTILINE):
        return "it contains links", 0
    return "", sum(sizes)


def _remove_unsafe(show_dir: Path, b: "fileops.Batch", log):
    """Links and programs have no business in a show folder (an archive is
    the usual way they arrive). Links are removed; programs go to trash."""
    for p in list(show_dir.rglob("*")):
        try:
            if p.is_symlink():
                p.unlink()
                log("warn", "extract", f"removed a link from the download: {p.name}")
            elif p.is_file() and p.name.lower().endswith(UNSAFE_SUFFIXES):
                b.trash(p, "a program, not part of a show")
                log("warn", "extract", f"moved {p.name} to trash — programs don't belong in a show")
        except OSError:
            pass


def prep_folder(show_dir: Path, cfg: dict, log, b: "fileops.Batch"):
    """Extract archives, flatten single-subdir nesting, convert SHN/WAV.

    In a dry-run batch this only describes what it would do."""
    w = cfg["watcher"]
    dry = b.dry_run

    def _done_with_archive(arc: Path, verb: str):
        b.trash(arc, "archive already extracted")
        log("info", "extract", f"{verb} {arc.name} (archive moved to trash)")

    if w["auto_extract_archives"]:
        budget = int(float(w.get("max_extract_gb", 50) or 0) * 1e9)
        spent = 0           # one budget for the whole show, across every archive in it

        def left():
            return (budget - spent) if budget else None

        for z in [p for p in show_dir.rglob("*.zip") if p.is_file()]:
            if dry:
                b.note("extract", file=z.name)
                continue
            try:
                with zipfile.ZipFile(z) as zf:
                    why, size = _zip_refusal(zf, left())
                    if why:
                        log("warn", "extract", f"not unzipping {z.name}: {why}")
                        continue
                    zf.extractall(z.parent)        # zipfile drops absolute and ../ paths
                spent += size
                _done_with_archive(z, "unzipped")
            except Exception as e:
                log("warn", "extract", f"failed to unzip {z.name}: {e}")
        for t in [p for p in show_dir.rglob("*") if p.is_file()
                  and p.name.lower().endswith((".tar", ".tar.gz", ".tgz",
                                              ".tar.bz2", ".tar.xz", ".txz"))]:
            if dry:
                b.note("extract", file=t.name)
                continue
            try:
                with tarfile.open(t) as tf:
                    why, size = _tar_refusal(tf, left())
                    if why:
                        log("warn", "extract", f"not untarring {t.name}: {why}")
                        continue
                    tf.extractall(t.parent, filter="data")   # blocks path traversal and links out
                spent += size
                _done_with_archive(t, "untarred")
            except Exception as e:
                log("warn", "extract", f"failed to untar {t.name}: {e}")
        sevenzip = shutil.which("7z") or shutil.which("7za")
        if sevenzip:
            for arc in [p for p in (*show_dir.rglob("*.rar"), *show_dir.rglob("*.7z")) if p.is_file()]:
                if dry:
                    b.note("extract", file=arc.name)
                    continue
                why, size = _sevenzip_refusal(sevenzip, arc, left())
                if why:
                    log("warn", "extract", f"not extracting {arc.name}: {why}")
                    continue
                r = subprocess.run([sevenzip, "x", "-y", "-p-", f"-o{arc.parent}",
                                    str(arc)], capture_output=True, timeout=1800)
                spent += size                       # counted even if it stopped partway
                if r.returncode == 0:
                    _done_with_archive(arc, "extracted")
                else:
                    log("warn", "extract", f"could not extract {arc.name}")
        elif any(show_dir.rglob("*.rar")) or any(show_dir.rglob("*.7z")):
            log("warn", "extract", "rar/7z present but 7z binary missing — rebuild image")
        if not dry:
            _remove_unsafe(show_dir, b, log)

    # flatten:  show/OnlySubdir/* -> show/*
    entries = [p for p in show_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        if dry:
            b.note("flatten", folder=inner.name)
        else:
            for item in inner.iterdir():
                target = show_dir / item.name
                if not target.exists():
                    b.move(item, target)
            try:
                inner.rmdir()
            except OSError:
                pass

    if w.get("scrub_junk_files", True):
        scrub_junk(show_dir, log, b)

    if w.get("dedupe_formats", True):
        dedupe_formats(show_dir, log, b)

    target = w.get("convert_target", "preserve")

    def _lossless_convert(src: Path, label: str, keep: bool):
        out = src.with_suffix(".flac")
        if out.exists():
            return
        if dry:
            b.note("convert", file=src.name, to=out.name, target=target,
                   keep_original=keep)
            return
        probe = _probe_audio(src)
        cmd = flac_command(src, out, target, probe)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=600)
            if r.returncode != 0 or not out.exists():
                log("warn", "convert", f"ffmpeg failed on {src.name}")
                if out.exists():
                    b.trash(out, "failed conversion output")
                return
            # never let go of the original until the FLAC demonstrably holds
            # the whole performance
            got = _probe_audio(out)
            want, have = probe.get("duration") or 0, got.get("duration") or 0
            if want and have and abs(want - have) > 1.0:
                log("warn", "convert",
                    f"{src.name}: FLAC is {have:.1f}s but the original is "
                    f"{want:.1f}s — kept the original, trashed the FLAC")
                b.trash(out, "conversion length mismatch")
                return
            depth = f"{got.get('bits') or '?'}-bit/{(got.get('rate') or 0) / 1000:g} kHz"
            log("info", "convert", f"{label}→FLAC {src.name} ({depth})")
            if not keep:
                b.trash(src, f"converted to {out.name}")
        except Exception as e:
            log("warn", "convert", f"{src.name}: {e}")

    if w["convert_shn_to_flac"]:
        for shn in sorted(show_dir.rglob("*.shn")):
            _lossless_convert(shn, "SHN", w["keep_shn_originals"])

    if w.get("convert_wav_to_flac", True):
        # WAV is uncompressed lossless — same PCM data as FLAC, just bigger
        # and untaggable in our pipeline, so convert unless it's the only copy
        # a format-dedupe pass already lost to (handled by dedupe running first)
        for wav in sorted(show_dir.rglob("*.wav")):
            _lossless_convert(wav, "WAV", w.get("keep_wav_originals", False))


# ── Identification ───────────────────────────────────────────────────────────

def identify(show_dir: Path, cfg: dict, log) -> dict:
    """Gather every metadata source and produce the merged result + score."""
    aliases = cfg["artists"]["aliases"]
    sidecar = read_sidecar(show_dir)

    known_venues = frozenset(library_index.venue_norms())
    tags_meta, curated, _titles = read_embedded_tags(show_dir)
    info_meta, info_path = parse_best_info(show_dir, known_venues)
    folder_meta = parse_folder_name(show_dir.name, aliases)
    files_meta = parse_filenames(show_dir, aliases)

    sidecar_meta = ShowMeta()
    if sidecar.get("official"):
        sidecar_meta.official = sidecar["official"]
    if sidecar.get("source_type"):
        sidecar_meta.source_type = sidecar["source_type"]
    if sidecar.get("shnid"):
        sidecar_meta.shnid = str(sidecar["shnid"])

    # bootstrap artist/date for external lookups
    boot_artist = (tags_meta.artist or info_meta.artist or folder_meta.artist)
    boot_date = (tags_meta.date or info_meta.date or folder_meta.date)

    ia_meta, slf_meta = None, None
    src_cfg = cfg["sources"]

    if src_cfg["use_internet_archive"]:
        ident = show_dir.name if sources.is_ia_identifier(show_dir.name) else ""
        if ident:
            ia_meta = sources.fetch_ia(ident)
            if ia_meta:
                log("info", "lookup", f"archive.org matched {ident}")

    from .metadata import plausible_artist
    if not plausible_artist(boot_artist):
        boot_artist = ""

    if src_cfg["use_setlistfm"] and src_cfg["setlistfm_api_key"]:
        if boot_artist and boot_date:
            slf_artist = aliases.get(_norm(boot_artist), boot_artist)
            slf_meta = sources.fetch_setlistfm(
                slf_artist, boot_date, src_cfg["setlistfm_api_key"])
            if slf_meta:
                log("info", "lookup", f"setlist.fm matched {slf_artist} {boot_date}")
        if not slf_meta and boot_date:
            # No artist (or artist unrecognized) — reverse lookup by city+date,
            # confirmed against the venue name and our known track titles.
            boot_venue = (info_meta.venue or tags_meta.venue or folder_meta.venue)
            boot_city = (info_meta.city or tags_meta.city or folder_meta.city)
            known_titles = [t["title"] for t in
                            (info_meta.tracks or tags_meta.tracks)]
            if boot_venue or boot_city:
                slf_meta = sources.fetch_setlistfm_reverse(
                    boot_date, boot_city, boot_venue,
                    src_cfg["setlistfm_api_key"], known_titles)
                if slf_meta:
                    log("info", "lookup",
                        f"setlist.fm reverse match: {slf_meta.artist} @ "
                        f"{slf_meta.venue} {boot_date}")

    ordered = {"sidecar": sidecar_meta}
    if curated:
        ordered["curated_tags"] = tags_meta
    ordered["info_file"] = info_meta
    if slf_meta:
        ordered["setlistfm"] = slf_meta
    if ia_meta:
        ordered["archive_org"] = ia_meta
    if not curated:
        ordered["tags"] = tags_meta
    ordered["folder_name"] = folder_meta
    ordered["filenames"] = files_meta

    merged, provenance, agreement = merge(ordered)

    if src_cfg["use_musicbrainz_genre"] and merged.artist and not merged.genre:
        merged.genre = sources.fetch_mb_genre(merged.artist)

    merged.artist = clean_artist(merged.artist) if merged.artist else ""
    if not plausible_artist(merged.artist):
        merged.artist = ""
        provenance.pop("artist", None)
    merged.state = normalise_state(merged.state)

    # apply your learned corrections (from past manual fixes)
    if merged.artist:
        fix = library_index.correction_get("artist", merged.artist)
        if fix:
            merged.artist = fix
            provenance["artist"] = provenance.get("artist", "") + "→corrected"
    if merged.venue:
        vfix = library_index.correction_get("venue", merged.venue)
        if vfix:
            merged.venue = vfix.get("venue", merged.venue)
            if vfix.get("city"):
                merged.city = vfix["city"]
            if vfix.get("state"):
                merged.state = vfix["state"]
            provenance["venue"] = "corrected"

    # text hygiene on everything that ends up in tags and folder names
    for f in ShowMeta.FIELDS:
        setattr(merged, f, fix_text(getattr(merged, f)))
    merged.tracks = [
        {**t, "title": clean_track_title(t["title"])}
        for t in merged.tracks
        if clean_track_title(t["title"]) and not is_junk_track_title(t["title"])
    ]

    # ── Track-title validation (Plex/On-This-Day quality bar) ────────────
    # Prefer setlist.fm's setlist over a shaky local one; cross-check every
    # title against the self-seeding per-artist song corpus. If the setlist
    # can't be confidently validated, flag it so it routes to Review rather
    # than porting junk titles downstream.
    titles_uncertain = ""
    slf_tracks = slf_meta.tracks if slf_meta else []
    local_tracks = merged.tracks
    audio_n = _count_audio(show_dir)

    # 1) if setlist.fm has a setlist and the local one is weak, adopt it
    if slf_tracks:
        local_recog = _corpus_recognition(merged.artist, local_tracks)
        if (not local_tracks or local_recog < 0.6
                or _mass_tagged(local_tracks)
                or abs(len(local_tracks) - audio_n) > abs(len(slf_tracks) - audio_n)):
            merged.tracks = slf_tracks
            provenance["tracks"] = "setlistfm"
            log("info", "titles", f"adopted setlist.fm setlist "
                f"({len(slf_tracks)} songs) over local titles")

    # 1b) archive.org lists a title for every file of the recording — tuning
    # and crowd tracks included — so when its count matches the files and the
    # setlist's doesn't, it's the better pairing.
    ia_tracks = ia_meta.tracks if ia_meta else []
    if ia_tracks and audio_n and len(ia_tracks) == audio_n != len(merged.tracks) \
            and not _mass_tagged(ia_tracks):
        merged.tracks = [{**t, "title": clean_track_title(t["title"])} for t in ia_tracks]
        provenance["tracks"] = "archive_org"
        log("info", "titles", f"used archive.org's per-file titles ({len(ia_tracks)} files)")

    # 2) drop a mass-tagged degenerate list entirely (but keep real reprises)
    if _mass_tagged(merged.tracks):
        log("warn", "titles", "titles look mass-tagged — discarded")
        merged.tracks = []
        provenance.pop("tracks", None)

    # 2b) pair titles with files: set aside tuning/crowd/set-break files first
    alignment = None
    if merged.tracks and audio_n:
        from . import align
        alignment = align.plan(_audio_files(show_dir, AUDIO_EXTS), merged.tracks)

    # 3) cross-check the final titles against the corpus + count
    if merged.tracks:
        recog = _corpus_recognition(merged.artist, merged.tracks)
        from_slf = provenance.get("tracks") == "setlistfm"
        count_ok = alignment is None or alignment["ok"]
        if not count_ok and from_slf:
            titles_uncertain = alignment["note"]
        # unknown artist with no corpus yet: can't validate, so trust setlist.fm
        # but be wary of purely-local titles
        corpus_size = library_index.songs_count_for(merged.artist)
        if not from_slf:
            if corpus_size >= 5 and recog < 0.5:
                titles_uncertain = (f"only {int(recog*100)}% of titles match known "
                                    f"{merged.artist} songs")
            elif not count_ok:
                titles_uncertain = alignment["note"]
            elif corpus_size < 5 and not slf_meta:
                # no corpus and no setlist.fm confirmation — can't vouch for these
                if _looks_unreliable(merged.tracks):
                    titles_uncertain = "unverified titles (no setlist.fm match)"

    # 4) learn confirmed songs into the corpus for future cross-checks
    if merged.tracks and merged.artist:
        confirmed = provenance.get("tracks") == "setlistfm"
        library_index.learn_songs(
            merged.artist, _songs_only(merged.tracks), confirmed=confirmed)

    sanitize_location(merged)   # cross-source composition can re-mangle

    # gazetteer completion: a known venue fills in missing city/state
    if merged.venue and not (merged.city and merged.state):
        hit = library_index.venue_lookup(merged.venue)
        if hit:
            _, v_city, v_state = hit
            if not merged.city and v_city:
                merged.city = v_city
                provenance["city"] = "library_index"
            if not merged.state and v_state:
                merged.state = v_state
                provenance["state"] = "library_index"

    # places: the state from the city (Toronto → ON), and outside the US and
    # Canada the country in full (Amsterdam, NL → Netherlands)
    if merged.city:
        from . import places
        places.apply(merged, provenance)

    library_dir = Path(cfg["paths"]["library_dir"])
    canon, in_library = canonical_artist(merged.artist, library_dir, cfg)
    if canon != merged.artist:
        provenance["artist"] = provenance.get("artist", "") + "→library"
    merged.artist = canon

    # a setlist.fm-confirmed artist overrides a weak local guess it contradicts
    if not in_library and slf_meta and slf_meta.artist \
            and _norm(slf_meta.artist) != _norm(merged.artist):
        c2, h2 = canonical_artist(clean_artist(slf_meta.artist), library_dir, cfg)
        if h2:
            merged.artist, in_library = c2, True
            provenance["artist"] = "setlistfm"

    # host artist: the headliner owns the Album Artist tag and the folder
    host, host_in_lib = derive_host_artist(merged.artist, library_dir, cfg)
    if host and host != merged.artist:
        merged.host_artist = host
        provenance["host_artist"] = "collab-split"
    in_library = in_library or host_in_lib

    # source-of-truth hierarchy for the source type:
    #   1. sidecar / curated bracket / explicit "Source:" line — authoritative
    #   2. a source token in the FOLDER NAME — definitive when present
    #   3. otherwise, infer from the info (.txt) file
    #   4. otherwise, listen to the audio (below)
    src_prov = provenance.get("source_type", "")
    explicit = (src_prov in ("sidecar", "curated_tags")
                or (src_prov == "info_file"
                    and getattr(info_meta, "source_explicit", False)))
    if not explicit:
        folder_src = folder_meta.source_type    # token found in the folder name
        if folder_src:
            if folder_src != merged.source_type:
                merged.source_type = folder_src
                provenance["source_type"] = "folder_name"
        else:
            # folder is silent — fall through to the info file's inference
            info_text = ""
            if info_path:
                try:
                    info_text = info_path.read_text(errors="replace")
                except OSError:
                    pass
            comb = infer_source_type(info_text)
            if comb and comb != merged.source_type:
                merged.source_type = comb
                provenance["source_type"] = "info_file"

    # last resort for the source: listen to the tape itself
    if not merged.source_type and not merged.official \
            and cfg["watcher"].get("audio_source_analysis", True):
        probe = audioprobe.analyze_source(show_dir)
        if probe["source_type"]:
            merged.source_type = probe["source_type"]
            provenance["source_type"] = "audio_analysis"
        log("info", "listen", probe["detail"]
            + (f" → {probe['source_type']}" if probe["source_type"] else ""))

    external_confirmed = bool(slf_meta or ia_meta)
    artist_strong = bool(merged.artist) and (
        in_library or curated or bool(sidecar.get("official"))
        or agreement.get("artist", 0) >= 2
        or str(provenance.get("artist", "")).startswith(("setlistfm", "archive_org")))
    conf = score(merged, agreement, curated, sidecar,
                 external_confirmed, in_library, artist_strong)
    missing = [f for f in REQUIRED_FIELDS if not getattr(merged, f)]

    return {"meta": merged, "provenance": provenance, "confidence": conf,
            "missing": missing, "curated": curated, "sidecar": sidecar,
            "artist_in_library": in_library,
            "titles_uncertain": titles_uncertain,
            "alignment": ({"how": alignment["how"], "extras": alignment["extras"]}
                          if alignment else None)}


# ── Album-art transplant ─────────────────────────────────────────────────────

_ART_FILES = ("folder.jpg", "cover.jpg", "front.jpg", "album.jpg",
              "folder.png", "cover.png", "front.png")


def extract_art(folder: Path):
    """First embedded picture found (FLAC or MP3), else a loose cover file.
    Returns (bytes, mime) or (None, None)."""
    for f in sorted(folder.rglob("*")):
        try:
            if f.suffix.lower() == ".flac":
                pics = FLAC(f).pictures
                if pics:
                    return pics[0].data, pics[0].mime or "image/jpeg"
            elif f.suffix.lower() == ".mp3":
                apics = ID3(f).getall("APIC")
                if apics:
                    return apics[0].data, apics[0].mime or "image/jpeg"
        except Exception:
            continue
    for name in _ART_FILES:
        p = folder / name
        if p.is_file():
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            try:
                return p.read_bytes(), mime
            except OSError:
                pass
    return None, None


def folder_has_art(folder: Path) -> bool:
    data, _ = extract_art(folder)
    return data is not None


def embed_art(folder: Path, data: bytes, mime: str) -> int:
    """Embed the picture into every audio file that lacks one; also drop a
    cover file if the folder has none. Returns files updated."""
    from mutagen.flac import Picture
    from mutagen.id3 import APIC
    done = 0
    for f in sorted(folder.rglob("*")):
        try:
            if f.suffix.lower() == ".flac":
                a = FLAC(f)
                if not a.pictures:
                    pic = Picture()
                    pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Front Cover", data
                    a.add_picture(pic)
                    a.save()
                    done += 1
            elif f.suffix.lower() == ".mp3":
                a = ID3(f)
                if not a.getall("APIC"):
                    a.add(APIC(encoding=3, mime=mime, type=3,
                               desc="Front Cover", data=data))
                    a.save(f)
                    done += 1
        except Exception:
            continue
    if not any((folder / n).exists() for n in _ART_FILES):
        ext = ".png" if "png" in mime else ".jpg"
        try:
            (folder / f"folder{ext}").write_bytes(data)
            perms.fix_path(folder / f"folder{ext}")
        except OSError:
            pass
    return done


# ── Duplicate comparison: is the newcomer an upgrade? ────────────────────────
# Source rank first (SBD > SBD.FM > MTX > AUD), then completeness:
# track count → titled-track count → total audio size (>5% to matter).

_SRC_RANK = {"SBD": 6, "SBD.FM": 5, "MTX": 4, "AUD.FOB": 3, "AUD": 2, "": 1}


def show_stats(folder: Path, source_hint: str = "") -> dict:
    files = [p for p in folder.rglob("*")
             if p.suffix.lower() in AUDIO_EXTS and p.is_file()]
    titled, size = 0, 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            pass
        if f.suffix.lower() in TAGGABLE_EXTS:
            try:
                from mutagen import File as MutagenFile
                t = (MutagenFile(f, easy=True).get("title") or [""])[0].strip()
                if len(t) > 1 and not t.lower().startswith(("track", "audio"))                         and t.lower() != f.stem.lower():
                    titled += 1
            except Exception:
                pass
    src = source_hint
    official = ""
    from .metadata import parse_album_string
    if not src:
        parsed, _ = parse_album_string(folder.name)
        src = parsed.source_type
        official = parsed.official
    else:
        # source came from the incoming meta; still sniff official from the name
        _p, _ = parse_album_string(folder.name)
        official = _p.official
    return {"tracks": len(files), "titled": titled, "size": size,
            "source": src or "", "official": official or ""}


def compare_shows(incoming: dict, existing: dict):
    """Returns ('upgrade'|'downgrade'|'tie', human reason)."""
    it, et = incoming["tracks"], existing["tracks"]

    def with_tracks(verdict, reason):
        """Every verdict spells out the track-count difference, if any —
        and flags an upgrade that would arrive with FEWER tracks."""
        if it == et:
            return verdict, reason
        note = f"{it} vs {et} tracks"
        if verdict == "upgrade" and it < et:
            note = f"⚠ fewer tracks: {note} — verify before replacing"
        elif verdict == "downgrade" and it > et:
            note = f"more tracks ({note}) but "
            return verdict, note + reason
        return verdict, f"{reason} ({note})"

    # an official release is the top of the source hierarchy — nothing an
    # incoming copy offers (rank, tracks, size) can supersede it.
    if existing.get("official") and not incoming.get("official"):
        return with_tracks("downgrade",
                           f"existing copy is an official release "
                           f"({existing['official']}) — not an upgrade")
    si = _SRC_RANK.get(incoming["source"], 1)
    se = _SRC_RANK.get(existing["source"], 1)
    inc_s, ex_s = incoming["source"] or "unknown", existing["source"] or "unknown"
    if si != se:
        verdict = "upgrade" if si > se else "downgrade"
        return with_tracks(verdict, f"source {inc_s} vs existing {ex_s}")
    if it != et:
        verdict = "upgrade" if it > et else "downgrade"
        return verdict, f"same source ({inc_s}), {it} vs {et} tracks"
    if incoming["titled"] != existing["titled"]:
        verdict = "upgrade" if incoming["titled"] > existing["titled"] else "downgrade"
        return verdict, (f"same source and track count, "
                         f"{incoming['titled']} vs {existing['titled']} titled tracks")
    bigger = incoming["size"] > existing["size"] * 1.05
    smaller = incoming["size"] < existing["size"] * 0.95
    mb = lambda b: f"{b / 1048576:.0f} MB"
    if bigger or smaller:
        verdict = "upgrade" if bigger else "downgrade"
        return verdict, (f"same source, tracks and titles — "
                         f"{mb(incoming['size'])} vs {mb(existing['size'])}")
    return "tie", (f"effectively identical: {inc_s}, "
                   f"{incoming['tracks']} tracks, ~{mb(existing['size'])}")


def _stash(path: Path, cfg: dict, bucket: str, b: "fileops.Batch") -> Path:
    """Move a folder into a watch-side holding pen (_replaced / _duplicates)."""
    root = Path(cfg["paths"]["watch_dir"]) / bucket
    b.mkdir(root)
    dest = root / path.name
    if dest.exists():
        from datetime import datetime
        dest = root / f"{path.name} ({datetime.now():%H%M%S})"
    b.move(path, dest)
    if not b.dry_run:
        perms.fix_tree(dest)
    return dest


# ── Duplicate check ──────────────────────────────────────────────────────────

def find_duplicate(meta: ShowMeta, library_dir: Path):
    if not (meta.artist and meta.date):
        return None
    artist_dir = library_dir / safe_name(meta.album_artist)
    if not artist_dir.is_dir():
        return None
    for d in artist_dir.iterdir():
        if not d.is_dir():
            continue
        if meta.date in d.name:
            return str(d)
        if _YEARDIR_RE.match(d.name) and d.name.endswith(meta.date[:4]):
            for sub in d.iterdir():
                if sub.is_dir() and meta.date in sub.name:
                    return str(sub)
    return None


# ── Artist-folder convention learning ────────────────────────────────────────
# Existing folders are the style guide: if Trey Anastasio's shows are named
# "trey2003-02-14 ..." inside "trey2003/" year folders, new shows follow suit.

_SHOWDIR_RE = re.compile(r"^(.*?)((?:19|20)\d{2})-\d{2}-\d{2}")
_YEARDIR_RE = re.compile(r"^(.*?)((?:19|20)\d{2})$")


def artist_folder_conventions(artist_dir: Path):
    """Returns (show_prefix|None, year_prefix|None, uses_year_subfolders).
    Prefixes are learned verbatim from existing names ('trey', 'gd',
    'Trey Anastasio ' ...). None = nothing to learn (new/empty artist)."""
    from collections import Counter
    show_prefixes, year_prefixes = Counter(), Counter()
    if artist_dir.is_dir():
        for child in artist_dir.iterdir():
            if not child.is_dir():
                continue
            m = _SHOWDIR_RE.match(child.name)
            if m:
                show_prefixes[m.group(1)] += 1
                continue
            y = _YEARDIR_RE.match(child.name)
            if y:
                year_prefixes[y.group(1)] += 1
                try:
                    for sub in child.iterdir():
                        if sub.is_dir():
                            m2 = _SHOWDIR_RE.match(sub.name)
                            if m2:
                                show_prefixes[m2.group(1)] += 1
                except OSError:
                    pass
    show_prefix = show_prefixes.most_common(1)[0][0] if show_prefixes else None
    year_prefix = year_prefixes.most_common(1)[0][0] if year_prefixes else None
    if show_prefix is None and year_prefix is not None:
        show_prefix = year_prefix
    return show_prefix, year_prefix, bool(year_prefixes)


# ── Tagging & filing ─────────────────────────────────────────────────────────

def _audio_files(folder: Path, exts=TAGGABLE_EXTS) -> list:
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in exts and p.is_file())


def _songs_only(tracks: list) -> list:
    """Titles worth learning as songs — not Tuning, Crowd, Set Break…"""
    from . import align
    return [t["title"] for t in tracks if not align.is_filler(t["title"])]


def tag_audio(show_dir: Path, meta: ShowMeta, log, b: "fileops.Batch"):
    if meta.parts:
        return _tag_parts(show_dir, meta, log, b)
    return _tag_files(_audio_files(show_dir), meta, meta, log, b)


def _tag_parts(show_dir: Path, meta: ShowMeta, log, b: "fileops.Batch") -> int:
    """An early + late show: each part is a disc, numbered from 1 again,
    matched against its own setlist; every file gets the same album."""
    n = 0
    total_discs = len(meta.parts)
    for part in meta.parts:
        folder = show_dir / part["dir"]
        if not folder.is_dir():
            log("warn", "tag", f"{part['dir']} isn't in the show folder any more")
            continue
        audio = sorted(p for p in folder.rglob("*")
                       if p.suffix.lower() in TAGGABLE_EXTS and p.is_file())
        setlist = ShowMeta(tracks=part.get("tracks") or [])
        n += _tag_files(audio, meta, setlist, log, b, disc=part["disc"], total_discs=total_discs)
    return n


def _tag_files(audio: list, meta: ShowMeta, setlist: ShowMeta, log, b: "fileops.Batch",
               disc: int = 0, total_discs: int = 0) -> int:
    from . import align
    album = meta.album_title
    loc = ", ".join(p for p in (meta.venue, meta.city, meta.state) if p)
    pairing = align.plan(audio, setlist.tracks, extras=meta.extras or None)
    if setlist.tracks and pairing["extras"]:
        log("info", "titles", ("not songs: " if pairing["ok"] else "probably not songs: ")
            + align.summary(pairing))
    if setlist.tracks and not pairing["ok"]:
        log("warn", "titles", pairing["note"])
    for i, (f, entry) in enumerate(zip(audio, pairing["entries"]), 1):
        track = {"title": entry["title"]} if entry["title"] else {}

        def _write(f, track=track, i=i):
            if f.suffix.lower() == ".flac":
                a = FLAC(f)
                a["ARTIST"] = meta.artist
                a["ALBUMARTIST"] = meta.album_artist
                a["ALBUM"] = album
                a["DATE"] = meta.date
                a["GENRE"] = meta.genre or "Live"
                if loc:
                    a["COMMENT"] = loc
                if disc:
                    a["DISCNUMBER"] = str(disc)
                    a["TOTALDISCS"] = a["DISCTOTAL"] = str(total_discs)
                    a["TRACKNUMBER"] = str(i)            # restarts on every disc
                if track:
                    a["TITLE"] = track["title"]
                    a["TRACKNUMBER"] = str(i)
                elif not a.get("TITLE"):
                    a["TITLE"] = f.stem
                a.save()
            else:
                try:
                    a = ID3(f)
                except ID3NoHeaderError:
                    a = ID3()
                a.setall("TPE1", [TPE1(encoding=3, text=meta.artist)])
                a.setall("TPE2", [TPE2(encoding=3, text=meta.album_artist)])
                a.setall("TALB", [TALB(encoding=3, text=album)])
                a.setall("TDRC", [TDRC(encoding=3, text=meta.date)])
                a.setall("TCON", [TCON(encoding=3, text=meta.genre or "Live")])
                if disc:
                    a.setall("TPOS", [TPOS(encoding=3, text=f"{disc}/{total_discs}")])
                    a.setall("TRCK", [TRCK(encoding=3, text=str(i))])
                if track:
                    a.setall("TIT2", [TIT2(encoding=3, text=track["title"])])
                    a.setall("TRCK", [TRCK(encoding=3, text=str(i))])
                a.save(f)
        try:
            b.retag(f, _write, {"title": track["title"] if track else None,
                                "track": i if track else None})
        except Exception as e:
            log("warn", "tag", f"{f.name}: {e}")
    if b.dry_run and audio:
        b.note("tags", files=len(audio), artist=meta.artist,
               album_artist=meta.album_artist, album=album, date=meta.date,
               **({"disc": disc} if disc else {}),
               titled=sum(1 for e in pairing["entries"] if e["title"]),
               **({"not_songs": pairing["extras"]} if pairing["extras"] else {}))
    return len(audio)


def safe_name(name: str) -> str:
    """Filesystem+SMB safe: no  / \\ : * ? \" < > |  and no trailing dots/spaces.
    (A colon in a folder name is legal on Linux but unreachable over SMB.)"""
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip(" .") or "Unknown"


def canonical_folder_name(meta: ShowMeta, cfg: dict,
                          learned_prefix: str = None) -> str:
    style = cfg["filing"]["folder_prefix_style"]
    host = meta.album_artist or meta.artist
    if learned_prefix is not None:
        prefix, sep = learned_prefix, ""     # verbatim: existing style wins
    else:
        prefix = (_norm(host) if style == "slug" else host) or "unknown"
        sep = "" if style == "slug" else " "
    loc = ", ".join(p for p in (meta.venue, meta.city, meta.state) if p)
    name = f"{prefix}{sep}{meta.date}"
    if loc:
        name += f" {loc}"
    if meta.bracket:
        name += f" [{meta.bracket}]"
    return safe_name(name)


def file_show(show_dir: Path, meta: ShowMeta, cfg: dict, log, b: "fileops.Batch") -> Path:
    """Tag, rename to canonical, move into /music/{Artist}/, fix permissions."""
    # the album tag ALWAYS carries a bracket: unlabeled tapes default to AUD
    if not meta.source_type and not meta.official:
        meta.source_type = cfg["filing"].get("default_source_type", "AUD")

    # scrub trade debris here too, so shows filed straight from Review
    # (which skip prep_folder) don't carry .torrent/.json/.json.gz/spectrograms
    # into the library. Idempotent — harmless if prep already ran it.
    if cfg["watcher"].get("scrub_junk_files", True):
        scrub_junk(show_dir, log, b)

    library_dir = Path(cfg["paths"]["library_dir"])
    artist_dir = library_dir / safe_name(meta.album_artist)   # host owns the folder
    b.mkdir(artist_dir)

    n = tag_audio(show_dir, meta, log, b)
    if not b.dry_run:
        log("info", "tag", f"tagged {n} file(s) — ALBUM: {meta.album_title}")

    # drop the sidecar before moving (to trash, so an undo can bring it back)
    for name in (".reelarr.json", ".barbosa.json"):
        sc = show_dir / name
        if sc.exists():
            b.trash(sc, "provenance sidecar, no longer needed")

    # match the artist folder's existing conventions
    show_prefix, year_prefix, uses_years = artist_folder_conventions(artist_dir)
    dest_parent = artist_dir
    if uses_years and meta.date:
        ydir = artist_dir / safe_name(f"{year_prefix or ''}{meta.date[:4]}")
        b.mkdir(ydir)
        dest_parent = ydir
        if not b.dry_run:
            log("info", "file", f"year layout → {ydir.name}/")
    dest = dest_parent / canonical_folder_name(meta, cfg, show_prefix)
    if dest.exists():
        i = 2
        while (alt := dest.parent / f"{dest.name} ({i})").exists():
            i += 1
        dest = alt
    b.move(show_dir, dest)
    if b.dry_run:
        return dest
    perms.fix_tree(dest)
    perms.fix_path(artist_dir)
    try:
        library_index.add_show(meta)   # every filed show teaches the index
    except Exception:
        pass
    return dest


# ── Top-level entry: process one watch-folder show ───────────────────────────

def _count_audio(show_dir: Path) -> int:
    return sum(1 for p in show_dir.rglob("*")
               if p.suffix.lower() in AUDIO_EXTS and p.is_file())


def _mass_tagged(tracks: list) -> bool:
    """True only for degenerate lists — one title covering nearly the whole
    show. Real reprises (Dark Star > GDTRFB > Dark Star, a couple of Jams,
    two Intros) are fine and return False."""
    if not tracks or len(tracks) <= 3:
        return False
    from collections import Counter
    titles = [t["title"].strip().lower() for t in tracks]
    counts = Counter(titles)
    if len(counts) == 1:
        return True
    top, n = counts.most_common(1)[0]
    # a genuine reprise repeats 2-3x; mass-tag has one title on ~everything
    return n / len(tracks) >= 0.8 and n >= 4


def _looks_unreliable(tracks: list) -> bool:
    """Heuristic junk check for a title list with no external confirmation."""
    if not tracks:
        return True
    junk = 0
    for t in tracks:
        title = t["title"]
        if (re.search(r"\.(flac|shn|wav|mp3)\b", title, re.IGNORECASE)
                or re.match(r"^\d{1,2}:\d{2}", title)
                or re.match(r"^(track|audio|untitled)\b", title, re.IGNORECASE)
                or sum(c.isdigit() for c in title) / max(len(title), 1) > 0.4):
            junk += 1
    return junk / len(tracks) >= 0.3


def _corpus_recognition(artist: str, tracks: list) -> float:
    """Fraction of a track list recognized in the per-artist song corpus.
    Returns 1.0 when there's nothing to check against (can't penalize)."""
    if not tracks or not artist:
        return 1.0
    corpus = library_index.known_songs(artist)
    if not corpus:
        return 1.0    # no corpus yet — don't penalize
    from . import align
    songs = [t for t in tracks if not align.is_filler(t["title"])]   # Tuning isn't a song to recognise
    if not songs:
        return 1.0
    recognized = sum(1 for t in songs
                     if library_index.song_known(artist, t["title"]))
    return recognized / len(songs)


def process_show(show_dir: Path) -> dict:
    cfg = config.load()

    def log(level, event, detail=""):
        db.log(level, event, f"{show_dir.name}: {detail}" if detail else show_dir.name)

    show_id = db.upsert_show(str(show_dir), status="processing",
                             source_hint=read_sidecar(show_dir).get("source", ""))
    # one batch for the whole intake of this show: undoing it puts the folder
    # back in the watch folder exactly as it arrived
    b = fileops.Batch(f"intake {show_dir.name}", kind="intake", show_id=show_id,
                      undo_state={"status": "review", "current_path": str(show_dir),
                                  "filed_path": "",
                                  # review, not pending: an undone show must not
                                  # be quietly re-filed by the next sweep
                                  "notes": "Undone from Activity — file, "
                                           "reprocess or reject it when ready"})
    try:
        prep_folder(show_dir, cfg, log, b)

        audio = [p for p in show_dir.rglob("*") if p.suffix.lower() in AUDIO_EXTS and p.is_file()]
        if not audio and b.dry_run and any(op["op"] == "extract" for op in b.plan):
            # the show is still inside an archive; a dry run can't look in
            # without extracting, so say so rather than calling it empty
            return _plan(show_id, b, log, "Dry run: the audio is inside an archive "
                         "— it'll be identified once Reelarr is live and extracts it.")
        if not audio:
            db.update_show(show_id, status="review", confidence=0,
                           missing=json.dumps(["no audio files found"]),
                           notes="No audio files found after extraction.")
            log("warn", "review", "no audio files")
            return {"status": "review"}

        from . import pairs
        pair = pairs.split(show_dir, cfg["artists"]["aliases"])
        if pair:
            log("info", "pair", f"early + late show: {pair[0].name} (disc 1) + {pair[1].name} (disc 2)")
            result = pairs.combine(identify(pair[0], cfg, log), identify(pair[1], cfg, log), *pair)
        else:
            result = identify(show_dir, cfg, log)
        meta, conf, missing = result["meta"], result["confidence"], result["missing"]

        dupe = find_duplicate(meta, Path(cfg["paths"]["library_dir"]))
        threshold = int(cfg["filing"]["auto_file_threshold"])
        needs_review = bool(missing) or conf < threshold
        note = ""
        replace_existing = None

        # never auto-file a show whose setlist couldn't be validated — the
        # titles port straight to Plex and On This Day, so hold it for review
        titles_uncertain = result.get("titles_uncertain", "")
        if titles_uncertain and cfg["filing"].get("hold_uncertain_titles", True):
            needs_review = True
            note = f"Titles need a look: {titles_uncertain}"

        if dupe:
            policy = cfg["filing"]["duplicate_policy"]
            inc = show_stats(show_dir, meta.source_type)
            # the incoming hasn't been tagged yet — credit the setlist we're
            # about to write, or comparing 'titled tracks' is unfair
            inc["titled"] = max(inc["titled"],
                                min(len(meta.tracks or []), inc["tracks"]))
            inc["official"] = meta.official or inc.get("official", "")
            ex = show_stats(Path(dupe))
            verdict, reason = compare_shows(inc, ex)
            if policy == "upgrade" and not needs_review:
                if verdict == "upgrade":
                    replace_existing = Path(dupe)
                    note = f"Upgrade over {Path(dupe).name} — {reason}"
                elif verdict == "downgrade":
                    stash = _stash(show_dir, cfg, "_duplicates", b)
                    if b.dry_run:
                        return _plan(show_id, b, log,
                                     f"Dry run: not an upgrade over {Path(dupe).name} — "
                                     f"{reason}. Would move to _duplicates.",
                                     confidence=conf, meta=meta.to_dict())
                    db.update_show(show_id, status="rejected", confidence=conf,
                                   meta=meta.to_dict(), current_path=str(stash),
                                   notes=f"Not an upgrade over {Path(dupe).name} — {reason}")
                    log("info", "duplicate",
                        f"kept existing copy ({reason}) → moved to _duplicates")
                    return {"status": "rejected", "reason": reason}
                else:
                    needs_review = True
                    note = f"Duplicate of {Path(dupe).name} — {reason}"
            else:
                if policy != "file_anyway":
                    needs_review = True
                note = f"Possible duplicate of: {dupe} ({verdict}: {reason})"
        if not result["artist_in_library"] \
                and cfg["filing"]["new_artist_policy"] == "review" and meta.artist:
            needs_review = True
            note = (note + " · " if note else "") + f"New artist: {meta.artist}"

        common = dict(confidence=conf, meta=meta.to_dict(),
                      provenance=result["provenance"], missing=missing,
                      notes=note)

        if needs_review:
            db.update_show(show_id, status="review", **common)
            log("info", "review",
                f"confidence {conf}, missing: {', '.join(missing) or 'none'}"
                + (f" — {note}" if note else ""))
            return {"status": "review", "confidence": conf}

        old_art = (None, None)
        if replace_existing and replace_existing.exists():
            if not folder_has_art(show_dir):
                old_art = extract_art(replace_existing)
            stash = _stash(replace_existing, cfg, "_replaced", b)
            if not b.dry_run:
                log("info", "upgrade",
                    f"retired {replace_existing.name} to _replaced ({note})")

        dest = file_show(show_dir, meta, cfg, log, b)
        if b.dry_run:
            return _plan(show_id, b, log, f"Dry run: would file to {dest}"
                         + (f" — {note}" if note else ""), dest=str(dest), **common)
        if old_art[0]:
            n_art = embed_art(dest, *old_art)
            log("info", "upgrade", f"carried album art from the old copy "
                f"({n_art} file(s) updated)")
        db.update_show(show_id, status="filed", filed_path=str(dest),
                       current_path=str(dest), **common)
        log("info", "filed", f"→ {dest}")
        return {"status": "filed", "confidence": conf, "dest": str(dest)}

    except Exception as e:
        db.update_show(show_id, status="error", error=str(e))
        log("error", "error", str(e))
        return {"status": "error", "error": str(e)}


def process_pair(early: Path, late: Path) -> dict:
    """An early and a late show of one night, arriving as two folders.
    Live: they're moved into one folder in the watch folder (journalled, so
    Undo separates them again) and processed as one show. Dry run: the plan
    for the combined show is recorded on both, and nothing moves."""
    from . import pairs
    cfg = config.load()
    artist, date = pairs.quick_identity(early, cfg["artists"]["aliases"])
    name = safe_name(f"{artist} {date} early + late show")
    parent = early.parent / name
    i = 2
    while parent.exists():
        parent = early.parent / f"{name} ({i})"
        i += 1

    if not fileops.dry_run_enabled():
        # Each original keeps its own row (and its own path); the combined
        # folder gets a row of its own. Undo puts both originals in Review —
        # not back in the queue, or the next sweep would pair them again.
        ids = {}
        for p, which in ((early, "early"), (late, "late")):
            ids[which] = db.upsert_show(str(p), status="combined",
                                        notes=f"the {which} show — combined into {parent.name}")
        pair_id = db.upsert_show(str(parent), status="pending", folder_name=parent.name)
        undo = {"_other_rows": {
            str(ids["early"]): {"status": "review", "current_path": str(early),
                                "notes": "Separated by Undo — the early show of a pair. "
                                         "File it from here, or reprocess it."},
            str(ids["late"]): {"status": "review", "current_path": str(late),
                               "notes": "Separated by Undo — the late show of a pair. "
                                        "File it from here, or reprocess it."},
            str(pair_id): {"status": "rejected", "current_path": "",
                           "notes": "Separated by Undo"},
        }}
        b = fileops.Batch(f"combine {early.name} + {late.name}", kind="pair", dry_run=False,
                          undo_state=undo)
        try:
            b.mkdir(parent)
            b.move(early, parent / early.name)
            b.move(late, parent / late.name)
        except Exception as e:
            if b.id:
                try:
                    fileops.undo(b.id)          # put whatever moved back where it was
                except Exception:
                    pass
            for i in (*ids.values(), pair_id):
                db.update_show(i, status="error", error=f"couldn't combine the early and late show: {e}")
            db.log("error", "pair", f"{early.name} + {late.name}: couldn't combine them: {e}")
            return {"status": "error", "error": str(e)}
        db.log("info", "pair", f"{early.name} + {late.name}: early + late show → {parent.name}/")
        return process_show(parent)

    # dry run: identify both halves where they are and say what would happen
    def log(level, event, detail=""):
        db.log(level, event, f"{early.name} + {late.name}: {detail}")
    ids = [db.upsert_show(str(p), status="processing") for p in (early, late)]
    try:
        result = pairs.combine(identify(early, cfg, log), identify(late, cfg, log), early, late)
        meta, conf, missing = result["meta"], result["confidence"], result["missing"]
        threshold = int(cfg["filing"]["auto_file_threshold"])
        b = fileops.Batch(f"combine {early.name} + {late.name}", kind="pair", dry_run=True)
        b.mkdir(parent)
        b.move(early, parent / early.name)
        b.move(late, parent / late.name)
        common = dict(confidence=conf, meta=meta.to_dict(), provenance=result["provenance"],
                      missing=missing)
        if missing or conf < threshold:
            note = (f"Dry run: early + late show — would combine with {late.name} "
                    f"and send to Review (confidence {conf}"
                    + (f", missing {', '.join(missing)}" if missing else "") + ")")
            dest = ""
        else:
            artist_dir = Path(cfg["paths"]["library_dir"]) / safe_name(meta.album_artist)
            prefix = artist_folder_conventions(artist_dir)[0] if artist_dir.is_dir() else None
            dest = str(artist_dir / canonical_folder_name(meta, cfg, prefix))
            b.note("tags", files=_count_audio(early) + _count_audio(late), album=meta.album_title,
                   discs="1 = early show, 2 = late show")
            note = f"Dry run: early + late show — would combine with {late.name} and file to {dest}"
        out = _plan(ids[0], b, log, note, dest=dest, **common)
        db.update_show(ids[1], status="planned", confidence=conf,
                       notes=f"Dry run: the late show of {early.name} — they'd be filed together",
                       plan=json.dumps({"dest": dest, "ops": b.plan}))
        return out
    except Exception as e:
        for i in ids:
            db.update_show(i, status="error", error=str(e))
        log("error", "error", str(e))
        return {"status": "error", "error": str(e)}


def _plan(show_id: int, b: "fileops.Batch", log, note: str, dest: str = "",
          **fields) -> dict:
    """Record a dry-run outcome: the show stays where it is, untouched, with
    the full list of what Reelarr would have done."""
    plan = {"dest": dest, "ops": b.plan}
    fields.pop("notes", None)
    me = fields.get("meta") or {}
    key = ((me.get("album_artist") or me.get("artist") or "").lower(), me.get("date") or "")
    if dest and all(key):
        # a dry run can't see what earlier plans would have filed, so two
        # copies of one show both plan to file cleanly. Live, the second would
        # meet the first as a duplicate — say so.
        for other in db.list_shows(status="planned", limit=100000):
            try:
                om = json.loads(other.get("meta") or "{}")
            except ValueError:
                continue
            okey = ((om.get("album_artist") or om.get("artist") or "").lower(), om.get("date") or "")
            if other["id"] != show_id and okey == key:
                note += (f" · {other['folder_name']} is also planned for this "
                         f"artist and date — live, whichever arrives second is "
                         f"checked as a duplicate")
                break
    db.update_show(show_id, status="planned", plan=json.dumps(plan), notes=note,
                   **fields)
    log("info", "plan", f"{note} ({len(b.plan)} step(s) planned, nothing changed)")
    return {"status": "planned", "dest": dest, "steps": len(b.plan)}


def replace_show(show_id: int, edited_meta: dict) -> dict:
    """Review action for duplicates: file the newcomer IN PLACE OF the existing
    library copy — album art is transplanted from the old show if the new one
    lacks it, then the old folder goes to trash (recoverable, and undoable
    from Activity). Runs live even in dry-run: it's one show, chosen by you."""
    cfg = config.load()
    row = db.get_show(show_id)
    if not row:
        return {"error": "show not found"}
    show_dir = Path(row["current_path"])
    if not show_dir.exists():
        return {"error": "folder no longer exists"}

    meta = ShowMeta()
    stored = json.loads(row["meta"] or "{}")
    for f in ShowMeta.FIELDS:
        setattr(meta, f, (edited_meta.get(f) if edited_meta.get(f) is not None
                          else stored.get(f, "")) or "")
    meta.tracks = stored.get("tracks", [])
    meta.extras = _extras_from(edited_meta, stored)
    missing = [f for f in REQUIRED_FIELDS if not getattr(meta, f)]
    if missing:
        return {"error": f"still missing: {', '.join(missing)}"}
    problem = _extras_problem(show_dir, meta)
    if problem:
        return {"error": problem}

    def _log(level, event, detail=""):
        db.log(level, event, f"{show_dir.name}: {detail}", show_id)
    _learn_from_brig_edit(stored, meta, _log)

    old = find_duplicate(meta, Path(cfg["paths"]["library_dir"]))
    if not old:
        return {"error": "no existing copy found to replace — use File it"}
    old = Path(old)

    def log(level, event, detail=""):
        db.log(level, event, f"{show_dir.name}: {detail}", show_id)

    art = (None, None)
    if not folder_has_art(show_dir):
        art = extract_art(old)

    old_row = db.get_show_by_filed(str(old))
    undo_state = fileops.show_undo_state(show_id)
    if old_row:
        undo_state["_other_rows"] = {str(old_row["id"]): fileops.show_undo_state(old_row["id"])}
    b = fileops.Batch(f"replace {old.name}", kind="replace", show_id=show_id,
                      dry_run=False, undo_state=undo_state)
    try:
        dest = file_show(show_dir, meta, cfg, log, b)
        if art[0]:
            n = embed_art(dest, *art)
            log("info", "replace", f"album art carried over ({n} file(s))")
        trashed = b.trash(old, f"replaced by {dest.name}")
        log("info", "replace", f"old copy moved to trash: {old.name} → {trashed}")
        # if the canonical name was blocked by the old copy, reclaim it
        m = re.match(r"^(.*) \(\d+\)$", dest.name)
        if m and not (dest.parent / m.group(1)).exists():
            clean = dest.parent / m.group(1)
            b.move(dest, clean)
            dest = clean
        if old_row:
            db.update_show(old_row["id"], status="rejected", filed_path="",
                           notes=f"replaced by upgrade: {dest.name}")
        db.update_show(show_id, status="filed", filed_path=str(dest),
                       current_path=str(dest), meta=meta.to_dict(),
                       confidence=100, missing=[])
        db.log("info", "filed", f"{dest.name} (replaced the old copy)", show_id)
        return {"status": "filed", "dest": str(dest), "replaced": str(old)}
    except Exception as e:
        db.update_show(show_id, status="error", error=str(e))
        return {"error": str(e)}


def toss_duplicates() -> dict:
    """Reject every review-queue show flagged as a duplicate → _duplicates."""
    cfg = config.load()
    tossed = 0
    for row in db.list_shows(status="review", limit=100000):
        if "duplicate" not in (row["notes"] or "").lower():
            continue
        p = Path(row["current_path"])
        if p.exists():
            b = fileops.Batch(f"toss duplicate {p.name}", kind="toss", show_id=row["id"],
                              undo_state=fileops.show_undo_state(row["id"]))
            stash = _stash(p, cfg, "_duplicates", b)
            if b.dry_run:
                tossed += 1
                continue
            db.update_show(row["id"], status="rejected", current_path=str(stash))
        else:
            db.update_show(row["id"], status="rejected")
        tossed += 1
    if fileops.dry_run_enabled():
        db.log("info", "duplicate", f"dry run: would toss {tossed} duplicate(s) — nothing moved")
        return {"tossed": 0, "would_toss": tossed, "dry_run": True}
    db.log("info", "duplicate", f"tossed {tossed} duplicate(s) from review")
    return {"tossed": tossed}


def _extras_from(edited: dict, stored: dict) -> list:
    """Files you marked 'not a song' in Review (names only, never paths)."""
    raw = edited.get("extras") if isinstance(edited.get("extras"), list) else stored.get("extras", [])
    # "" on its own means "you looked, and none of these are extras"
    return [Path(str(x)).name if str(x).strip() else "" for x in (raw or [])][:50]


def _extras_problem(show_dir: Path, meta: ShowMeta) -> str:
    """Your 'not a song' ticks must leave exactly one file per song."""
    if not (meta.extras and meta.tracks) or meta.parts:
        return ""
    from . import align
    p = align.plan(_audio_files(show_dir, AUDIO_EXTS), meta.tracks, extras=meta.extras)
    return "" if p["ok"] else f"Track titles: {p['note']}"


def approve_show(show_id: int, edited_meta: dict) -> dict:
    """User approved (possibly edited) metadata from the review queue."""
    cfg = config.load()
    row = db.get_show(show_id)
    if not row:
        return {"error": "show not found"}
    show_dir = Path(row["current_path"])
    if not show_dir.exists():
        db.update_show(show_id, status="error", error="folder no longer exists")
        return {"error": "folder no longer exists"}

    meta = ShowMeta()
    stored = json.loads(row["meta"] or "{}")
    for f in ShowMeta.FIELDS:
        setattr(meta, f, (edited_meta.get(f) if edited_meta.get(f) is not None
                          else stored.get(f, "")) or "")
    meta.tracks = stored.get("tracks", [])
    meta.extras = _extras_from(edited_meta, stored)
    meta.notes = stored.get("notes", "")
    meta.parts = stored.get("parts", []) or []
    meta.bracket_override = stored.get("bracket_override", "") or ""
    if meta.bracket_override and any(
            (edited_meta.get(f) or "") != (stored.get(f) or "")
            for f in ("source_type", "official", "shnid", "recorder") if edited_meta.get(f) is not None):
        meta.bracket_override = ""        # you edited the source: your bracket wins

    def log(level, event, detail=""):
        db.log(level, event, f"{show_dir.name}: {detail}", show_id)

    missing = [f for f in REQUIRED_FIELDS if not getattr(meta, f)]
    if missing:
        return {"error": f"still missing: {', '.join(missing)}"}
    problem = _extras_problem(show_dir, meta)
    if problem:
        return {"error": problem}

    # learn from your edits: whatever Reelarr guessed vs what you kept.
    # These become standing corrections applied to every future show, exactly
    # like the disk-diff learning pass — but caught the moment you approve.
    _learn_from_brig_edit(stored, meta, log)
    # a setlist you approved from Review is authoritative — seed the corpus
    if meta.tracks and meta.artist:
        library_index.learn_songs(
            (meta.album_artist or meta.artist), _songs_only(meta.tracks), confirmed=True)

    # a single show you approved by hand runs live even in dry-run; it's
    # journalled, so Activity → Undo puts it back in the watch folder
    b = fileops.Batch(f"approve {show_dir.name}", kind="approve", show_id=show_id,
                      dry_run=False, undo_state=fileops.show_undo_state(show_id))
    try:
        dest = file_show(show_dir, meta, cfg, log, b)
        db.update_show(show_id, status="filed", filed_path=str(dest),
                       current_path=str(dest), meta=meta.to_dict(),
                       confidence=100, missing=[])
        db.log("info", "filed", f"{dest.name} (approved from review)", show_id)
        return {"status": "filed", "dest": str(dest)}
    except Exception as e:
        db.update_show(show_id, status="error", error=str(e))
        return {"error": str(e)}


def _learn_from_brig_edit(guessed: dict, final: "ShowMeta", log) -> None:
    """Compare Reelarr's stored guess against the approved metadata and mint
    corrections for any field you changed in Review."""
    # artist: a corrected headliner teaches an artist→artist rule
    g_artist = (guessed.get("host_artist") or guessed.get("artist") or "").strip()
    f_artist = (final.album_artist or final.artist or "").strip()
    if g_artist and f_artist and _norm(g_artist) != _norm(f_artist):
        # Guard: a legitimate correction is a *refinement* (spelling, "&"/"and",
        # a missing "The"), not one band turning into an unrelated one. When the
        # two names share almost nothing, this is almost always a stale or
        # mis-loaded Review form — minting a standing rule from it silently
        # rewrites every future show by that artist. Skip it and flag it.
        import difflib
        sim = difflib.SequenceMatcher(None, _norm(g_artist), _norm(f_artist)).ratio()
        shares_token = bool(set(_norm_words(g_artist)) & set(_norm_words(f_artist)))
        if sim < 0.4 and not shares_token:
            log("warn", "learned",
                f"NOT learning artist rule {g_artist!r} → {f_artist!r} — the "
                f"names are too different ({sim:.0%} similar). If this was a "
                f"real rename, move the show to the right artist folder and run "
                f"Maintenance → Learn from your corrections. If not, the Review "
                f"form may have had the wrong show loaded.")
        else:
            try:
                library_index.correction_set("artist", g_artist, f_artist)
                log("info", "learned", f"artist correction: {g_artist!r} → {f_artist!r}")
            except Exception:
                pass

    # venue: a corrected venue teaches a venue→(venue,city,state) rule, keyed
    # on both the full wrong string and its head segment (parse paths differ)
    g_venue = (guessed.get("venue") or "").strip()
    if g_venue and final.venue and _norm(g_venue) != _norm(final.venue):
        trio = {"venue": final.venue, "city": final.city, "state": final.state}
        keys = {g_venue}
        head = g_venue.split(",")[0].strip()
        if len(head) > 3:
            keys.add(head)
        for k in keys:
            if _norm(k) and _norm(k) != _norm(final.venue):
                try:
                    library_index.correction_set("venue", k, trio)
                except Exception:
                    pass
        log("info", "learned",
            f"venue correction: {g_venue!r} → {final.venue!r}")

    # a corrected venue that now has a city/state also feeds the gazetteer
    if final.venue and final.city:
        try:
            library_index._upsert_venue(library_index._conn(),
                                        final.venue, final.city, final.state)
            library_index._conn().commit()
        except Exception:
            pass

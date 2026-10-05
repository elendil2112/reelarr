"""
Audio source analysis — AUD vs SBD from the waveform, when no text says.

Principle: bootlegs are tracked so applause sits at track edges.
  AUD  — the crowd is AT the mics: track edges nearly as loud as the music
  SBD  — crowd is a distant murmur off the desk: edges 25-40+ dB down,
         often true digital silence between songs

Method: for a handful of mid-show tracks, measure mean loudness of the
song body (middle 20s) vs the final edge (last ~4.5s) with ffmpeg
volumedetect. Average the delta:
  delta <= 15 dB  → AUD        delta >= 26 dB  → SBD
  in between      → inconclusive (caller falls back to the default)

Deliberately conservative: only consulted when no textual evidence exists,
cannot detect MTX or FM, and the wide gray band avoids confident mistakes.
"""
import re
import subprocess
from pathlib import Path

from .metadata import TAGGABLE_EXTS

AUD_MAX_DELTA = 15.0
SBD_MIN_DELTA = 26.0
SILENCE_FLOOR = -70.0      # mean below this = digital silence


def _mean_volume(path: Path, start: float, dur: float):
    """Mean loudness (dBFS) of a window, or None on failure."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-ss", f"{max(start, 0):.2f}", "-t", f"{dur:.2f}",
             "-i", str(path), "-map", "0:a:0",
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _duration(path: Path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def analyze_source(show_dir: Path, max_tracks: int = 4) -> dict:
    """Returns {"source_type": "AUD"|"SBD"|"", "detail": str}."""
    audio = sorted(p for p in show_dir.rglob("*")
                   if p.suffix.lower() in TAGGABLE_EXTS and p.is_file())
    if len(audio) < 2:
        return {"source_type": "", "detail": "too few tracks to sample"}

    # sample mid-show tracks; skip the first (intros/tuning) where possible
    pool = audio[1:-1] or audio
    step = max(1, len(pool) // max_tracks)
    samples = pool[::step][:max_tracks]

    deltas, silences, measured = [], 0, 0
    for track in samples:
        dur = _duration(track)
        if not dur or dur < 45:
            continue
        body = _mean_volume(track, dur / 2 - 10, 20)
        edge = _mean_volume(track, dur - 5.0, 4.5)
        if body is None or edge is None:
            continue
        measured += 1
        if edge <= SILENCE_FLOOR:
            silences += 1
            continue
        deltas.append(body - edge)

    if measured == 0:
        return {"source_type": "", "detail": "could not measure any tracks"}

    # digital silence at boundaries on most sampled tracks = board/master
    if silences >= max(2, measured - 1):
        return {"source_type": "SBD",
                "detail": f"digital silence at {silences}/{measured} track edges"}

    if not deltas:
        return {"source_type": "", "detail": "no usable edge measurements"}

    avg = sum(deltas) / len(deltas)
    detail = (f"edge-to-body delta {avg:.1f} dB across {len(deltas)} track(s)"
              + (f", {silences} silent edge(s)" if silences else ""))
    if avg <= AUD_MAX_DELTA:
        return {"source_type": "AUD", "detail": detail + " → crowd at the mics"}
    if avg >= SBD_MIN_DELTA or (silences and avg >= SBD_MIN_DELTA - 6):
        return {"source_type": "SBD", "detail": detail + " → clean board gaps"}
    return {"source_type": "", "detail": detail + " → inconclusive"}

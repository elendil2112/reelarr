"""Reelarr settings — persisted to /config/settings.json, editable from the GUI.

The file is versioned (see migrations.SETTINGS_STEPS) and always written
atomically with owner-only permissions, because it holds credentials."""
import json
import os
import copy
import threading

from . import paths
from .migrations import write_settings, LATEST
CONFIG_DIR = paths.config_dir()
SETTINGS_PATH = CONFIG_DIR / "settings.json"

DEFAULTS = {
    # ── Paths (inside the container; host paths are set in docker-compose) ──
    "paths": {
        "watch_dir": paths.default_watch_dir(),
        "library_dir": paths.default_library_dir(),
    },
    # ── Watch-folder processing ──
    "watcher": {
        "enabled": True,
        "poll_minutes": 10,          # how often the watch folder is scanned
        "settle_minutes": 5,         # folder must be unchanged this long before processing
        "auto_extract_archives": True,   # unzip archives found inside a show folder
        "pair_wait_minutes": 120,    # an early or late show waits this long for its other half
        "max_extract_gb": 50,        # refuse archives that unpack bigger than this
        "convert_shn_to_flac": True,
        "keep_shn_originals": False,
        "convert_wav_to_flac": True,
        "keep_wav_originals": False,
        "audio_source_analysis": True,   # listen for AUD vs SBD when text says nothing
        "dedupe_formats": True,
        "scrub_junk_files": True,        # trash .torrent/.json/.json.gz/spectrogram pngs
        # FLAC conversion target for WAV/SHN:
        #   preserve — keep the source's bit depth and sample rate (lossless)
        #   cd       — 16-bit/44.1 kHz with dither (how older installs converted)
        "convert_target": "preserve",
    },
    # ── Safety ──
    "safety": {
        "dry_run": True,             # plan every change, make none, until you go live
        "trash_days": 30,            # trashed files are purged after this (0 = never)
        "undo_keep_days": 90,        # journal entries older than this are pruned
    },
    # ── Torrent intake → Deluge ──
    "torrents": {
        "enabled": False,
        "watch_dir": "",             # folder to watch for .torrent files
        "client": "deluge",          # deluge | qbittorrent | transmission
        "url": "",                   # e.g. http://deluge:8112, http://qbittorrent:8080
        "username": "",              # not used by Deluge
        "password": "",              # the client's WEB UI password
        "label": "bootlegs",         # label/category applied to every torrent
        "download_dir": "",          # optional: save path, as the CLIENT sees it
        "add_paused": False,
        "poll_minutes": 5,
        "settle_seconds": 10,        # ignore a .torrent still being written
        "after_add": "move",         # move (to _added/) | delete
        # ── bringing finished downloads back in ──
        "import_completed": False,   # copy finished torrents into the watch folder
        "import_mode": "copy",       # copy (keeps seeding) | move
        "path_map": {},              # {"path the client reports": "path Reelarr sees"}
        "min_age_minutes": 2,        # let a finished download settle this long first
    },
    # ── Confidence / filing policy ──
    "filing": {
        "auto_file_threshold": 85,   # score 0-100 required to file without review
        "duplicate_policy": "upgrade",  # upgrade | review | file_anyway
        "hold_uncertain_titles": True,  # send shows with unvalidated setlists to Review
        "new_artist_policy": "auto",    # auto (create folder) | review
        "default_source_type": "AUD",   # bracket used when no source is detected
        "folder_prefix_style": "slug",  # slug -> gratefuldead2024-06-01 ...; name -> Grateful Dead 2024-06-01 ...
        "official_labels": "",          # comma-separated names of official-release sources
    },
    # ── Permissions (unRAID default: nobody:users) ──
    "permissions": {
        # inside the image these follow the container's PUID/PGID
        "puid": int(os.environ.get("PUID") or 99),
        "pgid": int(os.environ.get("PGID") or 100),
        "dir_mode": "0777",
        "file_mode": "0666",
    },
    # ── External metadata sources ──
    "places": {
        "enabled": True,             # bundled world-city list: fill/fix state & country
        "country_names": {},         # your spellings: {"United Kingdom": "England"}
    },
    "sources": {
        "use_internet_archive": True,
        "use_setlistfm": True,
        "setlistfm_api_key": "",
        "setlistfm_delay_seconds": 1.1,  # pause between backtag lookups (be kind to setlist.fm)
        "use_musicbrainz_genre": True,
    },
    # ── Artist handling ──
    "artists": {
        # etree-style prefix -> full artist name (editable; used when folder names use acronyms)
        "aliases": {
            "gd": "Grateful Dead",
            "jgb": "Jerry Garcia Band",
            "jg": "Jerry Garcia",
            "ph": "Phish",
            "bs": "Billy Strings",
            "bmfs": "Billy Strings",
            "dso": "Dark Star Orchestra",
            "sci": "The String Cheese Incident",
            "wsp": "Widespread Panic",
            "moe": "moe.",
            "um": "Umphrey's McGee",
            "gsw": "Goose",
            "db": "Dead & Company",
            "dac": "Dead & Company",
            "tab": "Trey Anastasio Band",
            "jrad": "Joe Russo's Almost Dead",
            "abb": "The Allman Brothers Band",
            "gov": "Gov't Mule",
            "wst": "Widespread Panic",
            "ymsb": "Yonder Mountain String Band",
            "lt": "Lettuce",
            "sts9": "STS9",
            "kdtu": "Karl Denson's Tiny Universe",
            "rre": "Railroad Earth",
            "lotus": "Lotus",
            "tlg": "The Lil Smokies",
            "gsbg": "Greensky Bluegrass",
            "gsbg.": "Greensky Bluegrass",
        },
        # fuzzy-match cutoff for mapping a parsed artist onto an existing library folder
        "match_cutoff": 0.88,
    },
    # ── Monitoring (Artists) ──
    "monitor": {
        "enabled": True,
        "interval_hours": 12,        # how often monitored artists are checked
        "lookback_days": 30,         # how far back a newly monitored artist looks
    },
    # ── Login ──
    "auth": {
        "mode": "forms",             # forms (login page) | none (your own SSO/proxy auth)
        "session_days": 30,          # how long a browser stays signed in
    },
    "timezone": os.environ.get("TZ") if "/" in (os.environ.get("TZ") or "") else "America/New_York",
    "ui_port_note": "Port is set in docker-compose.yml (host side).",
}

# Settings that are credentials. They are never sent back to the browser:
# GET /api/settings shows MASK in their place, and a save that sends MASK
# back leaves the stored value alone. Providers add their own.
SECRET_FIELDS = {
    ("torrents", "password"),
    ("sources", "setlistfm_api_key"),
}
MASK = "••••••"


def register_defaults(group: str, defaults: dict, secrets=()):
    """Called by an optional provider at import time to add its settings
    group (e.g. its credentials and schedule) without core knowing about it."""
    global _cache
    DEFAULTS[group] = _deep_merge(DEFAULTS.get(group, {}), defaults)
    for key in secrets:
        SECRET_FIELDS.add((group, key))
    _cache = None


def masked(settings: dict) -> dict:
    """A copy safe to send to the browser."""
    out = copy.deepcopy(settings)
    for group, key in SECRET_FIELDS:
        g = out.get(group)
        if isinstance(g, dict) and key in g:
            g[key] = MASK if g[key] else ""
    out.pop("_schema", None)
    return out


def strip_masked(patch: dict) -> dict:
    """Drop secret fields the browser sent back unchanged (still MASK)."""
    out = copy.deepcopy(patch or {})
    for group, key in SECRET_FIELDS:
        g = out.get(group)
        if isinstance(g, dict) and g.get(key) == MASK:
            del g[key]
    return out

_lock = threading.Lock()
_cache = None


# Free-form maps you edit as a whole (one per line in Settings). Saving one
# replaces it, so a line you delete stays deleted.
_REPLACE_WHOLE = {("artists", "aliases"), ("places", "country_names")}


def _deep_merge(base: dict, override: dict, _path: tuple = ()) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) \
                and (_path + (k,)) not in _REPLACE_WHOLE:
            out[k] = _deep_merge(out[k], v, _path + (k,))
        else:
            out[k] = v
    return out


def _persist(data: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data["_schema"] = LATEST["settings.json"]
    write_settings(SETTINGS_PATH, data)


def _load_locked(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    data = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
        except Exception:
            data = {}
    _cache = _deep_merge(DEFAULTS, data)
    return _cache


def load(force: bool = False) -> dict:
    """Load settings, merged over defaults so new keys appear after upgrades."""
    with _lock:
        return copy.deepcopy(_load_locked(force))


def set_key(path: list, value):
    """Set an exact value (no merge) — needed to replace whole dicts like
    a per-artist map where deep-merge can't remove keys."""
    global _cache
    with _lock:
        current = _load_locked()
        d = current
        for k in path[:-1]:
            d = d.setdefault(k, {})
        d[path[-1]] = value
        _persist(current)
        _cache = current
        return copy.deepcopy(current)


def save(new_settings: dict) -> dict:
    """Merge and persist settings; returns the effective settings."""
    global _cache
    with _lock:
        current = _load_locked()
        merged = _deep_merge(current, strip_masked(new_settings))
        _persist(merged)
        _cache = merged
        return copy.deepcopy(merged)

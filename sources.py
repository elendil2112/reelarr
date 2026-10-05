"""External metadata sources: Internet Archive, setlist.fm, MusicBrainz genre.
All failures are soft — network problems never break the pipeline."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .metadata import ShowMeta, extract_date, normalise_source_type, extract_official

_UA = "Reelarr/1.0 (personal live-music archiver)"


# ── Internet Archive ─────────────────────────────────────────────────────────

def is_ia_identifier(name: str) -> bool:
    if not name or re.search(r"[ ,()\[\]]", name):
        return False
    if len(name) > 200 or len(name) < 8:
        return False
    return bool(re.search(r"\d{2,4}[-._]\d{2}[-._]\d{2}", name))


def fetch_ia(identifier: str, timeout: int = 12):
    if not is_ia_identifier(identifier):
        return None
    url = f"https://archive.org/metadata/{urllib.parse.quote(identifier, safe='')}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    if not data or "metadata" not in data:
        return None

    md, files = data.get("metadata", {}), data.get("files", [])
    meta = ShowMeta()

    def first(*keys):
        for k in keys:
            v = md.get(k)
            if isinstance(v, list):
                v = v[0] if v else None
            if v:
                return str(v).strip()
        return ""

    meta.artist = first("creator", "artist", "band")
    raw_date = first("date")
    if raw_date:
        meta.date = extract_date(raw_date) or raw_date[:10]

    coverage = first("coverage")
    venue_field = first("venue")
    if venue_field:
        meta.venue = venue_field
    if coverage:
        parts = [p.strip() for p in coverage.split(",")]
        if not meta.venue and len(parts) >= 3:
            meta.venue = parts[0]
            parts = parts[1:]
        if len(parts) >= 2:
            meta.city, meta.state = parts[0], parts[-1]
        elif parts:
            meta.city = parts[0]

    src = first("source", "lineage")
    if src:
        meta.source_type = normalise_source_type(src)
        meta.official = extract_official(src, allow_generic=False)
    meta.notes = first("description")[:500]

    audio = [f for f in files if f.get("format", "").lower() in
             ("flac", "vbr mp3", "mp3", "ogg vorbis", "shorten")]
    audio.sort(key=lambda f: (str(f.get("track", "99")).zfill(4), f.get("name", "")))
    seen = set()
    n = 0
    for f in audio:
        title = f.get("title") or ""
        stem = re.sub(r"\.\w+$", "", f.get("name", ""))
        if not title or stem in seen:
            continue
        seen.add(stem)
        n += 1
        try:
            num = int(str(f.get("track", n)).split("/")[0])
        except Exception:
            num = n
        meta.tracks.append({"num": num, "title": title, "disc": 1})
    return meta


# ── setlist.fm ───────────────────────────────────────────────────────────────

def fetch_setlistfm(artist: str, date: str, api_key: str, timeout: int = 12):
    if not (api_key and artist and date):
        return None
    try:
        y, m, d = date.split("-")
    except ValueError:
        return None
    params = urllib.parse.urlencode(
        {"artistName": artist, "date": f"{d}-{m}-{y}", "p": "1"})
    req = urllib.request.Request(
        f"https://api.setlist.fm/rest/1.0/search/setlists?{params}",
        headers={"x-api-key": api_key, "Accept": "application/json",
                 "Accept-Language": "en", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None

    setlists = data.get("setlist", [])
    if not setlists:
        return None
    sl = setlists[0]
    meta = ShowMeta()
    meta.artist = sl.get("artist", {}).get("name", artist)
    raw = sl.get("eventDate", "")
    try:
        dd, mm, yyyy = raw.split("-")
        meta.date = f"{yyyy}-{mm}-{dd}"
    except ValueError:
        meta.date = date

    venue = sl.get("venue", {})
    meta.venue = venue.get("name", "")
    city = venue.get("city", {})
    meta.city = city.get("name", "")
    meta.state = _setlistfm_state(city)

    n = 0
    for s in sl.get("sets", {}).get("set", []):
        disc = 99 if s.get("encore") else 1
        for song in s.get("song", []):
            name = song.get("name", "").strip()
            if name:
                n += 1
                meta.tracks.append({"num": n, "title": name, "disc": disc})
    return meta


def _setlistfm_state(city: dict) -> str:
    """setlist.fm gives a stateCode everywhere (Munich → 'BY'). Only the US
    and Canada use those in Reelarr's tags; elsewhere it's the country."""
    cc = (city.get("country", {}) or {}).get("code", "").upper()
    if cc in ("US", "CA", ""):
        return city.get("stateCode", "") or city.get("state", "") or cc
    from . import places
    return places.country_name(cc) or (city.get("country", {}) or {}).get("name", "") or cc


def _slf_result_to_meta(sl) -> ShowMeta:
    meta = ShowMeta()
    meta.artist = sl.get("artist", {}).get("name", "")
    raw = sl.get("eventDate", "")
    try:
        dd, mm, yyyy = raw.split("-")
        meta.date = f"{yyyy}-{mm}-{dd}"
    except ValueError:
        pass
    venue = sl.get("venue", {})
    meta.venue = venue.get("name", "")
    city = venue.get("city", {})
    meta.city = city.get("name", "")
    meta.state = _setlistfm_state(city)
    n = 0
    for s in sl.get("sets", {}).get("set", []):
        disc = 99 if s.get("encore") else 1
        for song in s.get("song", []):
            name = song.get("name", "").strip()
            if name:
                n += 1
                meta.tracks.append({"num": n, "title": name, "disc": disc})
    return meta


def fetch_setlistfm_reverse(date: str, city: str, venue: str, api_key: str,
                            known_titles=None, timeout: int = 12):
    """No artist? Search setlist.fm by city+date and match the venue.
    If several shows happened in town that night, pick the one whose setlist
    overlaps our known track titles."""
    if not (api_key and date and (city or venue)):
        return None
    try:
        y, m, d = date.split("-")
    except ValueError:
        return None
    q = {"date": f"{d}-{m}-{y}", "p": "1"}
    if city:
        q["cityName"] = city
    params = urllib.parse.urlencode(q)
    req = urllib.request.Request(
        f"https://api.setlist.fm/rest/1.0/search/setlists?{params}",
        headers={"x-api-key": api_key, "Accept": "application/json",
                 "Accept-Language": "en", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            setlists = json.loads(r.read()).get("setlist", [])
    except Exception:
        return None
    if not setlists:
        return None

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    import difflib
    candidates = []
    for sl in setlists:
        sl_venue = sl.get("venue", {}).get("name", "")
        sim = difflib.SequenceMatcher(None, norm(venue), norm(sl_venue)).ratio() \
            if venue else 0.0
        candidates.append((sim, sl))
    candidates.sort(key=lambda c: c[0], reverse=True)

    # venue named and matched → trust it
    if venue and candidates[0][0] >= 0.72:
        pool = [sl for sim, sl in candidates if sim >= 0.72]
    elif not venue:
        pool = [sl for _, sl in candidates]
    else:
        return None

    if len(pool) > 1 and known_titles:
        known = {norm(t) for t in known_titles}

        def overlap(sl):
            titles = {norm(song.get("name", ""))
                      for s in sl.get("sets", {}).get("set", [])
                      for song in s.get("song", [])}
            return len(known & titles)
        pool.sort(key=overlap, reverse=True)
        if overlap(pool[0]) == 0:
            return None      # nothing corroborates — don't guess
    elif len(pool) > 1:
        return None          # ambiguous and no way to disambiguate

    return _slf_result_to_meta(pool[0])


# ── MusicBrainz genre ────────────────────────────────────────────────────────

_genre_cache: dict = {}
_MB_BLOCKLIST = {"seen live", "live", "concerts", "bootleg", "american",
                 "british", "male vocalists", "female vocalists"}


def fetch_mb_genre(artist_name: str, timeout: int = 10) -> str:
    slug = re.sub(r"[^a-z0-9]", "", artist_name.lower())
    if slug in _genre_cache:
        return _genre_cache[slug]
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
        q = urllib.parse.quote(f'artist:"{artist_name}"')
        req = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/artist?query={q}&fmt=json", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            artists = json.loads(r.read()).get("artists", [])
        if not artists:
            raise ValueError
        mbid = artists[0].get("id", "")
        time.sleep(1.1)   # MB anonymous rate limit
        req = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags&fmt=json",
            headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        genres = data.get("genres", []) or data.get("tags", [])
        genre = ""
        for g in genres:
            name = g.get("name", "").strip().lower()
            if name and name not in _MB_BLOCKLIST:
                genre = name.title()
                break
    except Exception:
        genre = ""
    _genre_cache[slug] = genre
    return genre

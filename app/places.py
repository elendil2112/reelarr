"""Where a show was: cities, US states, Canadian provinces and countries.

Reelarr's tags hold `Venue, City, ST`. Inside the US and Canada, ST is the
two-letter state or province code. Everywhere else, the country takes its
place, spelled out in full:

    Fillmore, San Francisco, CA          Paradiso, Amsterdam, Netherlands
    Massey Hall, Toronto, ON             E-Werk, Cologne, Germany

This module answers three questions, using a bundled copy of GeoNames' list of
every city with 1,000+ people (app/data/places.tsv.gz, © GeoNames, CC BY 4.0)
plus the cities in your own library:

    resolve("Amsterdam", "NL")    → "Netherlands"   (no Amsterdam in Newfoundland)
    resolve("St. John's", "NL")   → "NL"            (there is one)
    resolve("Munich", "Bavaria")  → "Germany"
    resolve("Toronto", "")        → "ON"
    resolve("Portland", "")       → ""              (Oregon or Maine? not guessing)

Nothing here touches the network.
"""
import gzip
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
DOMINANCE = 15          # a city name is unambiguous when its biggest place is this
                        # many times bigger than the next one with that name

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "ON": "Ontario",
    "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
    "NT": "Northwest Territories", "NU": "Nunavut",
}
_EXTRA_STATE_NAMES = {"washington dc": "DC", "washington d c": "DC", "dc": "DC",
                      "newfoundland": "NL", "labrador": "NL", "pei": "PE",
                      "yukon territory": "YT", "quebec city": "", "nwt": "NT",
                      "mass": "MA", "calif": "CA", "penn": "PA", "penna": "PA", "wash": "WA"}


def fold(s: str) -> str:
    """Lower-case, accents off, letters and digits only: 'Zürich' → 'zurich'.
    Must match tools/build_places.py."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ß", "ss").replace("ø", "o").replace("Ø", "o").replace("ł", "l") \
         .replace("Ł", "l").replace("æ", "ae").replace("đ", "d")
    s = re.sub(r"^(saint|st)[ .-]+", "st ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


# ── Data, loaded on first use ────────────────────────────────────────────────

_lock = threading.Lock()
_cities = None          # folded name → [(country, admin, population)]
_country_name = {}      # ISO2 → name for the tag
_country_alias = {}     # folded alias → ISO2
_state_name = {}        # folded US state / CA province name → code


def _load():
    global _cities
    if _cities is not None:
        return
    with _lock:
        if _cities is not None:
            return
        for code, name in {**US_STATES, **CA_PROVINCES}.items():
            _state_name[fold(name)] = code
        for k, v in _EXTRA_STATE_NAMES.items():
            if v:
                _state_name[fold(k)] = v
        with (DATA / "countries.tsv").open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                code, name, aliases = line.rstrip("\n").split("\t")
                _country_name[code] = name
                for a in aliases.split("|") + [name]:
                    _country_alias.setdefault(fold(a), code)
        cities = {}
        try:
            with gzip.open(DATA / "places.tsv.gz", "rt", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    k, cc, admin, pop, _gid = line.rstrip("\n").split("\t")
                    cities.setdefault(k, []).append((cc, admin, int(pop or 0)))
        except OSError:
            pass            # no data file: codes and country names still work
        _cities = cities


def country_name(code: str) -> str:
    """'DE' → 'Germany', with the user's own spellings applied."""
    _load()
    name = _country_name.get((code or "").upper(), "")
    return _override(name) if name else ""


def _override(name: str) -> str:
    try:
        from . import config
        ren = (config.load().get("places", {}) or {}).get("country_names", {}) or {}
    except Exception:
        ren = {}
    for k, v in ren.items():
        if fold(k) == fold(name) and v:
            return v
    return name


def is_country(state: str) -> bool:
    """True for a full country name as Reelarr writes it (incl. your renames)."""
    _load()
    f = fold(state)
    if not f or len(state.strip()) <= 3:
        return False
    if f in _country_alias:
        return True
    try:
        from . import config
        ren = (config.load().get("places", {}) or {}).get("country_names", {}) or {}
        return any(fold(v) == f for v in ren.values())
    except Exception:
        return False


# ── Cities ───────────────────────────────────────────────────────────────────

def _key(city: str) -> str:
    c = re.sub(r"\s*\(.*?\)\s*", " ", city or "")        # "Paris (Le Zénith)"
    return fold(c)


def places_named(city: str) -> list:
    """Every (country, US state / CA province, population) called this,
    biggest first."""
    _load()
    hits = list(_cities.get(_key(city), []))
    # "New York City" / "Mexico City" / "Quebec City" are listed without "City"
    if not hits and _key(city).endswith("city"):
        hits = list(_cities.get(_key(city)[:-4], []))
    return sorted(hits, key=lambda h: -h[2])


def _place_tag(cc: str, admin: str) -> str:
    if cc == "US" or cc == "PR":
        return admin if cc == "US" else "PR"
    if cc == "CA":
        return admin
    return country_name(cc)


def _dominant(hits: list, ratio: float = DOMINANCE):
    """The one place a bare city name means, or None if it's a toss-up."""
    best = {}
    for cc, admin, pop in hits:
        k = (cc, admin if cc in ("US", "CA") else "")
        best[k] = max(best.get(k, 0), pop)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if len(ranked) == 1 or ranked[0][1] >= ratio * max(ranked[1][1], 1):
        return ranked[0][0]
    return None


def _library_vote(city: str) -> str:
    """Where *your* library puts this city, if it's consistent about it."""
    try:
        from . import library_index
        votes = library_index.city_states(city)
    except Exception:
        return ""
    if not votes:
        return ""
    norm = Counter()
    for st, n in votes.items():
        s = normalise(st)
        if s:
            norm[s] += n
    if not norm:
        return ""
    (top, n), total = norm.most_common(1)[0], sum(norm.values())
    return top if n >= 2 and n >= 0.8 * total else ""


# ── States ───────────────────────────────────────────────────────────────────

def normalise(state: str) -> str:
    """Spell a state the house way without knowing the city:
    'California' → 'CA' · 'Ontario' → 'ON' · 'Deutschland' → 'Germany' ·
    'England' → 'United Kingdom'. Two-letter codes are left alone (is 'DE'
    Delaware or Germany? only the city can say), as is anything unknown."""
    _load()
    s = (state or "").strip().strip(".").strip()
    if not s:
        return ""
    f = fold(s)
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    if f in _state_name:
        return _state_name[f]
    cc = _country_alias.get(f)
    if cc and cc not in ("US", "CA"):
        return country_name(cc)
    return s


def resolve(city: str, state: str) -> str:
    """The state/country tag for a show in `city`, given whatever `state` the
    sources offered (possibly nothing). Returns the input, normalised, when
    there's no good reason to change it."""
    _load()
    city = (city or "").strip()
    raw = (state or "").strip().strip(".").strip()
    hits = places_named(city) if city else []

    if raw:
        f, up = fold(raw), raw.upper()
        if re.fullmatch(r"[A-Za-z]{2}", raw):
            # US state, Canadian province or a country? Whichever has this city.
            if up in US_STATES and (not hits or any(h[0] in ("US", "PR") and _place_tag(*h[:2]) == up for h in hits)):
                return up
            if up in CA_PROVINCES and any(h[0] == "CA" and h[1] == up for h in hits):
                return up
            cc = "GB" if up == "UK" else up
            if cc in _country_name and cc not in ("US", "CA") and any(h[0] == cc for h in hits):
                return country_name(cc)
            if cc == "US" or cc == "CA":        # 'Boston, US' → the state, if clear
                inside = [h for h in hits if h[0] == cc]
                d = _dominant(inside)
                return _place_tag(*d) if d else ""
            return up if (up in US_STATES or up in CA_PROVINCES) else raw
        if f in _state_name:
            return _state_name[f]
        cc = _country_alias.get(f)
        if cc in ("US", "CA"):                  # 'Toronto, Canada' → 'ON'
            inside = [h for h in hits if h[0] == cc]
            d = _dominant(inside)
            return _place_tag(*d) if d else ("" if cc == "US" else raw)
        if cc:
            return country_name(cc)
        if is_country(raw):
            return raw
        # a region we don't track ('Bavaria', 'Catalonia'): the country, if the
        # city is clearly abroad. A named region is evidence it isn't in the US
        # or Canada, so a clear-enough leader will do.
        d = _dominant([h for h in hits if h[0] not in ("US", "CA", "PR")], ratio=3)
        if d and d[0] not in ("US", "CA", "PR"):
            return country_name(d[0])
        return raw

    if not city:
        return ""
    mine = _library_vote(city)
    if mine:
        return mine
    d = _dominant(hits)
    return _place_tag(*d) if d else ""


def _is_city_not_region(s: str) -> bool:
    _load()
    f = fold(s)
    if re.fullmatch(r"[A-Za-z]{2,3}", s.strip()) or f in _state_name or f in _country_alias:
        return False
    return bool(places_named(s))


def apply(meta, provenance: dict = None) -> bool:
    """Resolve meta.state in place from meta.city. True if it changed."""
    try:
        from . import config
        if not (config.load().get("places", {}) or {}).get("enabled", True):
            return False
    except Exception:
        pass
    # "Concertgebouw, Amsterdam" parses as city 'Concertgebouw', state
    # 'Amsterdam'. When the 'state' is really a city (and the 'city' isn't),
    # shift everything one place left.
    st = (meta.state or "").strip()
    if st and not getattr(meta, "venue", "") and _is_city_not_region(st) \
            and not places_named(meta.city):
        meta.venue, meta.city, meta.state = meta.city, st, ""
        if provenance is not None:
            provenance["venue"] = provenance.get("city", "") + "→places"
            provenance["city"] = "places"
    before = meta.state or ""
    after = resolve(meta.city, before)
    if after != before:
        meta.state = after
        if provenance is not None:
            provenance["state"] = "places" if not before else provenance.get("state", "places")
        return True
    return False

"""Build app/data/places.tsv.gz and app/data/countries.tsv.

Run this only to refresh the bundled data; Reelarr itself just reads the
output and needs neither the input file nor pycountry.

    pip install pycountry
    # cities1000.txt from https://download.geonames.org/export/dump/cities1000.zip
    # (or the npm package cities-with-1000, which ships the same file)
    python tools/build_places.py /path/to/cities1000.txt

Place data © GeoNames (https://www.geonames.org), CC BY 4.0.
"""
import gzip
import re
import sys
import unicodedata
from pathlib import Path

import pycountry

OUT = Path(__file__).resolve().parent.parent / "app" / "data"
ALT_MIN_POP = 50_000      # exonyms ("Cologne", "Munich") for cities this big and up

# Canada's GeoNames admin1 codes → the postal abbreviations tapers write
CA_PROVINCES = {"01": "AB", "02": "BC", "03": "MB", "04": "NB", "05": "NL", "07": "NS",
                "08": "ON", "09": "PE", "10": "QC", "11": "SK", "12": "YT", "13": "NT",
                "14": "NU"}

# The name that goes in the tag. pycountry's formal names ("Korea, Republic of")
# aren't what anyone writes on a tape label.
NAME_OVERRIDES = {
    "BO": "Bolivia", "BN": "Brunei", "CD": "Democratic Republic of the Congo",
    "CG": "Republic of the Congo", "CZ": "Czech Republic", "FM": "Micronesia",
    "GB": "United Kingdom", "IR": "Iran", "KP": "North Korea", "KR": "South Korea",
    "LA": "Laos", "MD": "Moldova", "NL": "Netherlands", "PS": "Palestine",
    "RU": "Russia", "SY": "Syria", "TW": "Taiwan", "TZ": "Tanzania", "TR": "Turkey",
    "VA": "Vatican City", "VE": "Venezuela", "VN": "Vietnam", "CI": "Ivory Coast",
    "MK": "North Macedonia", "SZ": "Eswatini", "CV": "Cape Verde",
}
# Other ways people write a country, beyond ISO codes and pycountry's names
EXTRA_ALIASES = {
    "GB": ["UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales",
           "Northern Ireland", "N. Ireland", "ENG", "SCO"],
    "NL": ["Holland", "Nederland", "The Netherlands", "NED"],
    "DE": ["Deutschland", "West Germany", "East Germany", "W. Germany", "GER", "BRD"],
    "CH": ["Schweiz", "Suisse", "Svizzera", "SUI"],
    "AT": ["Osterreich", "Österreich"],
    "ES": ["Espana", "España"], "IT": ["Italia"], "BR": ["Brasil"],
    "MX": ["Méjico"], "JP": ["Nippon", "Nihon"], "DK": ["Danmark", "DEN"],
    "SE": ["Sverige"], "NO": ["Norge"], "FI": ["Suomi"], "BE": ["Belgie", "België", "Belgique"],
    "IE": ["Eire", "Éire", "Republic of Ireland"], "CZ": ["Czechia", "Czechoslovakia"],
    "PT": ["POR"], "HR": ["CRO", "Hrvatska"], "GR": ["Hellas"], "IS": ["Ísland"],
    "KR": ["Korea"], "AU": ["Oz"], "NZ": ["Aotearoa"], "RU": ["USSR", "Soviet Union"],
}


def fold(s: str) -> str:
    """Lower-case, accents off, letters and digits only: 'Zürich' → 'zurich'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ß", "ss").replace("ø", "o").replace("Ø", "o").replace("ł", "l") \
         .replace("Ł", "l").replace("æ", "ae").replace("đ", "d")
    s = re.sub(r"^(saint|st)[ .-]+", "st ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


def latin(s: str) -> bool:
    return bool(s) and all(ord(c) < 0x250 or c in "’'" for c in s)


def countries():
    rows = {}
    for c in pycountry.countries:
        name = NAME_OVERRIDES.get(c.alpha_2) or getattr(c, "common_name", None) or c.name
        aliases = {c.alpha_2, c.alpha_3, c.name, name,
                   getattr(c, "common_name", "") or "", getattr(c, "official_name", "") or ""}
        aliases |= set(EXTRA_ALIASES.get(c.alpha_2, []))
        rows[c.alpha_2] = (name, sorted(a for a in aliases if a))
    return rows


def main(src: Path):
    OUT.mkdir(parents=True, exist_ok=True)
    cs = countries()
    with (OUT / "countries.tsv").open("w", encoding="utf-8") as f:
        f.write("# code\tname\taliases (| separated). From pycountry (ISO 3166) + Reelarr's own.\n")
        for code, (name, aliases) in sorted(cs.items()):
            f.write(f"{code}\t{name}\t{'|'.join(aliases)}\n")

    seen, rows = set(), []
    for line in src.open(encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 15:
            continue
        gid, name, ascii_, alts, cc, admin = p[0], p[1], p[2], p[3], p[8], p[10]
        try:
            pop = int(p[14] or 0)
        except ValueError:
            pop = 0
        if cc == "CA":
            admin = CA_PROVINCES.get(admin, "")
        elif cc != "US":
            admin = ""
        names = {name, ascii_}
        if pop >= ALT_MIN_POP:
            names |= {a for a in alts.split(",") if latin(a) and len(a) > 3}
        for n in names:
            k = fold(n)
            if len(k) < 2 or (k, gid) in seen:
                continue
            seen.add((k, gid))
            rows.append((k, cc, admin, pop, gid))
    rows.sort()
    with gzip.open(OUT / "places.tsv.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("# folded name\tcountry\tUS state / CA province\tpopulation\tgeonameid — "
                "© GeoNames (geonames.org), CC BY 4.0\n")
        for k, cc, admin, pop, gid in rows:
            f.write(f"{k}\t{cc}\t{admin}\t{pop}\t{gid}\n")
    print(f"{len(rows):,} names for {len({r[4] for r in rows}):,} places; {len(cs)} countries")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

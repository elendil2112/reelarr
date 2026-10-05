"""The Live Music Archive as an indexer."""
import urllib.parse

from .. import lma
from ..metadata import infer_source_type, normalise_source_type
from .base import Indexer, IndexerError, Release

import re
import time

_SRC_RE = re.compile(r"(?:^|[.\-_ \[(])(d?sbd|sdb|soundboard|matrix|mtx|fob|aud|audience|fm)(?:$|[.\-_ \])])",
                     re.IGNORECASE)
_AUDIO_FORMATS = ("flac", "shorten", "wave", "mp3", "ogg", "24bit")


def _source_type(row: dict) -> str:
    m = _SRC_RE.search(row.get("identifier", "")) or _SRC_RE.search(row.get("title", ""))
    if m:
        return normalise_source_type(m.group(1))
    return infer_source_type(row.get("source", "")) or ""


def _release(row: dict) -> Release:
    ident = row.get("identifier", "")
    return Release(
        indexer="lma", id=ident, title=row.get("title", ""), artist=row.get("artist", ""),
        date=row.get("date", ""), venue=row.get("venue", ""), location=row.get("location", ""),
        source_type=_source_type(row), formats=row.get("formats", []),
        added=row.get("added", ""), downloads=int(row.get("downloads") or 0),
        url=f"{lma.ARCHIVE}/details/{urllib.parse.quote(ident)}",
        notes=row.get("source", ""), owned=bool(row.get("owned")))


def _quote(s: str) -> str:
    return '"' + (s or "").replace('"', "") + '"'


class LiveMusicArchive(Indexer):
    name = "lma"
    label = "Live Music Archive"
    homepage = "https://archive.org/details/etree"

    def search(self, query, page=1, rows=25, sort=""):
        try:
            out = lma.lookup(query, rows=rows, page=page)
        except lma.LmaError as e:
            raise IndexerError(str(e)) from None
        out["results"] = [_release(r) for r in out.get("results", [])]
        return out

    def _artist_query(self, artist, collection=""):
        if collection:
            return f"collection:({_quote(collection)}) AND mediatype:etree"
        return f"creator:({_quote(artist)}) AND mediatype:etree"

    def new_for_artist(self, artist, since="", rows=100, collection="", limit=1000):
        """Every upload added on or after `since`, newest first, across as many
        pages as it takes (up to `limit`). `collection` (e.g. GratefulDead) is
        more exact than matching the creator field."""
        q = self._artist_query(artist, collection)
        if since:
            q += f" AND addeddate:[{since[:10]} TO null]"
        docs, page = [], 1
        try:
            while len(docs) < limit:
                data = lma._get_json(lma._search_url(q, rows, page, "addeddate desc"))
                batch = data.get("response", {}).get("docs", []) or []
                docs += batch
                total = int(data.get("response", {}).get("numFound") or 0)
                if len(batch) < rows or len(docs) >= total:
                    break
                page += 1
                time.sleep(lma.DELAY)
        except lma.LmaError as e:
            raise IndexerError(str(e)) from None
        rows_ = [lma._row(d) for d in docs[:limit]]
        return [_release(r) for r in lma.mark_owned(rows_)]

    def count_for_artist(self, artist, collection=""):
        """How many recordings archive.org holds for this name, ever."""
        try:
            data = lma._get_json(lma._search_url(self._artist_query(artist, collection), 1, 1, "addeddate desc"))
        except lma.LmaError as e:
            raise IndexerError(str(e)) from None
        return int(data.get("response", {}).get("numFound") or 0)

    def similar_names(self, artist, rows=60):
        """When the exact name finds nothing: the creator names (and counts)
        that a free-text search for it turns up."""
        try:
            data = lma._get_json(lma._search_url(f"({_quote(artist)}) AND mediatype:etree",
                                                 rows, 1, "downloads desc"))
        except lma.LmaError:
            return []
        counts = {}
        for d in data.get("response", {}).get("docs", []) or []:
            c = lma._clean(d.get("creator")).strip()
            if c:
                counts[c] = counts.get(c, 0) + 1
        return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:5]

    def check_downloadable(self, release):
        """Some LMA items are stream-only at the band's request (many Grateful
        Dead soundboards among them). Their audio files are marked private, and
        the .torrent leaves them out. Grabbing one would fetch only text files,
        so say so instead."""
        try:
            meta = lma._get_json(lma.METADATA_API.format(ident=release.id), timeout=20)
        except lma.LmaError as e:
            return f"couldn't check with archive.org: {e}"
        if meta.get("is_dark"):
            return "withdrawn from archive.org"
        audio = [f for f in meta.get("files", []) or []
                 if any(a in (f.get("format") or "").lower() for a in _AUDIO_FORMATS)]
        if not audio:
            return "no audio files listed"
        if all(str(f.get("private", "")).lower() == "true" for f in audio):
            return "stream-only on archive.org (the band or taper doesn't allow downloads)"
        if not any(f.get("format") == "Archive BitTorrent" or (f.get("name") or "").endswith(".torrent")
                   for f in meta.get("files", []) or []):
            return "no .torrent available"
        return ""

    def grab(self, release_ids):
        try:
            return lma.grab(release_ids)
        except lma.LmaError as e:
            raise IndexerError(str(e)) from None

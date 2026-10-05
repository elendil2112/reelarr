"""The indexer interface."""
from dataclasses import asdict, dataclass, field


class IndexerError(Exception):
    pass


LOSSLESS = {"flac", "flac24", "shn", "wav", "alac", "ape", "wv"}


@dataclass
class Release:
    """One recording an indexer knows about."""
    indexer: str
    id: str                       # indexer-specific identifier
    title: str = ""
    artist: str = ""
    date: str = ""                # YYYY-MM-DD of the performance
    venue: str = ""
    location: str = ""
    source_type: str = ""         # SBD | AUD | MTX | SBD.FM | AUD.FOB | ""
    formats: list = field(default_factory=list)   # normalised: flac, flac24, shn, mp3, ogg…
    added: str = ""               # when it appeared on the indexer (ISO)
    downloads: int = 0
    url: str = ""                 # human page
    notes: str = ""               # free text: lineage, taper, etc.
    owned: bool = False           # set by Reelarr: already in the library
    restricted: str = ""          # set when the indexer won't let it be downloaded

    @property
    def lossless(self) -> bool:
        return any(f in LOSSLESS for f in self.formats)

    @property
    def hires(self) -> bool:
        return "flac24" in self.formats

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lossless"] = self.lossless
        d["hires"] = self.hires
        return d


class Indexer:
    name = "base"
    label = "Indexer"
    homepage = ""

    def search(self, query: str, page: int = 1, rows: int = 25, sort: str = "") -> dict:
        """Free text. Returns {"total": n, "page": p, "results": [Release]}."""
        raise NotImplementedError

    def new_for_artist(self, artist: str, since: str = "", rows: int = 50,
                       collection: str = "") -> list:
        """Recordings by this artist that appeared on the indexer after
        `since` (ISO date), newest first."""
        raise NotImplementedError

    def count_for_artist(self, artist: str, collection: str = ""):
        """Total recordings for this artist ever, or None if unknown."""
        return None

    def similar_names(self, artist: str) -> list:
        """Names the indexer does know that look like this one."""
        return []

    def check_downloadable(self, release: Release) -> str:
        """'' if Reelarr may grab it, else the reason it can't."""
        return ""

    def grab(self, release_ids: list) -> dict:
        """Fetch whatever the download client needs (e.g. a .torrent) into
        the torrent folder. Returns {"grabbed": n, "skipped": n, "failed": n}."""
        raise NotImplementedError

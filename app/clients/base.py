"""The download-client interface every client implements.

Reelarr doesn't care which torrent client you run. It hands a .torrent (or a
magnet) to whatever client is configured, asks for a label/category to be put
on it, and gets back an id. Everything client-specific — how you log in, what
labels are called, what the API wants — lives in the subclass.

Adding another client means writing one file: subclass DownloadClient,
implement connect/labels/add_torrent_file/add_magnet, and register it in
clients/__init__.py.
"""
import hashlib
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


def _int(v):
    try:
        return None if v is None or v == "" else int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


class DownloadClientError(Exception):
    """Anything that stopped us handing a torrent over."""


@dataclass
class TorrentInfo:
    """One torrent, described the same way whatever client it came from."""
    id: str = ""
    name: str = ""
    save_path: str = ""      # folder holding the content, AS THE CLIENT SEES IT
    content_path: str = ""   # full path to the file/folder, when the client says
    progress: float = 0.0    # 0.0 - 1.0
    finished: bool = False
    label: str = ""
    state: str = ""          # the client's own state name
    # For the Queue page. None = the client didn't say.
    status: str = ""         # downloading | queued | paused | stalled | checking |
                             # metadata | seeding | error  (same words for every client)
    queue: int = None        # position in the client's download queue, 1 = next
    size: int = None         # bytes wanted
    done: int = None         # bytes downloaded
    dl_speed: int = None     # bytes/s
    ul_speed: int = None     # bytes/s
    eta: int = None          # seconds; None = unknown / never
    seeds: int = None
    peers: int = None
    ratio: float = None
    added: float = None      # epoch seconds

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def source(self) -> str:
        """Where the finished content actually is, client-side."""
        if self.content_path:
            return self.content_path
        if self.save_path and self.name:
            return self.save_path.rstrip("/\\") + "/" + self.name
        return self.save_path or ""


@dataclass
class AddResult:
    torrent_id: str = ""
    already: bool = False           # the client already had this torrent
    label: str = ""                 # the label/category actually applied
    name: str = ""                  # human-readable torrent name, if known


# ── Torrent file parsing (just enough bencode to get the infohash) ───────────

MAX_TORRENT_BYTES = 20 * 2**20     # real .torrent files are KBs; refuse anything absurd
_MAX_DEPTH = 64


def _bdecode(data: bytes, i: int, depth: int = 0):
    """Minimal bencode reader, hardened: a malformed or hostile .torrent
    raises ValueError instead of recursing forever or reading past the end."""
    if depth > _MAX_DEPTH:
        raise ValueError("torrent nested too deeply")
    if i >= len(data):
        raise ValueError("truncated torrent")
    t = data[i:i + 1]
    if t == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if t == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise ValueError("truncated torrent")
            v, i = _bdecode(data, i, depth + 1)
            out.append(v)
        return out, i + 1
    if t == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise ValueError("truncated torrent")
            k, i = _bdecode(data, i, depth + 1)
            v, i = _bdecode(data, i, depth + 1)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    if j - i > 12 or not data[i:j].isdigit():
        raise ValueError("bad string length in torrent")
    n = int(data[i:j])
    if j + 1 + n > len(data):
        raise ValueError("truncated torrent")
    return data[j + 1:j + 1 + n], j + 1 + n


def torrent_infohash(data: bytes) -> str:
    """SHA-1 of the raw info dict — the torrent's identity, computed the same
    way every client computes it. Lets us label a torrent the client already
    had, and recognise duplicates before we even ask."""
    if data[:1] != b"d":
        raise ValueError("not a torrent file")
    if len(data) > MAX_TORRENT_BYTES:
        raise ValueError("file too large to be a .torrent")
    i = 1
    try:
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise ValueError("truncated torrent")
            k, i = _bdecode(data, i)
            start = i
            _v, i = _bdecode(data, i)
            if k == b"info":
                return hashlib.sha1(data[start:i]).hexdigest()
    except (IndexError, RecursionError, TypeError) as e:
        raise ValueError(f"damaged torrent file ({e.__class__.__name__})") from None
    raise ValueError("torrent file has no info dict")


def torrent_name(data: bytes) -> str:
    if len(data or b"") > MAX_TORRENT_BYTES:
        return ""
    try:
        meta, _ = _bdecode(data, 0)
        return (meta.get(b"info", {}).get(b"name", b"") or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def magnet_hash(uri: str) -> str:
    m = re.search(r"btih:([0-9a-fA-F]{40})", uri or "")
    return m.group(1).lower() if m else ""


# ── multipart/form-data (qBittorrent wants a file upload) ───────────────────

def encode_multipart(fields: dict, files: dict) -> tuple:
    """fields: {name: str}. files: {name: (filename, bytes)}. → (body, content_type)"""
    boundary = "----reelarr" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        out += f"{v}\r\n".encode()
    for k, (fname, data) in files.items():
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{k}"; '
                f'filename="{fname}"\r\n').encode()
        out += b"Content-Type: application/x-bittorrent\r\n\r\n"
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ── Base client ──────────────────────────────────────────────────────────────

class DownloadClient:
    key = ""                    # short id used in settings, e.g. "deluge"
    name = ""                   # display name, e.g. "Deluge"
    label_term = "label"        # what this client calls a label
    needs_username = True       # does the login take a username as well?
    default_port = 8080
    setup_hint = ""             # shown in the UI under the URL field

    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout: int = 20):
        self.base = (url or "").strip().rstrip("/")
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    # -- plumbing -----------------------------------------------------------
    def _http(self, path: str, data=None, headers=None, method=None):
        """Raw request against the client. Returns (status, body_bytes, headers)."""
        url = self.base + path if path.startswith("/") else path
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})
        except urllib.error.URLError as e:
            raise DownloadClientError(
                f"can't reach {self.name} at {self.base or '(no URL set)'} — {e.reason}"
            ) from None
        except Exception as e:
            raise DownloadClientError(f"{self.name}: {e}") from None

    def _json(self, path: str, payload=None, headers=None, method=None):
        body = json.dumps(payload).encode() if payload is not None else None
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        h.update(headers or {})
        status, raw, _ = self._http(path, body, h, method)
        if status >= 400:
            raise DownloadClientError(f"{self.name}: HTTP {status}")
        try:
            return json.loads(raw or b"{}")
        except Exception:
            raise DownloadClientError(f"{self.name}: unreadable response") from None

    def _form(self, path: str, fields: dict, headers=None):
        body = urllib.parse.urlencode(
            {k: v for k, v in fields.items() if v is not None}).encode()
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        h.update(headers or {})
        return self._http(path, body, h)

    # -- interface ----------------------------------------------------------
    def connect(self):
        """Log in / prove the client is reachable. Raise on failure."""
        raise NotImplementedError

    def labels(self) -> list:
        """Existing labels/categories."""
        raise NotImplementedError

    def add_torrent_file(self, path: Path, label: str = "",
                         download_dir: str = "", paused: bool = False) -> AddResult:
        raise NotImplementedError

    def add_magnet(self, uri: str, label: str = "",
                   download_dir: str = "", paused: bool = False) -> AddResult:
        raise NotImplementedError

    def version(self) -> str:
        return ""

    def get_torrents(self, label: str = "") -> list:
        """Every torrent the client holds, optionally only those carrying
        `label`. Returns TorrentInfo objects."""
        raise NotImplementedError

    @staticmethod
    def normalize_label(label: str) -> str:
        """Most clients take a label as typed. Ones that don't override this."""
        return (label or "").strip()

    # -- shared --------------------------------------------------------------
    def test(self) -> dict:
        """Used by the Articles page: prove we can reach it and label things."""
        self.connect()
        out = {"connected": True, "client": self.name,
               "label_term": self.label_term, "labels": [], "label_plugin": True}
        try:
            out["labels"] = self.labels()
        except DownloadClientError as e:
            out["label_plugin"] = False
            out["label_error"] = str(e)
        try:
            out["version"] = self.version()
        except Exception:
            pass
        return out

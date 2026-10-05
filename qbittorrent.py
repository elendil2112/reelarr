"""qBittorrent — via the Web API (v2).

Username + password against /api/v2/auth/login, then torrents are uploaded as
multipart form data. qBittorrent calls labels "categories"; a category can be
created on the fly, and setting one at add-time means no second call.
"""
import json
import urllib.parse
from pathlib import Path

from .base import (AddResult, DownloadClient, DownloadClientError, TorrentInfo, _int, _float,
                   encode_multipart, magnet_hash, torrent_infohash,
                   torrent_name)


class QBittorrentClient(DownloadClient):
    key = "qbittorrent"
    name = "qBittorrent"
    label_term = "category"
    needs_username = True
    default_port = 8080
    setup_hint = ("The qBittorrent Web UI address, e.g. http://qbittorrent:8080. "
                  "Uses the Web UI username and password.")

    def connect(self):
        if not self.base:
            raise DownloadClientError("no qBittorrent URL set")
        status, raw, _ = self._form("/api/v2/auth/login",
                                    {"username": self.username,
                                     "password": self.password},
                                    {"Referer": self.base})
        body = (raw or b"").decode("utf-8", "replace").strip()
        if status == 403:
            raise DownloadClientError(
                "qBittorrent refused the login — too many failed attempts, or "
                "this address is banned in its settings")
        if status >= 400 or body.lower() != "ok.":
            raise DownloadClientError(
                "qBittorrent rejected the username or password")
        return True

    def version(self) -> str:
        status, raw, _ = self._http("/api/v2/app/version")
        return (raw or b"").decode("utf-8", "replace").strip() if status < 400 else ""

    # -- categories ---------------------------------------------------------
    def labels(self) -> list:
        status, raw, _ = self._http("/api/v2/torrents/categories")
        if status >= 400:
            raise DownloadClientError(f"couldn't read categories (HTTP {status})")
        try:
            return list(json.loads(raw or b"{}").keys())
        except Exception:
            return []

    def _ensure_category(self, label: str) -> str:
        label = self.normalize_label(label)
        if not label:
            return ""
        if label not in self.labels():
            status, raw, _ = self._form("/api/v2/torrents/createCategory",
                                        {"category": label})
            if status >= 400:
                # Not fatal — adding with an unknown category still works in
                # recent builds; the torrent just lands uncategorised.
                raise DownloadClientError(
                    f"couldn't create the category {label!r} (HTTP {status})")
        return label

    # -- adding -------------------------------------------------------------
    def _add(self, fields: dict, files: dict) -> int:
        body, ctype = encode_multipart(fields, files)
        status, raw, _ = self._http("/api/v2/torrents/add", body,
                                    {"Content-Type": ctype, "Referer": self.base})
        if status >= 400:
            raise DownloadClientError(
                f"qBittorrent refused the torrent (HTTP {status})")
        text = (raw or b"").decode("utf-8", "replace").strip().lower()
        if text and text not in ("ok.", "ok"):
            raise DownloadClientError(f"qBittorrent said: {text[:120]}")
        return status

    def add_torrent_file(self, path, label="", download_dir="", paused=False):
        data = Path(path).read_bytes()
        cat = self._ensure_category(label) if label else ""
        known = set()
        try:
            known = {t.get("hash", "").lower() for t in self._torrents()}
        except DownloadClientError:
            pass
        ihash = ""
        try:
            ihash = torrent_infohash(data)
        except Exception:
            pass
        already = bool(ihash and ihash in known)
        self._add({"category": cat or None,
                   "savepath": download_dir or None,
                   "paused": "true" if paused else "false"},
                  {"torrents": (Path(path).name, data)})
        return AddResult(ihash, already, cat,
                         torrent_name(data) or Path(path).name)

    def add_magnet(self, uri, label="", download_dir="", paused=False):
        cat = self._ensure_category(label) if label else ""
        ihash = magnet_hash(uri)
        already = False
        try:
            already = bool(ihash and ihash in
                           {t.get("hash", "").lower() for t in self._torrents()})
        except DownloadClientError:
            pass
        self._form("/api/v2/torrents/add",
                   {"urls": uri, "category": cat or None,
                    "savepath": download_dir or None,
                    "paused": "true" if paused else "false"},
                   {"Referer": self.base})
        return AddResult(ihash, already, cat, uri[:60])

    def _torrents(self) -> list:
        status, raw, _ = self._http("/api/v2/torrents/info")
        if status >= 400:
            raise DownloadClientError(f"couldn't list torrents (HTTP {status})")
        try:
            return json.loads(raw or b"[]")
        except Exception:
            return []

    _DONE_STATES = {"uploading", "stalledup", "pauseduP".lower(), "pausedup",
                    "queuedup", "forcedup", "checkingup", "stoppedup"}

    @staticmethod
    def _status(state: str) -> str:
        s = state.lower()
        if s in ("downloading", "forceddl"):
            return "downloading"
        if s in ("uploading", "stalledup", "forcedup", "queuedup", "pausedup", "stoppedup"):
            return "seeding"
        if s == "stalleddl":
            return "stalled"
        if s == "queueddl":
            return "queued"
        if s in ("pauseddl", "stoppeddl"):
            return "paused"
        if s.startswith("checking") or s in ("moving", "allocating", "checkingresumedata"):
            return "checking"
        if s in ("metadl", "forcedmetadl"):
            return "metadata"
        if s in ("error", "missingfiles"):
            return "error"
        return s

    def get_torrents(self, label: str = "") -> list:
        cat = self.normalize_label(label)
        path = "/api/v2/torrents/info"
        if cat:
            path += "?category=" + urllib.parse.quote(cat)
        status, raw, _ = self._http(path)
        if status >= 400:
            raise DownloadClientError(f"couldn't list torrents (HTTP {status})")
        try:
            rows = json.loads(raw or b"[]")
        except Exception:
            return []
        out = []
        for t in rows:
            prog = float(t.get("progress") or 0)
            state = (t.get("state") or "").lower()
            pri = _int(t.get("priority"))
            eta = _int(t.get("eta"))
            out.append(TorrentInfo(
                id=(t.get("hash") or "").lower(), name=t.get("name", ""),
                save_path=t.get("save_path", ""),
                content_path=t.get("content_path", ""),
                progress=prog,
                finished=prog >= 0.999 or state in self._DONE_STATES,
                label=t.get("category", ""), state=state, status=self._status(state),
                queue=pri if pri and pri > 0 else None,      # 0/-1 = not in the queue
                size=_int(t.get("size")), done=_int(t.get("completed", t.get("downloaded"))),
                dl_speed=_int(t.get("dlspeed")), ul_speed=_int(t.get("upspeed")),
                eta=eta if eta is not None and 0 < eta < 8640000 else None,   # 8640000 = ∞
                seeds=_int(t.get("num_seeds")), peers=_int(t.get("num_leechs")),
                ratio=_float(t.get("ratio")), added=_float(t.get("added_on"))))
        return out

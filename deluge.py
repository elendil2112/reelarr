"""Deluge — via deluge-web's JSON-RPC endpoint (the API the browser UI uses).

Only needs the web URL and the web password; there's no username. Labelling
requires Deluge's Label plugin (Preferences → Plugins → Label).
"""
import base64
import json
import re
from pathlib import Path

from .base import (AddResult, DownloadClient, DownloadClientError, TorrentInfo, _int, _float,
                   magnet_hash, torrent_infohash, torrent_name)

_LABEL_OK = re.compile(r"^[a-z0-9_\-.]+$")


class DelugeClient(DownloadClient):
    key = "deluge"
    name = "Deluge"
    label_term = "label"
    needs_username = False
    default_port = 8112
    setup_hint = ("The Deluge web address you open in a browser, e.g. "
                  "http://deluge:8112. Use the WEB UI password. Labels need "
                  "the Label plugin switched on.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._id = 0
        self.endpoint = (self.base if self.base.endswith("/json")
                         else self.base + "/json")

    def _rpc(self, method: str, params=None):
        self._id += 1
        body = json.dumps({"method": method, "params": params or [],
                           "id": self._id}).encode()
        status, raw, _ = self._http(
            self.endpoint, body,
            {"Content-Type": "application/json", "Accept": "application/json"})
        if status >= 400:
            raise DownloadClientError(f"Deluge: HTTP {status} from {method}")
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            raise DownloadClientError(
                "Deluge: unreadable response — is that the web UI address?") from None
        err = payload.get("error")
        if err:
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise DownloadClientError(f"Deluge: {msg}")
        return payload.get("result")

    # -- session ------------------------------------------------------------
    def connect(self):
        if not self.base:
            raise DownloadClientError("no Deluge URL set")
        if not self._rpc("auth.login", [self.password]):
            raise DownloadClientError(
                "Deluge rejected the password (this is the web UI password, "
                "not the daemon one)")
        if not self._rpc("web.connected"):
            hosts = self._rpc("web.get_hosts") or []
            if not hosts:
                raise DownloadClientError(
                    "deluge-web isn't connected to a daemon and has no hosts set up")
            self._rpc("web.connect", [hosts[0][0]])
            if not self._rpc("web.connected"):
                raise DownloadClientError("deluge-web could not attach to its daemon")
        return True

    def version(self) -> str:
        try:
            return str(self._rpc("daemon.info") or "")
        except DownloadClientError:
            return ""

    # -- labels -------------------------------------------------------------
    @staticmethod
    def normalize_label(label: str) -> str:
        """Deluge only accepts lowercase a-z 0-9 _ - . — 'My Shows' → 'my-shows'."""
        s = (label or "").strip().lower()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9_\-.]", "", s)
        return s.strip("-.")

    def labels(self) -> list:
        try:
            return list(self._rpc("label.get_labels") or [])
        except DownloadClientError as e:
            raise DownloadClientError(
                "Deluge's Label plugin isn't enabled — switch it on under "
                f"Preferences → Plugins → Label ({e})") from None

    def _set_label(self, torrent_id: str, label: str) -> str:
        label = self.normalize_label(label)
        if not label or not torrent_id:
            return ""
        if not _LABEL_OK.match(label):
            raise DownloadClientError(f"{label!r} isn't a label Deluge will accept")
        if label not in [l.lower() for l in self.labels()]:
            try:
                self._rpc("label.add", [label])
            except DownloadClientError:
                pass          # already there, or created concurrently
        self._rpc("label.set_torrent", [torrent_id, label])
        return label

    # -- adding -------------------------------------------------------------
    def add_torrent_file(self, path, label="", download_dir="", paused=False):
        data = Path(path).read_bytes()
        options = {"add_paused": bool(paused)}
        if download_dir:
            options["download_location"] = download_dir
        already, tid = False, None
        try:
            tid = self._rpc("core.add_torrent_file",
                            [Path(path).name, base64.b64encode(data).decode(),
                             options])
        except DownloadClientError as e:
            if "already" not in str(e).lower():
                raise
            already = True
        if not tid:
            # Deluge answers null when it already has the torrent — work the id
            # out ourselves so the label still goes on.
            already = True
            tid = torrent_infohash(data)
        return AddResult(tid, already, self._set_label(tid, label),
                         torrent_name(data) or Path(path).name)

    def add_magnet(self, uri, label="", download_dir="", paused=False):
        options = {"add_paused": bool(paused)}
        if download_dir:
            options["download_location"] = download_dir
        already, tid = False, ""
        try:
            tid = self._rpc("core.add_torrent_magnet", [uri, options]) or ""
        except DownloadClientError as e:
            if "already" not in str(e).lower():
                raise
            already = True
        if not tid:
            already = True
            tid = magnet_hash(uri)
        return AddResult(tid, already, self._set_label(tid, label), uri[:60])

    # -- listing ------------------------------------------------------------
    _FIELDS = ["name", "progress", "save_path", "state", "label", "is_finished",
               "total_remaining", "queue", "total_wanted", "total_done",
               "download_payload_rate", "upload_payload_rate", "eta", "num_seeds",
               "num_peers", "ratio", "time_added"]
    _STATUS = {"downloading": "downloading", "seeding": "seeding", "paused": "paused",
               "checking": "checking", "queued": "queued", "error": "error",
               "allocating": "checking", "moving": "checking"}

    def get_torrents(self, label: str = "") -> list:
        want = self.normalize_label(label)
        try:
            raw = self._rpc("core.get_torrents_status", [{}, self._FIELDS]) or {}
        except DownloadClientError:
            # older daemons don't accept an empty filter dict
            raw = self._rpc("core.get_torrents_status", [{"state": "Seeding"},
                                                         self._FIELDS]) or {}
        out = []
        for tid, t in raw.items():
            lbl = (t.get("label") or "").strip()
            if want and lbl.lower() != want.lower():
                continue
            prog = float(t.get("progress") or 0) / 100.0
            finished = bool(t.get("is_finished")) or prog >= 0.999
            state = t.get("state", "")
            status = self._STATUS.get(state.lower(), state.lower())
            if status == "paused" and finished:
                status = "seeding"
            rate = _int(t.get("download_payload_rate"))
            if status == "downloading" and not rate and _int(t.get("num_seeds")) == 0:
                status = "stalled"
            q = _int(t.get("queue"))
            eta = _int(t.get("eta"))
            out.append(TorrentInfo(
                id=tid, name=t.get("name", ""), save_path=t.get("save_path", ""),
                progress=prog, finished=finished, label=lbl, state=state, status=status,
                queue=q + 1 if q is not None and q >= 0 else None,   # Deluge counts from 0
                size=_int(t.get("total_wanted")), done=_int(t.get("total_done")),
                dl_speed=rate, ul_speed=_int(t.get("upload_payload_rate")),
                eta=eta if eta and eta > 0 else None,
                seeds=_int(t.get("num_seeds")), peers=_int(t.get("num_peers")),
                ratio=_float(t.get("ratio")), added=_float(t.get("time_added"))))
        return out

"""Transmission — via its RPC endpoint (/transmission/rpc).

Transmission answers the first request with 409 and a session id header, which
you then echo on every call; this handles that automatically. Optional HTTP
basic auth. Labels are native from Transmission 3.0 onwards.
"""
import base64
import json
from pathlib import Path

from .base import (AddResult, DownloadClient, DownloadClientError, TorrentInfo, _int, _float,
                   magnet_hash, torrent_infohash, torrent_name)

_SESSION_HEADER = "X-Transmission-Session-Id"


class TransmissionClient(DownloadClient):
    key = "transmission"
    name = "Transmission"
    label_term = "label"
    needs_username = True
    default_port = 9091
    setup_hint = ("The Transmission web address, e.g. http://transmission:9091. "
                  "Username and password only if you've turned authentication "
                  "on. Labels need Transmission 3.0 or newer.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._session = ""
        self._tag = 0
        self.endpoint = (self.base if self.base.endswith("/rpc")
                         else self.base + "/transmission/rpc")

    def _auth_header(self) -> dict:
        if not self.username and not self.password:
            return {}
        raw = f"{self.username}:{self.password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _rpc(self, method: str, arguments=None, _retry=True):
        self._tag += 1
        body = json.dumps({"method": method, "arguments": arguments or {},
                           "tag": self._tag}).encode()
        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_header())
        if self._session:
            headers[_SESSION_HEADER] = self._session
        status, raw, resp_headers = self._http(self.endpoint, body, headers)

        if status == 409 and _retry:
            # Transmission handing us the session id — take it and repeat.
            self._session = (resp_headers.get(_SESSION_HEADER)
                             or resp_headers.get(_SESSION_HEADER.lower()) or "")
            if not self._session:
                raise DownloadClientError(
                    "Transmission asked for a session id but didn't send one")
            return self._rpc(method, arguments, _retry=False)
        if status == 401:
            raise DownloadClientError(
                "Transmission rejected the username or password")
        if status >= 400:
            raise DownloadClientError(f"Transmission: HTTP {status}")
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            raise DownloadClientError(
                "Transmission: unreadable response — is that the right address?"
            ) from None
        if payload.get("result") != "success":
            raise DownloadClientError(f"Transmission: {payload.get('result')}")
        return payload.get("arguments", {})

    # -- session ------------------------------------------------------------
    def connect(self):
        if not self.base:
            raise DownloadClientError("no Transmission URL set")
        self._rpc("session-get")
        return True

    def version(self) -> str:
        try:
            return str(self._rpc("session-get").get("version", ""))
        except DownloadClientError:
            return ""

    # -- labels -------------------------------------------------------------
    def labels(self) -> list:
        """Transmission has no label registry — labels exist only on torrents,
        so gather the ones currently in use."""
        args = self._rpc("torrent-get", {"fields": ["labels"]})
        seen = []
        for t in args.get("torrents", []):
            for l in t.get("labels") or []:
                if l not in seen:
                    seen.append(l)
        return seen

    # -- adding -------------------------------------------------------------
    def _add(self, arguments: dict, label: str, fallback_hash: str, name: str):
        label = self.normalize_label(label)
        if label:
            arguments["labels"] = [label]
        try:
            args = self._rpc("torrent-add", arguments)
        except DownloadClientError as e:
            # Older builds reject an unknown "labels" key — retry without it,
            # then set the label separately.
            if label and "labels" in str(e).lower():
                arguments.pop("labels", None)
                args = self._rpc("torrent-add", arguments)
            else:
                raise
        info = args.get("torrent-added") or args.get("torrent-duplicate") or {}
        already = "torrent-duplicate" in args
        tid = info.get("hashString") or fallback_hash
        applied = label
        if label and info.get("id") is not None:
            try:
                self._rpc("torrent-set", {"ids": [info["id"]], "labels": [label]})
            except DownloadClientError:
                applied = ""      # pre-3.0 Transmission: no label support
        return AddResult(tid, already, applied, info.get("name") or name)

    def add_torrent_file(self, path, label="", download_dir="", paused=False):
        data = Path(path).read_bytes()
        args = {"metainfo": base64.b64encode(data).decode(), "paused": bool(paused)}
        if download_dir:
            args["download-dir"] = download_dir
        ihash = ""
        try:
            ihash = torrent_infohash(data)
        except Exception:
            pass
        return self._add(args, label, ihash,
                         torrent_name(data) or Path(path).name)

    def add_magnet(self, uri, label="", download_dir="", paused=False):
        args = {"filename": uri, "paused": bool(paused)}
        if download_dir:
            args["download-dir"] = download_dir
        return self._add(args, label, magnet_hash(uri), uri[:60])

    _FIELDS = ["id", "hashString", "name", "percentDone", "downloadDir",
               "status", "labels", "isFinished", "queuePosition", "sizeWhenDone",
               "leftUntilDone", "rateDownload", "rateUpload", "eta", "peersSendingToUs",
               "peersGettingFromUs", "uploadRatio", "addedDate", "error"]
    # 0 stopped · 1 check wait · 2 checking · 3 download wait · 4 downloading · 5 seed wait · 6 seeding
    _STATUS = {0: "paused", 1: "checking", 2: "checking", 3: "queued", 4: "downloading",
               5: "seeding", 6: "seeding"}

    def get_torrents(self, label: str = "") -> list:
        want = self.normalize_label(label)
        args = self._rpc("torrent-get", {"fields": self._FIELDS})
        out = []
        for t in args.get("torrents", []):
            labels = [str(l) for l in (t.get("labels") or [])]
            if want and want.lower() not in [l.lower() for l in labels]:
                continue
            prog = float(t.get("percentDone") or 0)
            # status 5/6 = seeding, 0 = stopped
            finished = prog >= 0.999 or bool(t.get("isFinished"))
            code = _int(t.get("status"))
            status = self._STATUS.get(code, "")
            if t.get("error"):
                status = "error"
            elif status == "paused" and finished:
                status = "seeding"
            elif status == "downloading" and not _int(t.get("rateDownload")) \
                    and not _int(t.get("peersSendingToUs")):
                status = "stalled"
            size, left = _int(t.get("sizeWhenDone")), _int(t.get("leftUntilDone"))
            q, eta = _int(t.get("queuePosition")), _int(t.get("eta"))
            out.append(TorrentInfo(
                id=(t.get("hashString") or "").lower(), name=t.get("name", ""),
                save_path=t.get("downloadDir", ""),
                progress=prog, finished=finished,
                label=labels[0] if labels else "",
                state=str(t.get("status", "")), status=status,
                queue=q + 1 if q is not None and q >= 0 and not finished else None,
                size=size, done=size - left if size is not None and left is not None else None,
                dl_speed=_int(t.get("rateDownload")), ul_speed=_int(t.get("rateUpload")),
                eta=eta if eta and eta > 0 else None,         # -1 n/a, -2 unknown
                seeds=_int(t.get("peersSendingToUs")), peers=_int(t.get("peersGettingFromUs")),
                ratio=_float(t.get("uploadRatio")), added=_float(t.get("addedDate"))))
        return out

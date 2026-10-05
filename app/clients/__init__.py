"""Download clients Reelarr can hand torrents to.

Registering another one is a two-line change: write the subclass in its own
file, import it here, add it to CLIENTS. Everything else — settings, the
Articles page, the torrent watcher — picks it up automatically.
"""
from .base import (AddResult, DownloadClient, DownloadClientError,  # noqa: F401
                   TorrentInfo,
                   magnet_hash, torrent_infohash, torrent_name)
from .deluge import DelugeClient
from .qbittorrent import QBittorrentClient
from .transmission import TransmissionClient

CLIENTS = {c.key: c for c in (DelugeClient, QBittorrentClient, TransmissionClient)}

DEFAULT_CLIENT = DelugeClient.key


def get_class(key: str):
    cls = CLIENTS.get((key or "").strip().lower())
    if not cls:
        known = ", ".join(sorted(CLIENTS))
        raise DownloadClientError(
            f"unknown download client {key!r} — Reelarr knows: {known}")
    return cls


def build(cfg: dict) -> DownloadClient:
    """Make a client from the torrents settings block."""
    cls = get_class(cfg.get("client") or DEFAULT_CLIENT)
    return cls(cfg.get("url", ""), cfg.get("username", ""),
               cfg.get("password", ""))


def describe() -> list:
    """What the GUI needs to render the client picker."""
    return [{"key": c.key, "name": c.name, "label_term": c.label_term,
             "needs_username": c.needs_username, "default_port": c.default_port,
             "hint": c.setup_hint}
            for c in CLIENTS.values()]

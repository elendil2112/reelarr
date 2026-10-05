"""Indexers: places Reelarr can search for shows and grab them from.

Every indexer implements the small interface in base.py and returns
Release objects, so Discover, the Artists monitor and the Queue never care
which site a show came from. The Live Music Archive is built in; others
(another archive, a private tracker) can be added as a module here or by a
provider add-on calling register().
"""
from .base import Indexer, Release, IndexerError
from .lma import LiveMusicArchive

__all__ = ["Indexer", "Release", "IndexerError", "LiveMusicArchive",
           "register", "get", "all"]

_registry = {}


def register(indexer: Indexer):
    _registry[indexer.name] = indexer


def get(name: str) -> Indexer:
    if name not in _registry:
        raise IndexerError(f"no indexer called {name!r}")
    return _registry[name]


def all() -> list:
    return list(_registry.values())


register(LiveMusicArchive())

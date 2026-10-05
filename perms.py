"""Ownership & mode fixing so nothing Reelarr creates or moves ever has
permission problems on the unRAID host (default nobody:users, 0777/0666)."""
import os
from pathlib import Path

from . import config


def _modes():
    s = config.load()["permissions"]
    return (int(s["puid"]), int(s["pgid"]),
            int(str(s["dir_mode"]), 8), int(str(s["file_mode"]), 8))


def fix_path(path: Path):
    """Fix one file or directory.

    On unRAID (running as root) this sets nobody:users and 0777/0666 so the
    host never fights Reelarr. Off Linux — a native Windows or macOS run —
    ownership isn't ours to set, so this quietly does nothing there.
    """
    if os.name == "nt" or not hasattr(os, "chown"):
        return
    puid, pgid, dmode, fmode = _modes()
    try:
        os.chown(path, puid, pgid)
        os.chmod(path, dmode if path.is_dir() else fmode)
    except (PermissionError, OSError):
        pass  # not running as root — modes come from umask instead


def fix_tree(path: Path):
    """Recursively fix a directory tree (or single file)."""
    path = Path(path)
    if not path.exists():
        return
    fix_path(path)
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            for d in dirs:
                fix_path(Path(root) / d)
            for f in files:
                fix_path(Path(root) / f)


def make_dir(path: Path):
    """mkdir -p with correct ownership on every level created."""
    path = Path(path)
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for m in reversed(missing):
        fix_path(m)

"""Where Reelarr keeps its files.

In the Docker container these are the mounted volumes (/config, /watch,
/music). Off Docker — bare metal on Linux, Windows or macOS — they fall back
to normal per-user folders, so the app works with no setup.

Override any of them with environment variables:
    REELARR_CONFIG, REELARR_WATCH, REELARR_LIBRARY
The old BARBOSA_* names are still honoured so an existing install keeps
working after the switch.
"""
import os
import sys
from pathlib import Path

APP_NAME = "Reelarr"
LEGACY_APP_NAME = "Barbosa"


def _env(name: str):
    """REELARR_<name>, falling back to the legacy BARBOSA_<name>."""
    return os.environ.get(f"REELARR_{name}") or os.environ.get(f"BARBOSA_{name}")


def in_container() -> bool:
    """True when running in the Docker image, where /config is a mount."""
    return Path("/config").is_dir() and Path("/.dockerenv").exists()


def _user_data_dir(app_name: str = APP_NAME) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    return Path(os.environ.get("XDG_CONFIG_HOME",
                               str(Path.home() / ".config"))) / app_name.lower()


def legacy_user_data_dir() -> Path:
    """Where a bare-metal Barbosa kept its data — read once, at migration."""
    return _user_data_dir(LEGACY_APP_NAME)


def config_dir() -> Path:
    env = _env("CONFIG")
    if env:
        p = Path(env)
    elif Path("/config").is_dir():
        p = Path("/config")
    else:
        p = _user_data_dir()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def backups_dir() -> Path:
    p = config_dir() / "backups"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _media_root() -> str:
    """Inside the image: the folder the compose file mounts for music and
    downloads (REELARR_MEDIA_PATH, default /media)."""
    return os.environ.get("REELARR_MEDIA_PATH") or "/media"


def default_watch_dir() -> str:
    """Only a suggestion: the setup wizard asks before anything is watched."""
    env = _env("WATCH")
    if env:
        return env
    if Path("/watch").is_dir():          # an older compose file's mount
        return "/watch"
    if in_container():
        return str(Path(_media_root()) / "intake")
    return str(Path.home() / "Music" / "Reelarr Intake")


def default_library_dir() -> str:
    env = _env("LIBRARY")
    if env:
        return env
    if Path("/music").is_dir():
        return "/music"
    if in_container():
        return str(Path(_media_root()) / "library")
    return str(Path.home() / "Music" / "Reelarr Library")

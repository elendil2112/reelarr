"""First-run setup: what the wizard needs from the server.

  * which folders Reelarr can actually reach (mounts in Docker, the disk on
    bare metal), and a browser over them
  * validation of a watch/library pair before it's saved
  * the download-client path check: ask the client where a finished torrent
    lives, see whether Reelarr can open it, and if not, work out the mapping
  * the "is setup finished?" flag that keeps the watcher, torrent import and
    library scan idle until a person has confirmed the folders

Nothing here moves or deletes anything. The only write is a probe file used
to prove a folder is writable, removed straight away.
"""
import os
import sys
import tempfile
from pathlib import Path

from . import config, database as db, paths

DONE_KEY = "setup_wizard_done"

# Mount points that are Docker plumbing, not the user's folders
_SYSTEM_PREFIXES = ("/proc", "/sys", "/dev", "/etc", "/run", "/usr", "/var/lib",
                    "/app", "/bin", "/sbin", "/lib")
_PSEUDO_FS = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2",
              "overlay", "securityfs", "debugfs", "pstore", "bpf", "tracefs",
              "hugetlbfs", "configfs", "fusectl", "autofs", "nsfs", "devtmpfs"}


def done() -> bool:
    return db.kv_get(DONE_KEY) == "1"


def mark_done():
    db.kv_set(DONE_KEY, "1")
    db.log("info", "setup", "setup wizard finished — watching starts now")


# ── What can Reelarr see? ────────────────────────────────────────────────────

def _decode(s: str) -> str:
    # mountinfo escapes spaces etc. as \040 octal
    return s.encode().decode("unicode_escape") if "\\" in s else s


def container_mounts(mountinfo: str = None) -> list:
    """User folders mounted into this container (bind mounts / volumes)."""
    out = []
    try:
        lines = (mountinfo if mountinfo is not None
                 else Path("/proc/self/mountinfo").read_text()).splitlines()
    except OSError:
        return out
    for line in lines:
        left, _, right = line.partition(" - ")
        f = left.split()
        if len(f) < 5:
            continue
        point = _decode(f[4])
        fstype = right.split()[0] if right else ""
        if point == "/" or fstype in _PSEUDO_FS:
            continue
        if any(point == p or point.startswith(p + "/") for p in _SYSTEM_PREFIXES):
            continue
        if not Path(point).is_dir():
            continue           # /etc/hosts and friends are files
        out.append(point)
    return sorted(set(out))


def roots() -> list:
    """Starting points for the folder browser."""
    if paths.in_container():
        r = [m for m in container_mounts() if m != "/config"]
        media = os.environ.get("REELARR_MEDIA_PATH") or "/media"
        if Path(media).is_dir() and media not in r and on_mount(media):
            r.insert(0, media)
        return r
    if os.name == "nt":
        import string
        return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    r = [str(Path.home())]
    for extra in ("/mnt", "/media", "/Volumes", "/srv", "/data"):
        if Path(extra).is_dir():
            r.append(extra)
    return r


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def on_mount(path: str) -> bool:
    """In Docker: is this path inside something the host mounted? Anything
    else lives in the container's own layer and vanishes on recreate."""
    if not paths.in_container():
        return True
    p = Path(path).resolve()
    return any(_under(p, Path(m)) for m in container_mounts())


def inside_config(p: Path) -> bool:
    """Is p the config folder, or inside it — by identity, not by name? On
    unRAID the config folder is often ALSO visible through the media mount
    (/mnt/user/appdata/reelarr), so comparing paths isn't enough."""
    cfg = paths.config_dir()
    try:
        cst = cfg.stat()
    except OSError:
        return False
    try:
        cur = p.resolve()
    except OSError:
        return False
    for q in (cur, *cur.parents):
        try:
            st = q.stat()
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (cst.st_dev, cst.st_ino):
            return True
    return False


def _contains_config(p: Path) -> bool:
    """Would this folder hold Reelarr's config folder inside it?"""
    try:
        cfg = paths.config_dir().resolve()
        return p.exists() and _under(cfg, p.resolve()) and not inside_config(p)
    except OSError:
        return False


def _browsable(p: Path) -> bool:
    """The folder browser shows your media, never the app's own config
    (settings, databases, backups) — and in Docker, only what you mounted."""
    try:
        rp = p.resolve()
    except OSError:
        return False
    if inside_config(rp):
        return False
    return on_mount(str(rp))


def list_dir(path: str = "") -> dict:
    """One level of the folder browser."""
    if not path:
        return {"path": "", "parent": None,
                "dirs": [{"name": r, "path": r} for r in roots()]}
    p = Path(path).expanduser()
    if not _browsable(p):
        return {"error": f"{path} isn't one of the folders Reelarr may browse"}
    if not p.is_dir():
        return {"error": f"{path} isn't a folder Reelarr can see"}
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith(".") or not child.is_dir() or not _browsable(child):
                continue
            dirs.append({"name": child.name, "path": str(child)})
            if len(dirs) >= 500:
                break
    except PermissionError:
        return {"error": f"Reelarr isn't allowed to read {path} — check PUID/PGID"}
    parent = str(p.parent) if p.parent != p else None
    if paths.in_container() and parent and not on_mount(parent):
        parent = ""                       # back to the list of mounts
    return {"path": str(p), "parent": parent, "dirs": dirs,
            "writable": os.access(p, os.W_OK)}


def make_dir(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts:
        return {"error": "give a full path"}
    if not on_mount(str(p.parent if not p.exists() else p)) or not _browsable(p.parent):
        return {"error": "that isn't inside a mounted folder"}
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"couldn't create {p}: {e.strerror or e}"}
    return {"ok": True, "path": str(p)}


def media_problem() -> str:
    """In Docker: '' if Reelarr can see real folders, else why not.
    The usual first-run snag: no .env, so the compose file's default
    ./media folder (empty) is all the container has."""
    if not paths.in_container():
        return ""
    r = roots()
    if not r:
        return "no_mount"
    for root in r:
        try:
            if any(not c.name.startswith(".") for c in Path(root).iterdir()):
                return ""
        except OSError:
            continue
    return "empty"


MOUNT_HELP = (
    "Reelarr can only see folders Docker has given it, and right now that's "
    "an empty folder. Give it the folder that holds your music and downloads, "
    "then recreate the container. On unRAID, put these lines in a file called "
    ".env next to docker-compose.yml:\n"
    "  REELARR_MEDIA_DIR=/mnt/user\n"
    "  REELARR_MEDIA_PATH=/mnt/user\n"
    "  PUID=99\n  PGID=100\n  UMASK=000\n"
    "then run:  docker compose up -d")


# ── Folder validation ────────────────────────────────────────────────────────

def _writable(p: Path) -> str:
    try:
        fd, name = tempfile.mkstemp(prefix=".reelarr-write-test-", dir=p)
        os.close(fd)
        os.unlink(name)
        return ""
    except OSError as e:
        return e.strerror or str(e)


def check_folders(watch: str, library: str, torrent_dir: str = "") -> dict:
    """Problems block saving; notes are things worth knowing."""
    problems, notes = [], []
    named = {"watch folder": watch, "library folder": library}
    if torrent_dir:
        named[".torrent folder"] = torrent_dir
    resolved = {}
    for label, raw in named.items():
        if not raw or not str(raw).strip():
            problems.append(f"Choose a {label}.")
            continue
        p = Path(raw).expanduser()
        if not p.is_absolute():
            problems.append(f"The {label} must be a full path, not {raw!r}.")
            continue
        if not p.exists():
            problems.append(f"The {label} {p} doesn't exist yet — create it first.")
            continue
        if not p.is_dir():
            problems.append(f"The {label} {p} is a file, not a folder.")
            continue
        if paths.in_container() and not on_mount(str(p)):
            problems.append(
                f"The {label} {p} isn't inside a folder Docker mounted, so anything "
                f"written there disappears when the container is recreated. Pick a "
                f"folder under {', '.join(roots()) or 'a mounted path'}.")
            continue
        err = _writable(p)
        if err:
            problems.append(f"Reelarr can't write to the {label} {p} ({err}). "
                            f"Check that PUID/PGID own it.")
            continue
        if inside_config(p) or _contains_config(p):
            problems.append(f"The {label} can't be inside Reelarr's config folder, or contain it.")
            continue
        resolved[label] = p.resolve()

    w, l = resolved.get("watch folder"), resolved.get("library folder")
    if w and l:
        if w == l:
            problems.append("The watch folder and the library must be different folders.")
        elif _under(w, l):
            problems.append("The watch folder can't be inside the library — "
                            "half-processed shows would appear in your library.")
        elif _under(l, w):
            problems.append("The library can't be inside the watch folder — "
                            "Reelarr would try to re-import your whole library.")
        else:
            try:
                if w.stat().st_dev != l.stat().st_dev:
                    notes.append("The watch folder and library are on different drives, "
                                 "so filing a show copies it across rather than "
                                 "moving it instantly. That works, just slower.")
            except OSError:
                pass
            try:
                n = sum(1 for c in l.iterdir() if c.is_dir() and not c.name.startswith((".", "_")))
                if n:
                    notes.append(f"The library already has {n} artist folder(s) — "
                                 f"Reelarr will learn from them and file alongside them.")
            except OSError:
                pass
    return {"ok": not problems, "problems": problems, "notes": notes}


# ── Download client: where does it put things, and can we see them? ──────────

def _suggest(client_path: str, search_roots: list):
    """Find where a client's path shows up on our side by matching its
    trailing folders under our roots (up to two extra levels down).
    Returns (client_prefix, local_prefix) or None."""
    parts = [x for x in Path(client_path).parts if x not in ("/", "\\")]
    for k in range(len(parts), 0, -1):            # longest tail first
        tail = Path(*parts[-k:])
        for root in search_roots:
            root = Path(root)
            candidates = [root / tail]
            try:
                candidates += [c / tail for c in root.iterdir() if c.is_dir()]
                candidates += [g / tail for c in root.iterdir() if c.is_dir()
                               for g in c.iterdir() if g.is_dir()]
            except OSError:
                pass
            for cand in candidates:
                if cand.exists():
                    # the client's prefix is what's left after the matched tail;
                    # keep it at least one folder deep, so the rule is
                    # "/downloads → …" rather than a catch-all "/ → …"
                    keep = k if k < len(parts) else k - 1
                    if keep < 1:
                        continue
                    client_prefix = str(Path(client_path).parents[keep - 1])
                    local_prefix = str(cand.parents[keep - 1])
                    return client_prefix, local_prefix
    return None


def probe_client(cfg_override: dict = None, limit: int = 12) -> dict:
    """Ask the download client for a few torrents and check we can open them."""
    from . import clients, torrents
    cfg = dict(config.load()["torrents"])
    for k, v in (cfg_override or {}).items():
        if v not in (None, "", config.MASK):
            cfg[k] = v
    try:
        client = clients.build(cfg)
        client.connect()
        items = client.get_torrents("")
    except Exception as e:
        return {"error": str(e)}
    mapping = cfg.get("path_map") or {}
    samples = []
    for t in items:
        src = t.source()
        if not src:
            continue
        local = torrents.map_path(src, mapping)
        samples.append({"name": t.name, "client_path": src, "local_path": local,
                        "visible": Path(local).exists()})
        if len(samples) >= limit:
            break
    if not samples:
        return {"samples": [], "visible": 0,
                "message": "The client has no torrents yet, so there's nothing to check. "
                           "Finish setup and come back to Settings → Torrents once one "
                           "has downloaded."}
    visible = sum(1 for s in samples if s["visible"])
    out = {"samples": samples, "visible": visible, "total": len(samples),
           "mapping": mapping}
    if visible == len(samples):
        out["message"] = "Reelarr can see every download the client reported."
        return out
    # propose a mapping from the first one we can't see
    search = roots() if paths.in_container() else ["/"] + roots()
    for s in samples:
        if s["visible"]:
            continue
        hit = _suggest(s["client_path"], search)
        if not hit:
            continue
        client_prefix, local_prefix = hit
        proposed = dict(mapping)
        proposed[client_prefix] = local_prefix
        fixed = sum(1 for x in samples
                    if Path(torrents.map_path(x["client_path"], proposed)).exists())
        out["suggestion"] = {"client": client_prefix, "local": local_prefix,
                             "fixes": fixed, "of": len(samples)}
        break
    out["message"] = (f"Reelarr can see {visible} of {len(samples)} downloads. "
                      + ("A path mapping should fix the rest." if out.get("suggestion")
                         else "Make sure the folder your client downloads into is "
                              "mounted into Reelarr too, then check again."))
    return out


def verify_mapping(client_prefix: str, local_prefix: str) -> dict:
    lp = Path(local_prefix)
    if not lp.is_dir():
        return {"ok": False, "error": f"{local_prefix} isn't a folder Reelarr can see"}
    try:
        n = sum(1 for _ in lp.iterdir())
    except OSError as e:
        return {"ok": False, "error": f"can't read {local_prefix}: {e.strerror or e}"}
    return {"ok": True, "entries": n}


# ── Wizard state (prefill) ───────────────────────────────────────────────────

def state() -> dict:
    cfg = config.masked(config.load())
    return {
        "done": done(),
        "in_container": paths.in_container(),
        "roots": roots(),
        "mounts": container_mounts() if paths.in_container() else [],
        "media_problem": media_problem(),
        "puid": os.getuid() if hasattr(os, "getuid") else None,
        "pgid": os.getgid() if hasattr(os, "getgid") else None,
        "platform": sys.platform,
        "paths": cfg["paths"],
        "torrents": {k: cfg["torrents"].get(k) for k in
                     ("enabled", "client", "url", "username", "password", "label",
                      "watch_dir", "import_completed", "import_mode", "path_map")},
        "sources": {k: cfg["sources"].get(k) for k in
                    ("setlistfm_api_key", "use_setlistfm", "use_internet_archive")},
        "watcher": {k: cfg["watcher"].get(k) for k in
                    ("convert_target", "dedupe_formats", "keep_wav_originals",
                     "keep_shn_originals")},
        "safety": cfg.get("safety", {}),
        "timezone": cfg.get("timezone"),
    }

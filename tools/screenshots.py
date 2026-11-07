"""Rebuild the README screenshots from a throwaway demo install.

    python tools/screenshots.py            # writes docs/screenshots/*.png

Everything runs offline in a temporary folder: real FLAC files made with
ffmpeg are filed by the real pipeline, a stand-in Live Music Archive and a
stand-in qBittorrent supply the Artists and Queue pages, and Playwright's
Chromium takes the pictures. Needs ffmpeg and `pip install playwright`.
Nothing here touches your own config or library.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
PORT = 8291
USER, PASSWORD = "demo", "demo-password-123"
SIZE = {"width": 1440, "height": 860}


# ── a demo install ───────────────────────────────────────────────────────────

def _env(tmp: Path):
    for d in ("config", "watch", "music", "torrents", "dl"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    os.environ.update(REELARR_CONFIG=str(tmp / "config"), REELARR_WATCH=str(tmp / "watch"),
                      REELARR_LIBRARY=str(tmp / "music"), TZ="America/New_York")
    settings = {
        "safety": {"dry_run": False},
        "paths": {"watch_dir": str(tmp / "watch"), "library_dir": str(tmp / "music")},
        "sources": {"use_setlistfm": False, "use_internet_archive": False,
                    "use_musicbrainz_genre": False},
        "watcher": {"audio_source_analysis": False},
        "filing": {"auto_file_threshold": 70, "new_artist_policy": "auto"},
        "torrents": {"enabled": True, "client": "qbittorrent", "url": "http://qbittorrent:8080",
                     "watch_dir": str(tmp / "torrents"), "label": "reelarr"},
        "monitor": {"enabled": True, "interval_hours": 12},
    }
    (tmp / "config" / "settings.json").write_text(json.dumps(settings))
    sys.path.insert(0, str(ROOT))


def _flac(path: Path, seconds: int, tags: dict, freq: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-c:a", "flac", str(path)],
                   check=True)
    from mutagen.flac import FLAC
    a = FLAC(path)
    for k, v in tags.items():
        a[k] = v
    a.save()


def _show(watch: Path, folder: str, artist: str, songs: list, info: str = "",
          tag_titles: bool = True, seconds=None):
    d = watch / folder
    seconds = seconds or [4] * len(songs)
    for i, (song, sec) in enumerate(zip(songs, seconds), 1):
        tags = {"ARTIST": artist, "TRACKNUMBER": str(i)}
        if tag_titles:
            tags["TITLE"] = song
        _flac(d / f"t{i:02d}.flac", sec, tags, 220 + 30 * i)
    if info:
        (d / f"{folder}.txt").write_text(info)
    return d


def _info(artist, date, venue, city, source, songs, lineage="", taper=""):
    lines = [artist, date, venue, city, ""]
    if source:
        lines += [f"Source: {source}" + (f" > {lineage}" if lineage else "")]
    if taper:
        lines += [f"Recorded by {taper}"]
    lines += ["", *[f"{i:02d}. {s}" for i, s in enumerate(songs, 1)]]
    return "\n".join(lines) + "\n"


def seed(tmp: Path):
    from app import auth, database as db, pipeline, setup
    db.init()
    setup.mark_done()
    auth.create_user(USER, PASSWORD)
    watch = tmp / "watch"

    filed = [
        ("gd1977-05-08.sbd.hicks.4982.sbeok.shnf", "Grateful Dead",
         ["New Minglewood Blues", "Loser", "El Paso", "They Love Each Other", "Jack Straw"],
         _info("Grateful Dead", "1977-05-08", "Barton Hall, Cornell University", "Ithaca, NY",
               "SBD", ["New Minglewood Blues", "Loser", "El Paso", "They Love Each Other", "Jack Straw"],
               "Reel > DAT > CD", "Betty Cantor-Jackson")),
        ("bstrings2023-04-21.aud.flac24", "Billy Strings",
         ["Away From the Mire", "Dust in a Baggie", "Meet Me at the Creek", "Red Daisy"],
         _info("Billy Strings", "2023-04-21", "Red Rocks Amphitheatre", "Morrison, CO", "AUD",
               ["Away From the Mire", "Dust in a Baggie", "Meet Me at the Creek", "Red Daisy"],
               "Schoeps MK4 > Sound Devices MixPre-6")),
        ("goose2026-09-28.mk4.aud.flac24", "Goose",
         ["Arcadia", "Hot Tea", "Madalena", "Tumble"],
         _info("Goose", "2026-09-28", "Paradiso", "Amsterdam, Netherlands", "AUD",
               ["Arcadia", "Hot Tea", "Madalena", "Tumble"])),
        ("phish1997-11-22.mtx.flac", "Phish",
         ["Tweezer", "Reba", "Black-Eyed Katy", "Piper"],
         _info("Phish", "1997-11-22", "Hampton Coliseum", "Hampton, VA", "Matrix",
               ["Tweezer", "Reba", "Black-Eyed Katy", "Piper"])),
        ("wsp2024-12-31.sbd", "Widespread Panic",
         ["Chilly Water", "Ain't Life Grand", "Porch Song"],
         _info("Widespread Panic", "2024-12-31", "Fox Theatre", "Atlanta, GA", "SBD",
               ["Chilly Water", "Ain't Life Grand", "Porch Song"])),
    ]
    for folder, artist, songs, info in filed:
        pipeline.process_show(_show(watch, folder, artist, songs, info))

    # waits in Review: nothing says where it was
    pipeline.process_show(_show(
        watch, "jgb1990-11-17 show", "Jerry Garcia Band",
        ["How Sweet It Is", "Stop That Train", "Tangled Up in Blue"]))
    # waits in Review: no venue, and a tuning track the setlist doesn't list
    pipeline.process_show(_show(
        watch, "ttb2025-02-14.aud.flac16", "Tedeschi Trucks Band",
        ["Tuning", "Anyhow", "Midnight in Harlem", "Bound for Glory"],
        _info("Tedeschi Trucks Band", "2025-02-14", "", "", "AUD",
              ["Anyhow", "Midnight in Harlem", "Bound for Glory"]),
        tag_titles=False, seconds=[25, 80, 80, 80]))
    # one waiting in the watch folder for the next scan
    _show(watch, "mmj2026-10-31.aud.flac16", "My Morning Jacket", ["One Big Holiday", "Wordless Chorus"])

    _seed_artists()


# ── stand-ins for the indexer and the download client ───────────────────────

def _seed_artists():
    from app import indexers, monitor
    from app.indexers.base import Indexer, Release

    def rel(i, title, artist, date, src, fmt="flac", added="2026-11-06"):
        return Release(indexer="lma", id=title, title=title, artist=artist, date=date,
                       source_type=src, formats=[fmt], added=added,
                       url=f"https://archive.org/details/{title}")

    found = {
        "Billy Strings": [rel(1, "bs2026-11-01.sbd.flac16", "Billy Strings", "2026-11-01", "SBD"),
                          rel(2, "bs2026-11-02.sbd.flac16", "Billy Strings", "2026-11-02", "SBD"),
                          rel(3, "bs2026-11-02.aud.mp3", "Billy Strings", "2026-11-02", "AUD", "mp3")],
        "Goose": [rel(4, "goose2026-11-04.sbd.flac16", "Goose", "2026-11-04", "SBD"),
                  rel(5, "goose2026-11-05.aud.flac24", "Goose", "2026-11-05", "AUD", "flac24"),
                  rel(6, "goose2026-11-05.aud.mp3", "Goose", "2026-11-05", "AUD", "mp3")],
        "Grateful Dead": [],
    }

    class DemoLMA(Indexer):
        name, label = "lma", "Live Music Archive"

        def new_for_artist(self, artist, since="", rows=50, collection=""):
            return list(found.get(artist, []))

        def count_for_artist(self, artist, collection=""):
            return 17402 if artist == "Grateful Dead" else len(found.get(artist, []))

        def similar_names(self, artist):
            return []

        def check_downloadable(self, release):
            return ""

        def grab(self, release_ids):
            return {"grabbed": len(release_ids)}

    indexers.register(DemoLMA())
    monitor.save_artist("Billy Strings", lookback_days=30, sources=["SBD", "MTX"], action="grab")
    monitor.save_artist("Goose", lookback_days=30, action="wanted")
    monitor.save_artist("Grateful Dead", lookback_days=0, sources=["SBD"], action="wanted",
                        collection="GratefulDead")
    for a in monitor.list_artists():
        monitor.check_artist(a)


def _fake_client():
    from app import clients
    from app.clients.base import AddResult, TorrentInfo
    now = time.time()
    gb = 1024 ** 3
    listing = [
        TorrentInfo(id="a", name="bs2026-11-01.sbd.flac16", status="downloading", queue=1,
                    size=int(0.76 * gb), done=int(0.47 * gb), progress=0.62, dl_speed=3_300_000,
                    ul_speed=95_000, eta=95, seeds=14, peers=31, ratio=0.04, added=now - 600,
                    label="reelarr"),
        TorrentInfo(id="b", name="bs2026-11-02.sbd.flac16", status="downloading", queue=2,
                    size=int(1.3 * gb), done=int(0.23 * gb), progress=0.18, dl_speed=870_000,
                    ul_speed=22_000, eta=1320, seeds=3, peers=6, ratio=0.0, added=now - 540,
                    label="reelarr"),
        TorrentInfo(id="c", name="gd1972-05-04.sbd.miller.flac16", status="queued", queue=3,
                    size=int(0.98 * gb), done=0, progress=0.0, seeds=8, peers=12, ratio=0.0,
                    added=now - 300, label="reelarr"),
        TorrentInfo(id="d", name="goose2026-10-30.aud.flac24", status="seeding", size=int(2.1 * gb),
                    done=int(2.1 * gb), progress=1.0, finished=True, ul_speed=410_000, seeds=22,
                    peers=40, ratio=1.37, added=now - 86400, label="reelarr"),
    ]

    class Demo:
        name, label_term = "qBittorrent", "category"

        def connect(self):
            return True

        def get_torrents(self, label=""):
            return listing

        def add_torrent_file(self, *a, **k):
            return AddResult(torrent_id="x")

        def add_magnet(self, *a, **k):
            return AddResult(torrent_id="x")

    clients.build = lambda cfg: Demo()


# ── serve and shoot ──────────────────────────────────────────────────────────

def _serve():
    import uvicorn
    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.1)
    raise SystemExit("the demo server didn't start")


def shoot():
    from playwright.sync_api import sync_playwright
    base = f"http://127.0.0.1:{PORT}"
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        exe = os.environ.get("CHROMIUM") or ("/opt/pw-browsers/chromium"
                                             if Path("/opt/pw-browsers/chromium").exists() else None)
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = b.new_page(viewport=SIZE, color_scheme="light", device_scale_factor=1)
        page.goto(base + "/login")
        page.locator("input[name=username]:visible").fill(USER)
        page.locator("input[name=password]:visible").fill(PASSWORD)
        page.keyboard.press("Enter")
        page.wait_for_url(lambda u: "/login" not in u, timeout=10000)

        def go(view, prep=None, name=None):
            page.click(f"nav button[data-view={view}]")
            page.wait_for_timeout(1800)
            if prep:
                prep(page)
                page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / f"{name or view}.png"))
            print("wrote", OUT / f"{name or view}.png")

        def open_tracks(pg):
            pg.locator("summary:has-text('Track titles')").first.click()

        go("shows")
        go("artists")
        go("queue")
        go("review", open_tracks)
        b.close()


def main():
    if not shutil.which("ffmpeg"):
        raise SystemExit("needs ffmpeg")
    # a short, fixed path, because it shows on the Shows page
    tmp = Path(tempfile.gettempdir()) / "demo"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _env(tmp)
        seed(tmp)
        _fake_client()
        _serve()
        shoot()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

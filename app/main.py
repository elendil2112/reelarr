"""Reelarr — FastAPI backend + GUI."""
import json
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, fileops, indexers, migrations, monitor, providers, setup, system, paths as paths_mod
from .version import __version__
from . import config, database as db, pipeline, scheduler, watcher, library_index, audit, sources, torrents, clients, lma

STATIC = Path(__file__).parent / "static"
providers.load()          # before anything reads settings: providers add defaults


def startup():
    try:
        db.init()
    except migrations.MigrationError as e:
        # say it plainly, once, where docker logs / journalctl will show it
        bar = "=" * 72
        print(f"\n{bar}\nReelarr {__version__} cannot start:\n\n  {e}\n{bar}\n",
              file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    config.load()
    _cleanup_bad_corrections()
    providers.startup_all()
    scheduler.start()
    if setup.media_problem():
        bar = "-" * 72
        print(f"\n{bar}\n[reelarr] {setup.MOUNT_HELP}\n{bar}\n", file=sys.stderr, flush=True)
        db.log("warn", "system", "the media folder Docker mounted is empty — see the setup wizard")
    if auth.setup_required():
        db.log("warn", "system", "no login has been created yet — open the web UI to finish setup")
    elif auth.mode() == "none":
        db.log("warn", "system", "authentication is OFF (chosen in setup) — anyone who can "
               "reach this port has full control. Fine behind your own SSO; not otherwise.")
    mode = "DRY RUN — planning only, no files will change" if fileops.dry_run_enabled() else "live"
    db.log("info", "system", f"Reelarr {__version__} started ({mode})")


@asynccontextmanager
async def _lifespan(_app):
    startup()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="Reelarr", version=__version__, lifespan=_lifespan)


def _cleanup_bad_corrections():
    """Self-heal: remove any learned artist correction whose two sides are
    wildly different (e.g. a band mislabeled during a Review approve, which then
    rewrote every future show by that artist). Runs once per boot; harmless
    when there's nothing to clean."""
    import difflib
    from .pipeline import _norm, _norm_words
    removed = []
    for c in library_index.corrections_list("artist"):
        wrong, right = c["wrong"], c["right"]
        if not isinstance(right, str):
            continue
        sim = difflib.SequenceMatcher(None, _norm(wrong), _norm(right)).ratio()
        shares = bool(_norm_words(wrong) & _norm_words(right))
        if sim < 0.4 and not shares:
            if library_index.correction_delete("artist", wrong):
                removed.append(f"{wrong} → {right}")
    if removed:
        db.log("warn", "system",
               "removed bad artist correction(s) that were mis-tagging shows: "
               + "; ".join(removed)
               + ". Re-scan or re-file affected shows to fix their tags.")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/setup")
def setup_page():
    return FileResponse(STATIC / "setup.html")


# ── Setup wizard ─────────────────────────────────────────────────────────────

@app.get("/api/setup/state")
def api_setup_state():
    return setup.state()


@app.get("/api/setup/browse")
def api_setup_browse(path: str = ""):
    r = setup.list_dir(path)
    return JSONResponse(r, status_code=400) if r.get("error") else r


@app.post("/api/setup/mkdir")
def api_setup_mkdir(body: dict = Body(...)):
    r = setup.make_dir((body.get("path") or "").strip())
    return JSONResponse(r, status_code=400) if r.get("error") else r


@app.post("/api/setup/check-folders")
def api_setup_check(body: dict = Body(...)):
    return setup.check_folders(body.get("watch", ""), body.get("library", ""),
                               body.get("torrent_dir", ""))


@app.post("/api/setup/folders")
def api_setup_folders(body: dict = Body(...)):
    r = setup.check_folders(body.get("watch", ""), body.get("library", ""))
    if not r["ok"]:
        return JSONResponse(r, status_code=400)
    config.save({"paths": {"watch_dir": str(Path(body["watch"]).expanduser()),
                           "library_dir": str(Path(body["library"]).expanduser())}})
    db.log("info", "setup", f"folders set: watch {body['watch']}, library {body['library']}")
    return r


@app.post("/api/setup/client/probe")
def api_setup_probe(body: dict = Body(default={})):
    r = setup.probe_client(body)
    return JSONResponse(r, status_code=400) if r.get("error") else r


@app.post("/api/setup/client/verify-mapping")
def api_setup_verify_mapping(body: dict = Body(...)):
    return setup.verify_mapping(body.get("client", ""), body.get("local", ""))


@app.post("/api/setup/finish")
def api_setup_finish(body: dict = Body(default={})):
    cfg = config.load()
    r = setup.check_folders(cfg["paths"]["watch_dir"], cfg["paths"]["library_dir"])
    if not r["ok"]:
        return JSONResponse({"error": "the folders need fixing first", **r}, status_code=400)
    setup.mark_done()
    scheduler.reschedule()
    if body.get("index_library", True):
        scheduler.run_in_background(library_index.scan, cfg["paths"]["library_dir"])
    if body.get("scan_now"):
        scheduler.run_in_background(watcher.scan, True)
    return {"ok": True}


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    if auth.is_public(path):
        return await call_next(request)
    who = auth.identify(request)
    if not who["ok"]:
        if path.startswith("/api/"):
            return JSONResponse({"error": "login required",
                                 "setup_required": who.get("setup_required", False)},
                                status_code=401)
        return RedirectResponse("/login", status_code=303)
    if who["via"] == "session" and request.method not in ("GET", "HEAD", "OPTIONS") \
            and not auth.same_origin(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    request.state.user = who.get("user")
    return await call_next(request)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _set_session(resp, request: Request, token: str):
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=auth.is_https(request),
                    max_age=int(auth._session_days() * 86400), path="/")


@app.get("/api/health")
def api_health():
    return {"ok": True, "version": __version__}


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    who = auth.identify(request)
    return {"mode": auth.mode(), "setup_required": auth.setup_required(),
            "logged_in": bool(who["ok"]), "via": who.get("via"),
            "username": (who.get("user") or {}).get("username"),
            "version": __version__}


@app.post("/api/auth/setup")
def api_auth_setup(request: Request, body: dict = Body(...)):
    """First run only: create the admin login, or deliberately choose no auth."""
    if not auth.setup_required():
        return JSONResponse({"error": "setup is already done"}, status_code=409)
    if body.get("mode") == "none":
        if (body.get("confirm") or "").strip().upper() != "NO LOGIN":
            return JSONResponse({"error": "type NO LOGIN to confirm"}, status_code=400)
        config.save({"auth": {"mode": "none"}})
        db.kv_set("auth_setup_done", "1")
        db.log("warn", "auth", "setup: authentication switched OFF by choice")
        return {"ok": True, "mode": "none"}
    err = auth.validate_credentials(body.get("username"), body.get("password"))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    uid = auth.create_user(body["username"], body["password"])
    config.save({"auth": {"mode": "forms"}})
    auth.api_key()      # mint one now, so it's there when automation needs it
    db.log("info", "auth", f"setup: admin login created ({body['username'].strip()})")
    resp = JSONResponse({"ok": True, "mode": "forms"})
    _set_session(resp, request, auth.new_session(
        uid, _client_ip(request), request.headers.get("user-agent", "")))
    return resp


@app.post("/api/auth/login")
def api_auth_login(request: Request, body: dict = Body(...)):
    ip = _client_ip(request)
    wait = auth.locked_out(ip)
    if wait:
        return JSONResponse({"error": f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s)."},
                            status_code=429)
    u = auth.get_user(body.get("username", ""))
    if not u or not auth.verify_password(body.get("password", ""), u["pw_hash"]):
        auth.record_failure(ip)
        db.log("warn", "auth", f"failed login for {(body.get('username') or '')[:40]!r} from {ip}")
        return JSONResponse({"error": "Wrong username or password."}, status_code=401)
    auth.clear_failures(ip)
    resp = JSONResponse({"ok": True})
    _set_session(resp, request, auth.new_session(u["id"], ip, request.headers.get("user-agent", "")))
    return resp


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/security")
def api_security():
    u = auth.first_user()
    return {"mode": auth.mode(), "username": u["username"] if u else None,
            "api_key": auth.api_key(), "session_days": auth._session_days()}


@app.post("/api/security/password")
def api_security_password(body: dict = Body(...)):
    u = auth.first_user()
    if not u:
        return JSONResponse({"error": "no login exists — create one below"}, status_code=400)
    if not auth.verify_password(body.get("current", ""), u["pw_hash"]):
        return JSONResponse({"error": "current password is wrong"}, status_code=400)
    err = auth.validate_credentials(u["username"], body.get("new", ""))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    auth.set_password(u["id"], body["new"])
    db.log("info", "auth", "password changed — other sessions signed out")
    return {"ok": True, "relogin": True}


@app.post("/api/security/apikey")
def api_security_apikey():
    k = auth.regenerate_api_key()
    db.log("info", "auth", "API key regenerated — update any scripts using the old one")
    return {"api_key": k}


@app.post("/api/security/mode")
def api_security_mode(request: Request, body: dict = Body(...)):
    want = body.get("mode")
    if want == "none":
        u = auth.first_user()
        if u and not auth.verify_password(body.get("password", ""), u["pw_hash"]):
            return JSONResponse({"error": "enter your current password to turn login off"},
                                status_code=400)
        config.save({"auth": {"mode": "none"}})
        db.log("warn", "auth", "authentication switched OFF")
        return {"mode": "none"}
    if want == "forms":
        u = auth.first_user()
        if not u:
            err = auth.validate_credentials(body.get("username"), body.get("password"))
            if err:
                return JSONResponse({"error": err}, status_code=400)
            uid = auth.create_user(body["username"], body["password"])
        else:
            uid = u["id"]
        config.save({"auth": {"mode": "forms"}})
        db.log("info", "auth", "authentication switched ON")
        resp = JSONResponse({"mode": "forms"})
        _set_session(resp, request, auth.new_session(
            uid, _client_ip(request), request.headers.get("user-agent", "")))
        return resp
    return JSONResponse({"error": "mode must be forms or none"}, status_code=400)


@app.get("/api/providers")
def api_providers():
    return {"providers": [p.describe() for p in providers.all()],
            "errors": providers.errors()}


# ── Status / dashboard ───────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    cfg = config.load()
    watch = Path(cfg["paths"]["watch_dir"])
    library = Path(cfg["paths"]["library_dir"])
    pending_dirs = 0
    if watch.is_dir():
        pending_dirs = sum(1 for p in watch.iterdir()
                           if p.is_dir() and not p.name.startswith((".", "_")))
    return {
        "counts": db.counts(),
        "watch_dir_ok": watch.is_dir(),
        "library_dir_ok": library.is_dir(),
        "folders_in_watch": pending_dirs,
        "last_scan": db.kv_get("last_scan"),
        "scan_running": watcher.is_running(),
        "providers": providers.statuses(),
        "wanted": monitor.counts().get("wanted", 0),
        "monitor_running": monitor.status().get("running", False),
        "library_index": library_index.counts(),
        "next_runs": scheduler.next_runs(),
    }


@app.get("/api/shows")
def api_shows(status: str = None, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    rows = db.list_shows(status=status, limit=limit, offset=max(0, offset))
    for r in rows:
        for k in ("meta", "provenance", "missing"):
            try:
                r[k] = json.loads(r[k] or "null")
            except Exception:
                pass
    return {"items": rows, "total": db.shows_count(status)}


@app.get("/api/log")
def api_log(limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 300))
    return {"items": db.recent_log(limit, max(0, offset)),
            "total": db.log_count()}


@app.get("/api/artists")
def api_artists():
    lib = Path(config.load()["paths"]["library_dir"])
    if not lib.is_dir():
        return []
    return sorted(d.name for d in lib.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_")))


# ── Monitored artists, wanted, queue ─────────────────────────────────────────

@app.get("/api/monitor/artists")
def api_monitor_artists():
    try:
        library = sorted(set(library_index.known_artists().values()), key=str.lower)
    except Exception:
        library = []
    return {"artists": monitor.list_artists(), "library": library,
            "indexers": [{"name": i.name, "label": i.label} for i in indexers.all()],
            "status": monitor.status(),
            "gates": monitor.gates(),
            "settings": config.load().get("monitor", {})}


@app.post("/api/monitor/artists")
def api_monitor_save(body: dict = Body(...)):
    try:
        a = monitor.save_artist(body.get("name", ""), lookback_days=body.get("lookback_days"),
                                **{k: body.get(k) for k in monitor.FIELDS})
    except (ValueError, TypeError, indexers.IndexerError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if a and not a.get("checked_at") and a.get("monitored") and setup.done():
        # a newly added artist is checked straight away, not at the next 12-hour run
        scheduler.run_in_background(monitor.check_artist, a)
        a = {**a, "checking": True}
    return a


@app.post("/api/monitor/artists/{artist_id}/lookback")
def api_monitor_lookback(artist_id: int, body: dict = Body(...)):
    if not setup.done():
        return JSONResponse({"error": "finish the setup wizard first"}, status_code=409)
    try:
        a = monitor.look_back(artist_id, body.get("days"))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    scheduler.run_in_background(monitor.check_artist, a)
    return {**a, "checking": True}


@app.delete("/api/monitor/artists/{artist_id}")
def api_monitor_remove(artist_id: int):
    return {"removed": monitor.remove_artist(artist_id)}


@app.post("/api/monitor/artists/{artist_id}/check")
def api_monitor_check(artist_id: int):
    a = monitor.get_artist(artist_id)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not setup.done():
        return JSONResponse({"error": "finish the setup wizard first"}, status_code=409)
    return monitor.check_artist(a)


@app.post("/api/monitor/run")
def api_monitor_run():
    if not setup.done():
        return JSONResponse({"error": "finish the setup wizard first"}, status_code=409)
    scheduler.run_in_background(monitor.run_all)
    return {"started": True}


@app.get("/api/releases")
def api_releases(status: str = "wanted", limit: int = 200):
    return {"items": monitor.list_releases(status, max(1, min(limit, 1000))),
            "counts": monitor.counts()}


@app.post("/api/releases/{rid}/grab")
def api_release_grab(rid: int):
    r = monitor.grab_release(rid)
    return JSONResponse(r, status_code=400) if r.get("error") else r


@app.post("/api/releases/{rid}/ignore")
def api_release_ignore(rid: int):
    return {"ok": monitor.ignore_release(rid)}


@app.get("/api/queue")
def api_queue():
    cfg = config.load()
    out = {"wanted": monitor.list_releases("wanted", 100),
           "grabbed": monitor.list_releases("grabbed", 100),
           "skipped": monitor.list_releases("skipped", 50),
           "dry_run": fileops.dry_run_enabled(),
           "torrent_folder": bool((cfg.get("torrents", {}) or {}).get("watch_dir")),
           "downloading": [], "client": None,
           "intake": [dict(id=r["id"], folder_name=r["folder_name"], status=r["status"],
                           updated_at=r["updated_at"], notes=r["notes"])
                      for st in ("processing", "pending", "planned")
                      for r in db.list_shows(status=st, limit=100)]}
    t = cfg.get("torrents", {})
    if t.get("enabled") and t.get("url"):
        try:
            c = clients.build(t)
            c.connect()
            out["client"] = c.name
            out["label"] = t.get("label", "")
            done = torrents._load_imported()
            importing = bool(t.get("import_completed"))
            seen = []
            for x in c.get_torrents(t.get("label", "")):
                d = x.to_dict()
                d.pop("save_path", None); d.pop("content_path", None)
                handed_over = x.finished and (x.id in done or not importing)
                if x.finished:
                    d["status"] = "imported" if handed_over else "import pending"
                    d["eta"] = None
                seen.append(d)
                if not handed_over:             # handed over to the library: not in the queue
                    out["downloading"].append(d)
            _attach_client(out["grabbed"], seen)
        except Exception as e:
            out["client_error"] = str(e)
    return out


def _torrent_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _attach_client(grabbed: list, seen: list):
    """Give each grabbed release its torrent's status, progress and ETA, so
    Recently grabbed shows how far along it is. An archive.org torrent is
    named after the item, so the release id is the match."""
    by_name = {}
    for d in seen:
        by_name.setdefault(_torrent_key(d.get("name")), d)
    for g in grabbed:
        keys = [_torrent_key(g.get("release_id")), _torrent_key((g.get("data") or {}).get("title"))]
        d = next((by_name[k] for k in keys if k and k in by_name), None)
        if d is None:
            rid = keys[0]
            d = next((v for k, v in by_name.items() if rid and len(rid) >= 8 and k.startswith(rid)), None)
        g["client"] = None if d is None else {
            "status": d.get("status") or d.get("state") or "", "progress": d.get("progress"),
            "eta": d.get("eta"), "dl_speed": d.get("dl_speed"), "name": d.get("name")}


# ── Discover (indexers) ──────────────────────────────────────────────────────

@app.post("/api/discover/search")
def api_discover_search(body: dict = Body(default={})):
    q = (body.get("q") or "").strip()
    if not q:
        return JSONResponse({"error": "Type something to search for."}, status_code=400)
    try:
        idx = indexers.get(body.get("indexer") or "lma")
        out = idx.search(q, page=int(body.get("page", 1)), rows=int(body.get("rows", 25)))
    except indexers.IndexerError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    out["results"] = [r.to_dict() for r in out["results"]]
    out["indexer"] = idx.name
    return out


@app.post("/api/discover/grab")
def api_discover_grab(body: dict = Body(default={})):
    ids = body.get("ids") or []
    if not ids:
        return JSONResponse({"error": "Nothing selected."}, status_code=400)
    try:
        idx = indexers.get(body.get("indexer") or "lma")
        if body.get("check", True) and len(ids) <= 5:
            from .indexers.base import Release
            blocked = {i: idx.check_downloadable(Release(indexer=idx.name, id=i)) for i in ids}
            blocked = {i: why for i, why in blocked.items() if why}
            ids = [i for i in ids if i not in blocked]
        else:
            blocked = {}
        out = idx.grab(ids) if ids else {"grabbed": 0, "skipped": 0, "failed": 0}
    except indexers.IndexerError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    out["blocked"] = blocked
    if out.get("grabbed") and config.load()["torrents"].get("enabled"):
        scheduler.run_in_background(torrents.scan, True)
        out["handed_on"] = True
    return out


# ── System ───────────────────────────────────────────────────────────────────

@app.get("/api/system/health")
def api_system_health():
    return {"checks": system.health(), "version": __version__,
            "schema": migrations.status(), "backups": system.list_backups(),
            "config_dir": str(paths_mod.config_dir()),
            "providers": [p.describe()["name"] for p in providers.all()]}


@app.post("/api/system/backup")
def api_system_backup():
    return system.make_backup()


@app.get("/api/system/backup/{name}")
def api_system_backup_download(name: str):
    p = system.backup_path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, filename=p.name, media_type="application/zip")


# ── Actions ──────────────────────────────────────────────────────────────────

@app.post("/api/scan")
def api_scan(force: bool = False):
    scheduler.run_in_background(watcher.scan, force)
    return {"started": True, "force": force}


@app.post("/api/scan/stop")
def api_scan_stop():
    return watcher.request_stop()


@app.post("/api/show/{show_id}/approve")
def api_approve(show_id: int, meta: dict = Body(default={})):
    result = pipeline.approve_show(show_id, meta)
    code = 400 if "error" in result else 200
    return JSONResponse(result, status_code=code)


@app.post("/api/show/{show_id}/replace")
def api_replace(show_id: int, meta: dict = Body(default={})):
    result = pipeline.replace_show(show_id, meta)
    return JSONResponse(result, status_code=400 if "error" in result else 200)


@app.post("/api/review/toss_duplicates")
def api_toss_duplicates():
    return pipeline.toss_duplicates()


@app.post("/api/show/{show_id}/setlist_lookup")
def api_setlist_lookup(show_id: int, body: dict = Body(default={})):
    """Manually pull setlist.fm metadata for a Review show by its artist + date.
    Returns proposed venue/city/state/tracks for review — does NOT commit; the
    you approves via the normal Review flow. Artist/date can be overridden in
    the body (the show's stored values are the default)."""
    row = db.get_show(show_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    cfg = config.load()
    key = cfg["sources"].get("setlistfm_api_key", "")
    if not key:
        return JSONResponse(
            {"error": "No setlist.fm API key — add one in Settings."},
            status_code=400)
    meta = json.loads(row["meta"] or "{}")
    artist = (body.get("artist") or meta.get("artist") or "").strip()
    date = (body.get("date") or meta.get("date") or "").strip()
    if not artist or not date:
        return JSONResponse(
            {"error": "This show has no artist/date to search on — fill those "
                      "in first."},
            status_code=400)
    slf = sources.fetch_setlistfm(artist, date, key)
    if not slf:
        return {"found": False,
                "message": f"No setlist.fm match for {artist} on {date}."}
    tracks = [t.get("title", "") for t in (slf.tracks or []) if t.get("title")]
    # persist venue + tracklist into the show's stored meta so that when the
    # you approves, the setlist is written through (approve reads tracks
    # from stored meta). Survives a page refresh too. Editable fields still let
    # you override before approving.
    meta["venue"] = slf.venue or meta.get("venue", "")
    meta["city"] = slf.city or meta.get("city", "")
    meta["state"] = slf.state or meta.get("state", "")
    if slf.tracks:
        meta["tracks"] = slf.tracks
    note = (row.get("notes") or "")
    stamp = f"setlist.fm: {slf.venue}, {slf.city} ({len(tracks)} tracks)"
    db.update_show(show_id, meta=meta,
                   notes=(note + " | " if note else "") + stamp)
    return {"found": True, "artist": artist, "date": date,
            "venue": slf.venue, "city": slf.city, "state": slf.state,
            "tracks": tracks, "track_count": len(tracks)}


@app.post("/api/show/{show_id}/reprocess")
def api_reprocess(show_id: int):
    row = db.get_show(show_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(row["current_path"])
    if not path.exists():
        return JSONResponse({"error": "folder no longer exists"}, status_code=400)
    db.update_show(show_id, status="pending")
    scheduler.run_in_background(pipeline.process_show, path)
    return {"started": True}


@app.post("/api/show/{show_id}/reject")
def api_reject(show_id: int):
    """Move to _rejected inside the watch folder; keeps the files, gets them out of the way."""
    row = db.get_show(show_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = Path(row["current_path"])
    if path.exists():
        dest_root = Path(config.load()["paths"]["watch_dir"]) / "_rejected"
        b = fileops.Batch(f"reject {path.name}", kind="reject", show_id=show_id,
                          dry_run=False, undo_state=fileops.show_undo_state(show_id))
        b.mkdir(dest_root)
        dest = dest_root / path.name
        if not dest.exists():
            b.move(path, dest)
        db.update_show(show_id, status="rejected", current_path=str(dest))
    else:
        db.update_show(show_id, status="rejected")
    db.log("info", "reject", path.name, show_id)
    return {"ok": True}


@app.delete("/api/show/{show_id}")
def api_delete_record(show_id: int):
    db.delete_show(show_id)
    return {"ok": True}


@app.post("/api/library/rescan")
def api_library_rescan():
    cfg = config.load()
    scheduler.run_in_background(library_index.scan, cfg["paths"]["library_dir"])
    return {"started": True}


# ── Maintenance (learning & audit) ─────────────────────────────────────────

@app.post("/api/audit/learn")
def api_audit_learn():
    scheduler.run_in_background(audit.run_learn_job)
    return {"started": True}


@app.get("/api/audit/learn/status")
def api_audit_learn_status():
    return audit.job_state("audit_learn")


@app.post("/api/audit/return")
def api_audit_return():
    scheduler.run_in_background(audit.run_return_job)
    return {"started": True}


@app.get("/api/audit/return/status")
def api_audit_return_status():
    return audit.job_state("audit_return")


@app.post("/api/audit/suspects/run")
def api_audit_suspects_run():
    scheduler.run_in_background(audit.run_suspects_job)
    return {"started": True}


@app.get("/api/audit/suspects/status")
def api_audit_suspects_status():
    return audit.job_state("audit_suspects")


@app.post("/api/backtag/run")
def api_backtag_run(dry_run: bool = True):
    from . import backtag
    scheduler.run_in_background(backtag.run_backtag_job, dry_run)
    return {"started": True, "dry_run": dry_run}


@app.get("/api/backtag/status")
def api_backtag_status():
    import json as _json
    raw = db.kv_get("backtag")
    return _json.loads(raw) if raw else {"status": "idle"}


@app.post("/api/audit/export")
def api_audit_export():
    scheduler.run_in_background(audit.run_export_job)
    return {"started": True}


@app.get("/api/audit/export/status")
def api_audit_export_status():
    return {**audit.job_state("audit_export"), "exports": audit.list_exports()}


@app.get("/api/audit/download/{name}")
def api_audit_download(name: str):
    p = audit.EXPORT_DIR / Path(name).name
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, filename=p.name, media_type="application/json")


@app.get("/api/torrents/status")
def api_torrents_status():
    return {**torrents.status(), "last_run_db": db.kv_get("last_torrent_run")}


@app.post("/api/torrents/scan")
def api_torrents_scan():
    scheduler.run_in_background(torrents.scan, True)
    return {"started": True}


@app.post("/api/torrents/import")
def api_torrents_import():
    scheduler.run_in_background(torrents.import_completed, True)
    return {"started": True}


@app.get("/api/torrents/clients")
def api_torrent_clients():
    return {"clients": clients.describe(), "default": clients.DEFAULT_CLIENT}


@app.post("/api/torrents/test")
def api_torrents_test(body: dict = Body(default={})):
    """Try the configured (or supplied) download client and report back."""
    cfg = dict(config.load()["torrents"])
    for k in ("client", "url", "username", "password"):
        if body.get(k) not in (None, "", config.MASK):
            cfg[k] = body[k]
    if not (cfg.get("url") or "").strip():
        return JSONResponse({"error": "Set the download client's address first."},
                            status_code=400)
    try:
        client = clients.build(cfg)
        info = client.test()
        want = client.normalize_label(cfg.get("label", ""))
        info["label"] = want
        info["label_exists"] = want.lower() in [l.lower() for l in info.get("labels", [])]
        return info
    except clients.DownloadClientError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"{e}"}, status_code=400)


@app.post("/api/lma/search")
def api_lma_search(body: dict = Body(default={})):
    """Search the Live Music Archive, or open an archive.org item/collection."""
    q = (body.get("q") or "").strip()
    if not q:
        return JSONResponse({"error": "Type something to search for."},
                            status_code=400)
    try:
        return lma.lookup(q, rows=int(body.get("rows", 25)),
                          page=int(body.get("page", 1)))
    except lma.LmaError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"{e}"}, status_code=400)


@app.post("/api/lma/grab")
def api_lma_grab(body: dict = Body(default={})):
    """Pull .torrent files into the torrent folder, where the normal torrent
    run hands them to the download client."""
    ids = body.get("identifiers") or ([body["identifier"]]
                                      if body.get("identifier") else [])
    if not ids:
        return JSONResponse({"error": "Nothing selected."}, status_code=400)
    try:
        out = lma.grab(ids)
    except lma.LmaError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # hand them straight on rather than waiting for the next poll
    try:
        if out.get("grabbed") and config.load()["torrents"].get("enabled"):
            scheduler.run_in_background(torrents.scan, True)
            out["handed_on"] = True
    except Exception:
        pass
    return out


@app.get("/api/corrections")
def api_corrections(kind: str = None):
    return {"corrections": library_index.corrections_list(kind)}


@app.post("/api/corrections/delete")
def api_corrections_delete(payload: dict = Body(...)):
    kind = (payload.get("kind") or "").strip()
    wrong = (payload.get("wrong") or "").strip()
    if not kind or not wrong:
        return JSONResponse({"error": "kind and wrong required"}, status_code=400)
    ok = library_index.correction_delete(kind, wrong)
    return {"deleted": ok}


# ── System, safety, undo ─────────────────────────────────────────────────────

@app.get("/api/system")
def api_system():
    return {"version": __version__, "setup_done": setup.done(), "schema": migrations.status(),
            "config_dir": str(paths_mod.config_dir()),
            "backups_dir": str(paths_mod.backups_dir()),
            "dry_run": fileops.dry_run_enabled()}


@app.get("/api/safety")
def api_safety():
    s = config.load().get("safety", {})
    return {"dry_run": bool(s.get("dry_run")), "trash_days": s.get("trash_days", 30),
            "planned": db.shows_count("planned")}


@app.post("/api/safety/dry-run")
def api_set_dry_run(payload: dict = Body(...)):
    """Turning dry-run OFF must be deliberate: the client has to send the
    confirmation phrase, so a stray settings save can't do it."""
    on = bool(payload.get("on"))
    if not on and (payload.get("confirm") or "").strip().upper() != "GO LIVE":
        return JSONResponse({"error": "type GO LIVE to confirm"}, status_code=400)
    config.save({"safety": {"dry_run": on}})
    if on:
        db.log("info", "safety", "dry run switched ON — intake will plan, not change files")
        return {"dry_run": True}
    planned = db.shows_count("planned")
    db.log("warn", "safety", f"dry run switched OFF — going live; {planned} planned "
           f"show(s) will be processed for real on the next scan")
    for row in db.list_shows(status="planned", limit=100000):
        db.update_show(row["id"], status="pending", plan="")
    scheduler.run_in_background(watcher.scan, True)
    return {"dry_run": False, "released": planned}


@app.get("/api/show/{show_id}/plan")
def api_show_plan(show_id: int):
    row = db.get_show(show_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return json.loads(row.get("plan") or "{}")
    except ValueError:
        return {}


@app.get("/api/activity/actions")
def api_actions(limit: int = 30, offset: int = 0):
    return {"items": fileops.list_batches(max(1, min(limit, 200)), max(0, offset))}


@app.get("/api/activity/actions/{batch_id}")
def api_action_ops(batch_id: int):
    return {"ops": fileops.batch_ops(batch_id)}


@app.post("/api/activity/actions/{batch_id}/undo")
def api_undo(batch_id: int):
    r = fileops.undo(batch_id)
    if r.get("error"):
        return JSONResponse(r, status_code=400)
    return r


# ── Settings ─────────────────────────────────────────────────────────────────

@app.get("/api/settings")
def api_get_settings():
    return config.masked(config.load())


@app.put("/api/settings")
def api_put_settings(new: dict = Body(...)):
    new = dict(new or {})
    new.pop("auth", None)       # login mode changes go through /api/security/mode
    if "timezone" in new:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(new["timezone"] or "")
        except Exception:
            return JSONResponse({"error": f"unknown time zone {new['timezone']!r}"}, status_code=400)
    s = new.get("safety")
    if isinstance(s, dict):
        s.pop("dry_run", None)  # and dry-run through the GO LIVE confirmation
    merged = config.save(new)
    scheduler.reschedule()
    db.log("info", "settings", "settings updated")
    return config.masked(merged)


_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def _security_headers(request, call_next):
    """Defence in depth for the web UI: no framing (clickjacking), no
    third-party scripts, no MIME sniffing, no referrer leaks."""
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


@app.middleware("http")
async def _no_stale_static(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/static") or p in ("/", "/index.html"):
        # a stale cached app.js after a deploy = buttons wired to nothing.
        # Force revalidation so the GUI always matches the backend.
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


providers.mount(app)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

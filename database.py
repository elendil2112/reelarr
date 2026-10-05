"""Reelarr state — SQLite in /config so it survives container rebuilds.

The schema itself lives in migrations.py; this module only reads and writes."""
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import paths
DB_PATH = paths.config_dir() / "reelarr.db"
_local = threading.local()


def _conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn = c
    return _local.conn


def init():
    """Bring every store up to date. Raises migrations.MigrationError — with a
    message meant for a person — if that can't be done safely."""
    from . import migrations
    reports = migrations.run_all()
    for r in reports:
        if r.get("notes"):
            log("info", "system", "carried over from Barbosa: " + "; ".join(r["notes"]))
        elif r.get("from") != r.get("to") and r.get("from") is not None:
            log("info", "system",
                f"upgraded {r['store']} v{r['from']} → v{r['to']}"
                + (f" (backup: {r['backup']})" if r.get("backup") else ""))
    return reports


# ── Shows ─────────────────────────────────────────────────────────────────────

def get_show_by_path(path: str):
    r = _conn().execute("SELECT * FROM shows WHERE current_path=?", (path,)).fetchone()
    return dict(r) if r else None


def get_show_by_filed(path: str):
    r = _conn().execute("SELECT * FROM shows WHERE filed_path=?", (path,)).fetchone()
    return dict(r) if r else None


def get_show(show_id: int):
    r = _conn().execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchone()
    return dict(r) if r else None


def upsert_show(path: str, **fields) -> int:
    now = time.time()
    existing = get_show_by_path(path)
    if existing:
        update_show(existing["id"], **fields)
        return existing["id"]
    folder_name = fields.pop("folder_name", Path(path).name)
    cols = {"folder_name": folder_name, "current_path": path,
            "created_at": now, "updated_at": now}
    for k in ("status", "confidence", "meta", "provenance", "missing",
              "notes", "source_hint", "filed_path", "error"):
        if k in fields:
            v = fields[k]
            cols[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    keys = ",".join(cols)
    q = ",".join("?" for _ in cols)
    cur = _conn().execute(f"INSERT INTO shows ({keys}) VALUES ({q})", list(cols.values()))
    _conn().commit()
    return cur.lastrowid


def update_show(show_id: int, **fields):
    sets, vals = ["updated_at=?"], [time.time()]
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(show_id)
    _conn().execute(f"UPDATE shows SET {', '.join(sets)} WHERE id=?", vals)
    _conn().commit()


def list_shows(status=None, limit=200, offset=0):
    if status:
        rows = _conn().execute(
            "SELECT * FROM shows WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM shows ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def shows_on_date(date: str) -> list:
    """Filed shows recorded on a given date — used to flag "you already have
    this" when browsing the Live Music Archive. Matches on the date string,
    which appears in both the folder name and the stored metadata."""
    if not date:
        return []
    like = f"%{date}%"
    rows = _conn().execute(
        "SELECT filed_path, meta FROM shows WHERE status='filed' "
        "AND (filed_path LIKE ? OR meta LIKE ?) LIMIT 40", (like, like)).fetchall()
    return [dict(r) for r in rows]


def shows_count(status=None) -> int:
    if status:
        return _conn().execute("SELECT COUNT(*) n FROM shows WHERE status=?",
                               (status,)).fetchone()["n"]
    return _conn().execute("SELECT COUNT(*) n FROM shows").fetchone()["n"]


def counts():
    rows = _conn().execute("SELECT status, COUNT(*) n FROM shows GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def delete_show(show_id: int):
    _conn().execute("DELETE FROM shows WHERE id=?", (show_id,))
    _conn().commit()


# ── Log ───────────────────────────────────────────────────────────────────────

_SECRET_URL = re.compile(r"(://)[^/\s?#]*@")          # up to the LAST @: passwords may contain @
_SECRET_PARAM = re.compile(r"((?:pass(?:word)?|passwd|pwd|token|api_?key|apikey|secret)=)[^&\s\"']+",
                           re.IGNORECASE)


_SECRET_HEADER = re.compile(r"((?:authorization|x-api-key|cookie)\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;'\"}]+",
                            re.IGNORECASE)
_SECRET_KV = re.compile(r"""(["']?(?:password|passwd|token|secret|api_?key|bot_token)["']?\s*[:=]\s*["']?)[^"',\s}&]+""",
                        re.IGNORECASE)


def redact(text: str) -> str:
    """Strip credentials from anything about to be logged:
    http://user:pass@host → http://***@host · ?apikey=abc → ?apikey=***"""
    if not text:
        return text
    text = _SECRET_URL.sub(r"\1***@", str(text))
    text = _SECRET_HEADER.sub(r"\1***", text)
    text = _SECRET_PARAM.sub(r"\1***", text)
    return _SECRET_KV.sub(r"\1***", text)


def log(level: str, event: str, detail: str = "", show_id=None):
    detail = redact(detail)
    _conn().execute(
        "INSERT INTO log (ts, level, event, detail, show_id) VALUES (?,?,?,?,?)",
        (time.time(), level, event, detail, show_id))
    _conn().commit()


def recent_log(limit=300, offset=0):
    rows = _conn().execute(
        "SELECT * FROM log ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def log_count() -> int:
    return _conn().execute("SELECT COUNT(*) n FROM log").fetchone()["n"]


# ── KV ────────────────────────────────────────────────────────────────────────

def kv_get(k, default=None):
    r = _conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    _conn().execute("INSERT INTO kv (k,v) VALUES (?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    _conn().commit()

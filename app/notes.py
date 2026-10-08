"""Notes — your own scratchpad inside Reelarr: tapes to chase, trades in
progress, which source of a show you liked. Saved as you type.

Every save carries the revision the page last saw. If another tab or device
saved in between, the save is refused (and the page offers both versions)
rather than one silently overwriting the other."""
import time

from . import database as db

MAX_TITLE = 200
MAX_BODY = 1_000_000          # characters — a lot of notes, but not a database dump


class Conflict(Exception):
    def __init__(self, current: dict):
        super().__init__("changed elsewhere")
        self.current = current


def _c():
    return db._conn()


def _full(r) -> dict:
    return {"id": r["id"], "title": r["title"], "body": r["body"], "pinned": bool(r["pinned"]),
            "rev": r["rev"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


def list_notes() -> list:
    """Newest first, pinned on top. Bodies are trimmed to a preview; the
    whole text is included too so the list can be searched without fetching
    each note."""
    rows = _c().execute("SELECT * FROM notes WHERE deleted_at IS NULL "
                        "ORDER BY pinned DESC, updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = _full(r)
        first = next((ln.strip() for ln in r["body"].splitlines() if ln.strip()), "")
        d["preview"] = first[:140]
        out.append(d)
    return out


def get(nid: int) -> dict | None:
    r = _c().execute("SELECT * FROM notes WHERE id=? AND deleted_at IS NULL", (nid,)).fetchone()
    return _full(r) if r else None


def create(title: str = "", body: str = "") -> dict:
    now = time.time()
    c = _c()
    cur = c.execute("INSERT INTO notes (title, body, created_at, updated_at) VALUES (?,?,?,?)",
                    ((title or "")[:MAX_TITLE], (body or "")[:MAX_BODY], now, now))
    c.commit()
    return get(cur.lastrowid)


def save(nid: int, rev: int, title=None, body=None, pinned=None) -> dict:
    """Write whichever fields were sent. `rev` must match the stored revision."""
    if title is not None and len(title) > MAX_TITLE:
        raise ValueError(f"Titles are limited to {MAX_TITLE} characters.")
    if body is not None and len(body) > MAX_BODY:
        raise ValueError("That note is too long to save (over a million characters).")
    c = _c()
    sets, args = [], []
    for col, val in (("title", title), ("body", body)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if pinned is not None:
        sets.append("pinned=?")
        args.append(1 if pinned else 0)
    if not sets:
        n = get(nid)
        if n is None:
            raise KeyError(nid)
        return n
    sets += ["rev=rev+1", "updated_at=?"]
    args += [time.time(), nid, int(rev)]
    cur = c.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id=? AND rev=? AND deleted_at IS NULL", args)
    c.commit()
    if cur.rowcount == 0:
        now = get(nid)
        if now is None:
            raise KeyError(nid)
        raise Conflict(now)
    return get(nid)


def delete(nid: int) -> bool:
    c = _c()
    cur = c.execute("UPDATE notes SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (time.time(), nid))
    c.commit()
    return cur.rowcount > 0


def restore(nid: int) -> dict | None:
    c = _c()
    c.execute("UPDATE notes SET deleted_at=NULL WHERE id=?", (nid,))
    c.commit()
    return get(nid)


def purge(older_than_days: int = 30) -> int:
    """Deleted notes are kept for a month (Undo, or a restore from backup), then dropped."""
    c = _c()
    cur = c.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                    (time.time() - older_than_days * 86400,))
    c.commit()
    return cur.rowcount

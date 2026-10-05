"""Authentication: one admin login, browser sessions, and an API key.

Modes (settings → auth.mode):
  forms  — the default. A login page; a session cookie afterwards.
  none   — no login at all. For people who put Reelarr behind their own SSO
           or reverse-proxy auth. It has to be chosen on purpose (setup, or
           Settings → Security with the current password).

Until the first-run setup has happened, every API call answers 401 with
setup_required=true and the UI shows the "create a login" screen.

Automation authenticates with the API key — header X-Api-Key, or ?apikey=
for tools that can only call a URL.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

from . import config, database as db

COOKIE = "reelarr_session"
API_HEADER = "x-api-key"

# ── Password hashing (stdlib scrypt, no extra dependencies) ──────────────────

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b = lambda x: base64.b64encode(x).decode()
    return f"scrypt${_N}${_R}${_P}${b(salt)}${b(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        alg, n, r, p, salt, dk = stored.split("$")
        if alg != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(want))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── State ────────────────────────────────────────────────────────────────────

def mode() -> str:
    m = (config.load().get("auth", {}) or {}).get("mode", "forms")
    return m if m in ("forms", "none") else "forms"


def user_count() -> int:
    return db._conn().execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def setup_done() -> bool:
    return db.kv_get("auth_setup_done") == "1" or user_count() > 0


def setup_required() -> bool:
    return not setup_done()


def validate_credentials(username: str, password: str):
    username = (username or "").strip()
    if not username or len(username) > 64:
        return "Choose a username (up to 64 characters)."
    if len(password or "") < 8:
        return "Use a password of at least 8 characters."
    return None


def create_user(username: str, password: str) -> int:
    cur = db._conn().execute(
        "INSERT INTO users (username, pw_hash, created_at) VALUES (?,?,?)",
        (username.strip(), hash_password(password), time.time()))
    db._conn().commit()
    db.kv_set("auth_setup_done", "1")
    return cur.lastrowid


def get_user(username: str):
    r = db._conn().execute("SELECT * FROM users WHERE username=?",
                           ((username or "").strip(),)).fetchone()
    return dict(r) if r else None


def first_user():
    r = db._conn().execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return dict(r) if r else None


def set_password(user_id: int, password: str):
    db._conn().execute("UPDATE users SET pw_hash=? WHERE id=?",
                       (hash_password(password), user_id))
    # a password change signs out every other session
    db._conn().execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db._conn().commit()


# ── Sessions ─────────────────────────────────────────────────────────────────

def _session_days() -> float:
    try:
        return float(config.load().get("auth", {}).get("session_days", 30))
    except (TypeError, ValueError):
        return 30.0


def new_session(user_id: int, ip: str = "", agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db._conn().execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, ip, agent) "
        "VALUES (?,?,?,?,?,?,?)",
        (_digest(token), user_id, now, now + _session_days() * 86400, now, ip, agent[:200]))
    db._conn().execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db._conn().commit()
    return token


def session_user(token: str):
    if not token:
        return None
    now = time.time()
    r = db._conn().execute(
        "SELECT u.id, u.username, s.last_seen FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash=? AND s.expires_at > ?", (_digest(token), now)).fetchone()
    if not r:
        return None
    if now - (r["last_seen"] or 0) > 300:     # don't write on every request
        db._conn().execute("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                           (now, _digest(token)))
        db._conn().commit()
    return {"id": r["id"], "username": r["username"]}


def end_session(token: str):
    if token:
        db._conn().execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))
        db._conn().commit()


# ── API key ──────────────────────────────────────────────────────────────────

def api_key() -> str:
    k = db.kv_get("api_key")
    if not k:
        k = regenerate_api_key()
    return k


def regenerate_api_key() -> str:
    k = secrets.token_hex(16)
    db.kv_set("api_key", k)
    return k


def api_key_ok(candidate: str) -> bool:
    if not candidate:
        return False
    k = db.kv_get("api_key")
    return bool(k) and hmac.compare_digest(candidate.strip(), k)


# ── Login throttling ─────────────────────────────────────────────────────────
# 5 failures from one address within 15 minutes locks that address out for
# 5 minutes. In memory: a restart clears it, which is fine for a home app.

_fail_lock = threading.Lock()
_failures = {}          # ip -> [timestamps]
MAX_FAILS, WINDOW, LOCKOUT = 5, 900, 300


def locked_out(ip: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    with _fail_lock:
        times = [t for t in _failures.get(ip, []) if time.time() - t < WINDOW]
        _failures[ip] = times
        if len(times) >= MAX_FAILS:
            remaining = LOCKOUT - (time.time() - times[-1])
            return max(0, int(remaining))
    return 0


def record_failure(ip: str):
    with _fail_lock:
        _failures.setdefault(ip, []).append(time.time())


def clear_failures(ip: str):
    with _fail_lock:
        _failures.pop(ip, None)


# ── Request check (used by the middleware) ───────────────────────────────────

PUBLIC_PREFIXES = ("/static/", "/providers/", "/api/auth/", "/api/health")
PUBLIC_PATHS = {"/login", "/favicon.ico"}


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def identify(request) -> dict:
    """Who is making this request. Returns {"ok": bool, "via": ..., "user": ...}."""
    if mode() == "none" and setup_done():
        return {"ok": True, "via": "none", "user": None}
    key = request.headers.get(API_HEADER) or request.query_params.get("apikey")
    if key and api_key_ok(key):
        return {"ok": True, "via": "apikey", "user": None}
    if setup_required():
        return {"ok": False, "setup_required": True}
    u = session_user(request.cookies.get(COOKIE, ""))
    if u:
        return {"ok": True, "via": "session", "user": u}
    return {"ok": False, "setup_required": False}


def same_origin(request) -> bool:
    """Cross-site form posts can't carry our cookie (SameSite=Lax), but check
    Origin as well when a browser sends one."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return origin.split("://", 1)[-1].rstrip("/") == host


def is_https(request) -> bool:
    return request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").lower() == "https"

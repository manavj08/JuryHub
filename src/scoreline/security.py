"""Authentication, sessions, the role model, CSRF and rate limiting.

Every permission decision in the app goes through the helpers at the bottom
of this file. Templates may hide buttons for convenience, but they never make
a decision the backend has not already made.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from .db import all_, fmt_ts, get_meta, now, one, set_meta

SESSION_COOKIE = "session"
SESSION_DAYS = 14
ROLE_RANK = {"visitor": 0, "participant": 1, "judge": 2, "organizer": 3, "admin": 4}


# --- passwords ----------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(dk)))
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


# --- sessions -----------------------------------------------------------------

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: str, token: str | None = None, label: str | None = None,
                   days: int = SESSION_DAYS) -> str:
    token = token or secrets.token_urlsafe(32)
    expires = fmt_ts(datetime.now(timezone.utc) + timedelta(days=days))
    conn.execute(
        "INSERT OR REPLACE INTO sessions(token_hash, user_id, created_at, expires_at, label) VALUES (?,?,?,?,?)",
        (token_hash(token), user_id, now(), expires, label),
    )
    return token


def destroy_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def session_token(request: Request) -> str | None:
    """Cookie for browsers, Bearer for API clients. Same session table."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def load_user(conn, request: Request):
    token = session_token(request)
    if not token:
        return None
    return one(
        conn,
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash(token), now()),
    )


# --- CSRF ---------------------------------------------------------------------

def app_secret(conn) -> str:
    secret = get_meta(conn, "secret")
    if not secret:
        secret = secrets.token_hex(32)
        set_meta(conn, "secret", secret)
    return secret


def csrf_token(conn, request: Request) -> str:
    token = session_token(request) or "anon"
    return hmac.new(app_secret(conn).encode(), token_hash(token).encode(), "sha256").hexdigest()[:40]


async def check_csrf(conn, request: Request) -> None:
    """HTML forms carry a per-session token. JSON API calls are protected by
    requiring application/json (not sendable cross-site without a CORS
    preflight, which we never grant) plus SameSite=Lax cookies."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json") or request.headers.get("authorization"):
        return
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), csrf_token(conn, request)):
        raise HTTPException(403, "This form expired. Reload the page and try again.")


# --- rate limiting --------------------------------------------------------------

class RateLimiter:
    """Sliding window, in memory. Good for one process; documented as such."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        q = self.hits[key]
        t = time.monotonic()
        while q and t - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        return True


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- roles --------------------------------------------------------------------

def event_roles(conn, user, event_id: str) -> set[str]:
    """All roles `user` holds on `event_id`. Admin implies organizer."""
    if not user:
        return {"visitor"}
    roles = {r["role"] for r in all_(conn,
             "SELECT role FROM event_members WHERE event_id = ? AND user_id = ?", (event_id, user["id"]))}
    if user["is_admin"]:
        roles |= {"admin", "organizer"}
    return roles or {"visitor"}


def is_organizer(conn, user, event_id: str) -> bool:
    return bool(user) and "organizer" in event_roles(conn, user, event_id)


def require_user(user):
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    return user


def require_role(conn, user, event_id: str, *allowed: str):
    """401 if nobody is signed in, 403 if they are but lack every allowed role."""
    require_user(user)
    if not (event_roles(conn, user, event_id) & set(allowed)):
        raise HTTPException(403, "Your role on this event does not allow that.")
    return user


def require_admin(user):
    require_user(user)
    if not user["is_admin"]:
        raise HTTPException(403, "Only administrators can do that.")
    return user


def any_role(conn, user, role: str) -> bool:
    if not user:
        return False
    if user["is_admin"] and role in ("organizer", "admin"):
        return True
    return bool(one(conn, "SELECT 1 FROM event_members WHERE user_id = ? AND role = ? LIMIT 1",
                    (user["id"], role)))

"""Glue between FastAPI and the templates."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .db import connect, parse_ts
from .security import any_role, csrf_token, event_roles, load_user
from . import services

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --- template filters -----------------------------------------------------------

def _dt(value, fmt=None):
    d = parse_ts(value) if isinstance(value, str) else value
    if not d:
        return ""
    if fmt is None:
        return f"{d.day} {d.strftime('%b %Y, %H:%M UTC')}"
    fmt = fmt.replace("%-d", str(d.day)).replace("%#d", str(d.day))
    return d.strftime(fmt)


def _date(value):
    return _dt(value, "%-d %b %Y")


def _initials(title: str) -> str:
    words = [w for w in (title or "?").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _hue(seed: str) -> int:
    return int(hashlib.sha256((seed or "").encode()).hexdigest()[:4], 16) % 360


def _paragraphs(text: str):
    parts = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return Markup("".join(f"<p>{escape(p).replace(chr(10), Markup('<br>'))}</p>" for p in parts))


def _plural(n, one_word, many=None):
    return f"{n} {one_word if n == 1 else (many or one_word + 's')}"


templates.env.filters.update(dt=_dt, date=_date, initials=_initials, hue=_hue, paragraphs=_paragraphs)
templates.env.globals.update(plural=_plural, phases=services.PHASES)


# --- rendering ----------------------------------------------------------------

def flash(resp, message: str):
    resp.set_cookie("flash", quote(message), max_age=30, httponly=True, samesite="lax")
    return resp


def redirect(url: str, message: str | None = None):
    resp = RedirectResponse(url, status_code=303)
    if message:
        flash(resp, message)
    return resp


def render(request: Request, conn, name: str, status_code: int = 200, **ctx):
    user = ctx.pop("user", None) or load_user(conn, request)
    message = request.cookies.get("flash")
    nav = {
        "judge": any_role(conn, user, "judge"),
        "organizer": any_role(conn, user, "organizer"),
    }
    ctx.update(
        request=request, user=user, nav=nav, csrf=csrf_token(conn, request),
        flash=unquote(message) if message else None,
    )
    if "event" in ctx and ctx["event"] is not None:
        ctx.setdefault("roles", event_roles(conn, user, ctx["event"]["id"]))
        ctx.setdefault("phase", services.phase(ctx["event"]))
    resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if message:
        resp.delete_cookie("flash")
    return resp

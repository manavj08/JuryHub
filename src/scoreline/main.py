"""Scoreline: a self-hostable hackathon submission and judging portal."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import api, routes_judge, routes_manage, routes_participant, routes_public
from .db import connect
from .routes_participant import uploads_dir
from .web import render

app = FastAPI(
    title="Scoreline API",
    version="1.0.0",
    description="Every organizer, judge and participant action in Scoreline. Authenticate with the "
                "session cookie or `Authorization: Bearer <session token>`.",
    docs_url=None,       # Swagger UI loads from a CDN; the portal must work offline.
    redoc_url=None,
)

BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(uploads_dir()), check_dir=False), name="uploads")

app.include_router(api.router)
app.include_router(routes_public.router)
app.include_router(routes_participant.router)
app.include_router(routes_judge.router)
app.include_router(routes_manage.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'")
    return resp


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _error(request: Request, status: int, message: str):
    if _is_api(request):
        return JSONResponse({"error": message, "status": status}, status_code=status)
    if status == 401:
        # Pages redirect to sign-in. The API never redirects: a redirect would
        # turn "not allowed" into a 200 login page for any client that follows it.
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
    conn = connect()
    try:
        return render(request, conn, "error.html", status=status, message=message, status_code=status)
    finally:
        conn.close()


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    msg = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    if exc.status_code == 404 and msg == "Not Found":
        msg = "There is nothing at this address."
    return _error(request, exc.status_code, msg)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return _error(request, 422, "Some of the values sent were not valid: " +
                  "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()))


@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request: Request, exc: sqlite3.IntegrityError):
    text = str(exc)
    if "submissions_closed" in text:
        return _error(request, 403, "Submissions for this event are closed.")
    if "results_published" in text:
        return _error(request, 403, "Results are published, so scores are locked.")
    if "UNIQUE" in text and "assignments" in text:
        return _error(request, 409, "That judge is already assigned to that project.")
    if "track" in text or "conflict" in text:
        return _error(request, 409, text.capitalize() + ".")
    return _error(request, 409, "That change conflicts with existing data and was not saved.")


@app.get("/healthz", include_in_schema=False)
def healthz():
    conn = connect()
    try:
        conn.execute("SELECT 1 FROM events LIMIT 1")
    finally:
        conn.close()
    return {"ok": True}

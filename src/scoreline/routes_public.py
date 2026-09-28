"""Pages anyone can see, plus sign in / sign up / invitations."""

from __future__ import annotations

import math
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from . import audit, services
from .db import all_, new_id, now, one, transaction
from .security import (SESSION_COOKIE, check_csrf, check_password, client_ip, create_session,
                       destroy_session, event_roles, hash_password, is_organizer, limiter, load_user,
                       session_token, token_hash)
from .web import get_conn, redirect, render

router = APIRouter()
PAGE_SIZE = 48
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/")
def home(request: Request, conn=Depends(get_conn)):
    events = services.list_events(conn)
    counts = {}
    for e in events:
        counts[e["id"]] = one(conn, "SELECT (SELECT COUNT(*) FROM projects WHERE event_id = ? AND status='submitted' "
                                    "AND duplicate_of IS NULL) projects, (SELECT COUNT(*) FROM teams WHERE event_id = ?) teams, "
                                    "(SELECT COUNT(*) FROM tracks WHERE event_id = ?) tracks",
                              (e["id"],) * 3)
    # Lead with the event that has something to look at, then by stage.
    order = {"open": 0, "judging": 1, "results": 2, "upcoming": 3}
    events = sorted(events, key=lambda e: (counts[e["id"]]["projects"] == 0, order[services.phase(e)],
                                           e["submissions_close_at"]))
    featured = events[0] if events else None
    strip, track_ix = [], {}
    if featured:
        _, strip = services.gallery(conn, event_id=featured["id"], sort="newest", limit=5)
        track_ix = services.track_index(conn, featured["id"])
    return render(request, conn, "home.html", events=events, featured=featured, counts=counts,
                  phase_of=services.phase, strip=strip, track_ix=track_ix)


@router.get("/events/{event_id}")
def event_page(event_id: str, request: Request, conn=Depends(get_conn)):
    ev = services.get_event(conn, event_id)
    user = load_user(conn, request)
    team = services.team_for(conn, event_id, user["id"]) if user else None
    prizes = all_(conn, "SELECT p.*, t.name track_name FROM prizes p LEFT JOIN tracks t ON t.id = p.track_id "
                        "WHERE p.event_id = ? ORDER BY p.position", (event_id,))
    total, recent = services.gallery(conn, event_id=event_id, sort="newest", limit=6)
    track_counts = {r["track_id"]: r["n"] for r in all_(conn,
                    "SELECT track_id, COUNT(*) n FROM projects WHERE event_id = ? AND status='submitted' "
                    "AND duplicate_of IS NULL GROUP BY track_id", (event_id,))}
    return render(request, conn, "event.html", user=user, event=ev, tracks=services.tracks(conn, event_id),
                  prizes=prizes, team=team, recent=recent, total=total, track_counts=track_counts,
                  criteria=services.criteria(conn, event_id), track_ix=services.track_index(conn, event_id))


@router.get("/projects")
def gallery(request: Request, event: str = "", track: str = "", tag: str = "", q: str = "",
            sort: str = "title", page: int = 1, conn=Depends(get_conn)):
    page = max(1, page)
    total, rows = services.gallery(conn, event_id=event or None, track=track or None, tag=tag or None,
                                   q=q or None, sort=sort, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    events = services.list_events(conn)
    ev = one(conn, "SELECT * FROM events WHERE id = ?", (event,)) if event else None
    track_list = services.tracks(conn, event) if event else all_(
        conn, "SELECT t.*, e.name event_name FROM tracks t JOIN events e ON e.id = t.event_id "
              "WHERE e.is_public = 1 ORDER BY e.submissions_close_at DESC, t.position")
    track_ix = {}
    for e in events:
        track_ix.update(services.track_index(conn, e["id"]))
    return render(request, conn, "gallery.html", rows=rows, total=total, page=page,
                  pages=max(1, math.ceil(total / PAGE_SIZE)), events=events, current_event=ev,
                  tracks=track_list, tags=services.popular_tags(conn, event or None), track_ix=track_ix,
                  f={"event": event, "track": track, "tag": tag, "q": q, "sort": sort})


def visible_project(conn, user, project_id):
    p = one(conn, "SELECT p.*, tm.name team_name, tr.name track_name, e.name event_name FROM projects p "
                  "JOIN teams tm ON tm.id = p.team_id JOIN events e ON e.id = p.event_id "
                  "LEFT JOIN tracks tr ON tr.id = p.track_id WHERE p.id = ?", (project_id,))
    if not p:
        raise HTTPException(404, "No project with that id.")
    public = p["status"] == "submitted" and not p["duplicate_of"]
    if not public:
        on_team = user and one(conn, "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                               (p["team_id"], user["id"]))
        if not (on_team or is_organizer(conn, user, p["event_id"])):
            raise HTTPException(404, "No project with that id.")
    return p


@router.get("/projects/{project_id}")
def project_page(project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    p = visible_project(conn, user, project_id)
    ev = services.get_event(conn, p["event_id"])
    members = services.team_members(conn, p["team_id"])
    tags = [r["tag"] for r in all_(conn, "SELECT tag FROM project_tags WHERE project_id = ? ORDER BY tag", (project_id,))]
    images = all_(conn, "SELECT * FROM project_images WHERE project_id = ? ORDER BY position", (project_id,))
    answers = all_(conn, "SELECT q.prompt, a.body FROM answers a JOIN questions q ON q.id = a.question_id "
                         "WHERE a.project_id = ? AND a.body <> '' ORDER BY q.position", (project_id,))
    comments = all_(conn, "SELECT c.*, u.name FROM comments c JOIN users u ON u.id = c.user_id "
                          "WHERE c.project_id = ? AND c.hidden = 0 ORDER BY c.created_at", (project_id,))
    return render(request, conn, "project.html", user=user, event=ev, p=p, members=members, tags=tags,
                  images=images, answers=answers, comments=comments,
                  track_ix=services.track_index(conn, ev["id"]))


@router.post("/projects/{project_id}/comments")
async def add_comment(project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    if not user:
        raise HTTPException(401, "Sign in to comment.")
    await check_csrf(conn, request)
    p = visible_project(conn, user, project_id)
    if not limiter.allow(f"comment:{user['id']}", 5, 60):
        raise HTTPException(429, "You are commenting faster than we allow. Wait a minute and try again.")
    body = str((await request.form()).get("body", "")).strip()[:2000]
    if not body:
        return redirect(f"/projects/{project_id}#comments", "Write something before posting.")
    recent_same = one(conn, "SELECT 1 FROM comments WHERE user_id = ? AND project_id = ? AND body = ?",
                      (user["id"], project_id, body))
    if recent_same:
        return redirect(f"/projects/{project_id}#comments", "You already posted that comment.")
    with transaction(conn):
        cid = new_id("cmt")
        conn.execute("INSERT INTO comments(id, project_id, user_id, body, created_at) VALUES (?,?,?,?,?)",
                     (cid, project_id, user["id"], body, now()))
        audit.record(conn, actor=user, event_id=p["event_id"], action="comment.create", subject=project_id,
                     summary=f"{user['name']} commented on “{p['title']}”", detail={"comment": cid})
    return redirect(f"/projects/{project_id}#comments", "Comment posted.")


@router.post("/comments/{comment_id}/hide")
async def hide_comment(comment_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    c = one(conn, "SELECT c.*, p.event_id, p.title FROM comments c JOIN projects p ON p.id = c.project_id "
                  "WHERE c.id = ?", (comment_id,))
    if not c:
        raise HTTPException(404, "No such comment.")
    if not is_organizer(conn, user, c["event_id"]):
        raise HTTPException(403, "Only organizers can hide comments.")
    await check_csrf(conn, request)
    with transaction(conn):
        conn.execute("UPDATE comments SET hidden = 1 WHERE id = ?", (comment_id,))
        audit.record(conn, actor=user, event_id=c["event_id"], action="comment.hide", subject=c["project_id"],
                     summary=f"{user['name']} hid a comment on “{c['title']}”", detail={"comment": comment_id,
                                                                                       "body": c["body"]})
    return redirect(f"/projects/{c['project_id']}#comments", "Comment hidden.")


@router.get("/events/{event_id}/results")
def public_results(event_id: str, request: Request, conn=Depends(get_conn)):
    ev = services.get_event(conn, event_id)
    user = load_user(conn, request)
    if not ev["results_published_at"]:
        return render(request, conn, "results_pending.html", user=user, event=ev, status_code=404)
    table = services.results(conn, event_id)
    prizes = all_(conn, "SELECT * FROM prizes WHERE event_id = ? ORDER BY position", (event_id,))
    return render(request, conn, "results_public.html", user=user, event=ev, table=table, prizes=prizes,
                  track_ix=services.track_index(conn, event_id), tracks=services.tracks(conn, event_id))


# --- auth -----------------------------------------------------------------------

def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


def _sign_in(conn, user_id: str, next_url: str, message: str):
    token = create_session(conn, user_id)
    resp = redirect(_safe_next(next_url), message)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=14 * 86400)
    return resp


@router.get("/login")
def login_form(request: Request, next: str = "/", conn=Depends(get_conn)):
    return render(request, conn, "login.html", next=next, email="")


@router.post("/login")
async def login(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    next_url = str(form.get("next", "/"))
    if not limiter.allow(f"login:{client_ip(request)}:{email}", 8, 300):
        return render(request, conn, "login.html", next=next_url, email=email, status_code=429,
                      error="Too many attempts for this address. Wait five minutes and try again.")
    user = one(conn, "SELECT * FROM users WHERE email = ?", (email,))
    if not user or not check_password(str(form.get("password", "")), user["password_hash"]):
        return render(request, conn, "login.html", next=next_url, email=email, status_code=401,
                      error="That email and password do not match an account.")
    with transaction(conn):
        resp = _sign_in(conn, user["id"], next_url, f"Signed in as {user['name']}.")
    return resp


@router.post("/logout")
async def logout(request: Request, conn=Depends(get_conn)):
    token = session_token(request)
    if token:
        with transaction(conn):
            destroy_session(conn, token)
    resp = redirect("/", "Signed out.")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/signup")
def signup_form(request: Request, next: str = "/", conn=Depends(get_conn)):
    return render(request, conn, "signup.html", next=next, values={})


@router.post("/signup")
async def signup(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("name", "email", "next")}
    values["email"] = values["email"].lower()
    password = str(form.get("password", ""))
    error = None
    if not limiter.allow(f"signup:{client_ip(request)}", 10, 3600):
        error = "Too many new accounts from this network. Try again later."
    elif not values["name"]:
        error = "Add your name so teammates and judges know who you are."
    elif not EMAIL_RE.match(values["email"]):
        error = "That email address does not look complete."
    elif len(password) < 8:
        error = "Use at least 8 characters for your password."
    elif one(conn, "SELECT 1 FROM users WHERE email = ?", (values["email"],)):
        error = "An account with that email already exists. Sign in instead."
    if error:
        return render(request, conn, "signup.html", next=values["next"], values=values, error=error, status_code=400)
    with transaction(conn):
        uid = new_id("usr")
        conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                     (uid, values["email"], values["name"], hash_password(password), now()))
        audit.record(conn, actor={"id": uid, "name": values["name"]}, action="user.signup",
                     summary=f"{values['name']} created an account", subject=uid)
        resp = _sign_in(conn, uid, values["next"], "Account created. You are signed in.")
    return resp


@router.get("/invite/{token}")
def invite_form(token: str, request: Request, conn=Depends(get_conn)):
    inv = one(conn, "SELECT i.*, u.name, u.email, e.name event_name FROM invites i JOIN users u ON u.id = i.user_id "
                    "LEFT JOIN events e ON e.id = i.event_id WHERE i.token_hash = ?", (token_hash(token),))
    if not inv or inv["used_at"]:
        raise HTTPException(404, "This invitation link is not valid or was already used.")
    return render(request, conn, "invite.html", inv=inv, token=token)


@router.post("/invite/{token}")
async def invite_accept(token: str, request: Request, conn=Depends(get_conn)):
    inv = one(conn, "SELECT i.*, u.name FROM invites i JOIN users u ON u.id = i.user_id WHERE i.token_hash = ?",
              (token_hash(token),))
    if not inv or inv["used_at"]:
        raise HTTPException(404, "This invitation link is not valid or was already used.")
    form = await request.form()
    password = str(form.get("password", ""))
    if len(password) < 8:
        full = one(conn, "SELECT i.*, u.name, u.email, e.name event_name FROM invites i JOIN users u ON u.id = i.user_id "
                         "LEFT JOIN events e ON e.id = i.event_id WHERE i.token_hash = ?", (token_hash(token),))
        return render(request, conn, "invite.html", inv=full, token=token, status_code=400,
                      error="Use at least 8 characters for your password.")
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), inv["user_id"]))
        conn.execute("UPDATE invites SET used_at = ? WHERE token_hash = ?", (now(), token_hash(token)))
        audit.record(conn, actor={"id": inv["user_id"], "name": inv["name"]}, event_id=inv["event_id"],
                     action="invite.accept", summary=f"{inv['name']} accepted their invitation", subject=inv["user_id"])
        resp = _sign_in(conn, inv["user_id"], "/judge" if inv["purpose"] == "judge" else "/",
                        "Password set. You are signed in.")
    return resp

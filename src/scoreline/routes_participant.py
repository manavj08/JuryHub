"""Joining an event, forming a team by invite link, and the submission form."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from . import audit, services
from .db import all_, db_path, new_id, now, one, transaction
from .importer import invite_code
from .security import check_csrf, event_roles, load_user, require_user
from .web import get_conn, redirect, render

router = APIRouter()

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_GALLERY = 6


def uploads_dir() -> Path:
    d = Path(db_path()).parent / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def save_image(upload: UploadFile) -> str | None:
    if not upload or not upload.filename:
        return None
    ext = IMAGE_TYPES.get(upload.content_type or "")
    if not ext:
        raise HTTPException(400, "Images must be PNG, JPEG, WebP or GIF.")
    data = await upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Each image must be under 3 MB.")
    # Magic-byte check, so a renamed file is not trusted on its content type alone.
    if not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8") or data[8:12] == b"WEBP"
            or data.startswith(b"GIF8")):
        raise HTTPException(400, "That file is not a readable image.")
    name = secrets.token_hex(12) + ext
    (uploads_dir() / name).write_bytes(data)
    return f"/uploads/{name}"


def ensure_participant(conn, user, event_id):
    roles = event_roles(conn, user, event_id)
    if "judge" in roles:
        raise HTTPException(403, "You are judging this event, so you cannot also compete in it.")
    if "participant" not in roles:
        conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (event_id, user["id"], "participant", now()))


@router.post("/events/{event_id}/join")
async def join_event(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    await check_csrf(conn, request)
    ev = services.get_event(conn, event_id)
    with transaction(conn):
        ensure_participant(conn, user, event_id)
        audit.record(conn, actor=user, event_id=event_id, action="participant.join",
                     summary=f"{user['name']} registered for {ev['name']}", subject=user["id"])
    return redirect(f"/events/{event_id}/team", f"You are registered for {ev['name']}.")


@router.get("/events/{event_id}/team")
def team_page(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    ev = services.get_event(conn, event_id)
    team = services.team_for(conn, event_id, user["id"])
    members = services.team_members(conn, team["id"]) if team else []
    project = services.project_for_team(conn, team["id"]) if team else None
    return render(request, conn, "team.html", user=user, event=ev, team=team, members=members, project=project)


@router.post("/events/{event_id}/team")
async def create_team(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    await check_csrf(conn, request)
    ev = services.get_event(conn, event_id)
    if services.phase(ev) in ("judging", "results"):
        raise HTTPException(403, "Submissions are closed, so new teams cannot be formed.")
    name = str((await request.form()).get("name", "")).strip()[:80]
    if not name:
        return redirect(f"/events/{event_id}/team", "Give your team a name.")
    with transaction(conn):
        ensure_participant(conn, user, event_id)
        if services.team_for(conn, event_id, user["id"]):
            return redirect(f"/events/{event_id}/team", "You are already on a team for this event.")
        tid = new_id("tm")
        conn.execute("INSERT INTO teams(id, event_id, name, invite_code, created_by, created_at) VALUES (?,?,?,?,?,?)",
                     (tid, event_id, name, invite_code(), user["id"], now()))
        conn.execute("INSERT INTO team_members(event_id, team_id, user_id, joined_at) VALUES (?,?,?,?)",
                     (event_id, tid, user["id"], now()))
        audit.record(conn, actor=user, event_id=event_id, action="team.create", subject=tid,
                     summary=f"{user['name']} created team {name}")
    return redirect(f"/events/{event_id}/team", f"Team {name} created. Share the invite link with your teammates.")


@router.post("/events/{event_id}/team/rotate")
async def rotate_invite(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    await check_csrf(conn, request)
    team = services.team_for(conn, event_id, user["id"])
    if not team:
        raise HTTPException(403, "You are not on a team for this event.")
    with transaction(conn):
        conn.execute("UPDATE teams SET invite_code = ? WHERE id = ?", (invite_code(), team["id"]))
        audit.record(conn, actor=user, event_id=event_id, action="team.rotate_invite", subject=team["id"],
                     summary=f"{user['name']} replaced the invite link for {team['name']}")
    return redirect(f"/events/{event_id}/team", "New invite link created. The old one no longer works.")


@router.post("/events/{event_id}/team/leave")
async def leave_team(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    await check_csrf(conn, request)
    ev = services.get_event(conn, event_id)
    team = services.team_for(conn, event_id, user["id"])
    if not team:
        return redirect(f"/events/{event_id}/team")
    if services.phase(ev) != "open" and services.phase(ev) != "upcoming":
        raise HTTPException(403, "Teams are locked once submissions close.")
    with transaction(conn):
        conn.execute("DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team["id"], user["id"]))
        audit.record(conn, actor=user, event_id=event_id, action="team.leave", subject=team["id"],
                     summary=f"{user['name']} left {team['name']}")
    return redirect(f"/events/{event_id}/team", f"You left {team['name']}.")


@router.get("/t/{code}")
def join_team_page(code: str, request: Request, conn=Depends(get_conn)):
    team = one(conn, "SELECT * FROM teams WHERE invite_code = ?", (code,))
    if not team:
        raise HTTPException(404, "This invite link is not valid. Ask your team for a fresh one.")
    ev = services.get_event(conn, team["event_id"])
    user = load_user(conn, request)
    members = services.team_members(conn, team["id"])
    current = services.team_for(conn, ev["id"], user["id"]) if user else None
    return render(request, conn, "team_join.html", user=user, event=ev, team=team, members=members,
                  current=current, code=code)


@router.post("/t/{code}")
async def join_team(code: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    await check_csrf(conn, request)
    team = one(conn, "SELECT * FROM teams WHERE invite_code = ?", (code,))
    if not team:
        raise HTTPException(404, "This invite link is not valid. Ask your team for a fresh one.")
    ev = services.get_event(conn, team["event_id"])
    if services.phase(ev) not in ("open", "upcoming"):
        raise HTTPException(403, "Teams are locked once submissions close.")
    with transaction(conn):
        ensure_participant(conn, user, ev["id"])
        if services.team_for(conn, ev["id"], user["id"]):
            return redirect(f"/events/{ev['id']}/team", "Leave your current team before joining another.")
        n = one(conn, "SELECT COUNT(*) n FROM team_members WHERE team_id = ?", (team["id"],))["n"]
        if n >= ev["max_team_size"]:
            return redirect(f"/t/{code}", f"{team['name']} is full ({ev['max_team_size']} people).")
        conn.execute("INSERT INTO team_members(event_id, team_id, user_id, joined_at) VALUES (?,?,?,?)",
                     (ev["id"], team["id"], user["id"], now()))
        audit.record(conn, actor=user, event_id=ev["id"], action="team.join", subject=team["id"],
                     summary=f"{user['name']} joined {team['name']} by invite link")
    return redirect(f"/events/{ev['id']}/team", f"You joined {team['name']}.")


# --- submission -------------------------------------------------------------------

FIELDS = ("title", "tagline", "description", "demo_video_url", "repo_url", "live_url")


def _require_team(conn, user, event_id):
    team = services.team_for(conn, event_id, user["id"])
    if not team:
        raise HTTPException(403, "Join or create a team before submitting a project.")
    return team


def _url_ok(v: str) -> bool:
    return not v or v.startswith("https://") or v.startswith("http://")


def validate(values: dict, submitting: bool, questions, answers) -> list[str]:
    errors = []
    if not values.get("title"):
        errors.append("Give the project a name.")
    for k, label in (("demo_video_url", "Demo video"), ("repo_url", "Repository"), ("live_url", "Live link")):
        if not _url_ok(values.get(k, "")):
            errors.append(f"{label} must be a full link starting with https://")
    if submitting:
        if not values.get("tagline"):
            errors.append("Add a one-line tagline; it is what people see in the gallery.")
        if not values.get("repo_url"):
            errors.append("Add the repository link. Judges need the code.")
        if not values.get("track_id"):
            errors.append("Choose a track.")
        for q in questions:
            if q["required"] and not answers.get(q["id"], "").strip():
                errors.append(f"Answer the required question: {q['prompt']}")
    return errors


def upsert_project(conn, user, ev, team, values: dict, tags: list[str], answers: dict, action: str,
                   thumbnail: str | None = None, images: list[str] | None = None):
    """Shared by the form and the JSON API. Raises on closed events.

    action: 'draft' saves, 'submit' saves and submits, 'unsubmit' moves back
    to draft. Returns (project_id, errors).
    """
    services.assert_submissions_open(ev)
    questions = all_(conn, "SELECT * FROM questions WHERE event_id = ? ORDER BY position", (ev["id"],))
    tracks = {t["id"] for t in services.tracks(conn, ev["id"])}
    if values.get("track_id") and values["track_id"] not in tracks:
        return None, ["That track does not belong to this event."]
    errors = validate(values, action == "submit", questions, answers)
    if errors:
        return None, errors
    existing = services.project_for_team(conn, team["id"])
    t = now()
    if existing:
        pid = existing["id"]
        conn.execute("UPDATE projects SET title=?, tagline=?, description=?, demo_video_url=?, repo_url=?, "
                     "live_url=?, track_id=?, updated_at=? WHERE id=?",
                     (*[values.get(k, "") for k in FIELDS], values.get("track_id") or None, t, pid))
    else:
        pid = new_id("prj")
        conn.execute("INSERT INTO projects(id, event_id, team_id, track_id, title, tagline, description, "
                     "demo_video_url, repo_url, live_url, status, source, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,'draft','portal',?,?)",
                     (pid, ev["id"], team["id"], values.get("track_id") or None,
                      *[values.get(k, "") for k in FIELDS], t, t))
    if thumbnail:
        conn.execute("UPDATE projects SET thumbnail = ? WHERE id = ?", (thumbnail, pid))
    if images:
        start = one(conn, "SELECT COUNT(*) n FROM project_images WHERE project_id = ?", (pid,))["n"]
        for i, path in enumerate(images[: max(0, MAX_GALLERY - start)]):
            conn.execute("INSERT INTO project_images(id, project_id, path, position) VALUES (?,?,?,?)",
                         (new_id("img"), pid, path, start + i))
    conn.execute("DELETE FROM project_tags WHERE project_id = ?", (pid,))
    for tag in tags[:12]:
        conn.execute("INSERT OR IGNORE INTO project_tags(project_id, tag) VALUES (?,?)", (pid, tag))
    for q in questions:
        if q["id"] in answers:
            conn.execute("INSERT INTO answers(project_id, question_id, body) VALUES (?,?,?) "
                         "ON CONFLICT(project_id, question_id) DO UPDATE SET body = excluded.body",
                         (pid, q["id"], answers[q["id"]][:4000]))
    if action == "submit":
        conn.execute("UPDATE projects SET status='submitted', submitted_at=? WHERE id=?", (t, pid))
        verb, summary = "project.submit", f"{user['name']} submitted “{values['title']}”"
    elif action == "unsubmit":
        conn.execute("UPDATE projects SET status='draft', submitted_at=NULL WHERE id=?", (pid,))
        verb, summary = "project.unsubmit", f"{user['name']} moved “{values['title']}” back to draft"
    else:
        verb, summary = "project.save", f"{user['name']} saved “{values['title']}”"
    audit.record(conn, actor=user, event_id=ev["id"], action=verb, subject=pid, summary=summary,
                 detail={"team": team["id"], "fields": sorted(k for k in values if values.get(k))})
    return pid, []


def parse_tags(raw: str) -> list[str]:
    out = []
    for t in (raw or "").replace(";", ",").split(","):
        t = t.strip().lower()[:30]
        if t and t not in out:
            out.append(t)
    return out


@router.get("/events/{event_id}/project")
def submission_form(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    ev = services.get_event(conn, event_id)
    team = services.team_for(conn, event_id, user["id"])
    if not team:
        return redirect(f"/events/{event_id}/team", "Create or join a team first.")
    p = services.project_for_team(conn, team["id"])
    tags = ", ".join(r["tag"] for r in all_(conn, "SELECT tag FROM project_tags WHERE project_id = ? ORDER BY tag",
                                            (p["id"],))) if p else ""
    answers = {r["question_id"]: r["body"] for r in all_(conn, "SELECT * FROM answers WHERE project_id = ?",
                                                         (p["id"],))} if p else {}
    images = all_(conn, "SELECT * FROM project_images WHERE project_id = ? ORDER BY position", (p["id"],)) if p else []
    return render(request, conn, "submission.html", user=user, event=ev, team=team, p=p, tags=tags,
                  answers=answers, images=images, tracks=services.tracks(conn, event_id),
                  questions=all_(conn, "SELECT * FROM questions WHERE event_id = ? ORDER BY position", (event_id,)),
                  errors=[], values=dict(p) if p else {})


@router.post("/events/{event_id}/project")
async def submission_save(event_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    ev = services.get_event(conn, event_id)
    # Deadline first: a late request is refused for being late, whatever else is wrong with it.
    services.assert_submissions_open(ev)
    await check_csrf(conn, request)
    team = _require_team(conn, user, event_id)
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in FIELDS}
    values["track_id"] = str(form.get("track_id", "")).strip()
    values["title"] = values["title"][:120]
    values["tagline"] = values["tagline"][:160]
    tags = parse_tags(str(form.get("tags", "")))
    answers = {k[2:]: str(v).strip() for k, v in form.items() if k.startswith("q_")}
    action = str(form.get("action", "draft"))
    thumb = await save_image(form.get("thumbnail")) if hasattr(form.get("thumbnail"), "filename") else None
    images = []
    for f in form.getlist("images"):
        if hasattr(f, "filename"):
            path = await save_image(f)
            if path:
                images.append(path)
    with transaction(conn):
        pid, errors = upsert_project(conn, user, ev, team, values, tags, answers, action, thumb, images)
    if errors:
        p = services.project_for_team(conn, team["id"])
        return render(request, conn, "submission.html", user=user, event=ev, team=team, p=p,
                      tags=", ".join(tags), answers=answers, tracks=services.tracks(conn, event_id),
                      images=all_(conn, "SELECT * FROM project_images WHERE project_id = ? ORDER BY position",
                                  (p["id"],)) if p else [],
                      questions=all_(conn, "SELECT * FROM questions WHERE event_id = ? ORDER BY position", (event_id,)),
                      errors=errors, values=values, status_code=400)
    msg = {"submit": "Project submitted. You can keep editing until the deadline.",
           "unsubmit": "Moved back to draft. It is hidden from the gallery until you submit again."}.get(
        action, "Draft saved.")
    return redirect(f"/events/{event_id}/project", msg)


@router.post("/events/{event_id}/project/images/{image_id}/delete")
async def delete_image(event_id: str, image_id: str, request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    ev = services.get_event(conn, event_id)
    services.assert_submissions_open(ev)
    await check_csrf(conn, request)
    team = _require_team(conn, user, event_id)
    img = one(conn, "SELECT i.* FROM project_images i JOIN projects p ON p.id = i.project_id "
                    "WHERE i.id = ? AND p.team_id = ?", (image_id, team["id"]))
    if not img:
        raise HTTPException(404, "No such image on your project.")
    with transaction(conn):
        conn.execute("DELETE FROM project_images WHERE id = ?", (image_id,))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), img["project_id"]))
    return redirect(f"/events/{event_id}/project", "Image removed.")

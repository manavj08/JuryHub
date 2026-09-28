"""JSON API. Same permission helpers as the HTML pages, same session table.

Authenticate with the session cookie or `Authorization: Bearer <token>`.
Errors are always JSON: {"error": "...", "status": 403}.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import audit, services
from .db import all_, one, transaction
from .importer import export_event
from .routes_judge import assignment_or_403, save_review
from .routes_participant import parse_tags, upsert_project
from .security import event_roles, is_organizer, load_user, require_role, require_user
from .web import get_conn

router = APIRouter(prefix="/api", tags=["api"])


def _project_json(p) -> dict:
    return {k: p[k] for k in ("id", "event_id", "team_id", "track_id", "title", "tagline", "description",
                              "demo_video_url", "repo_url", "live_url", "status", "submitted_at")
            if k in p.keys()} | ({"team_name": p["team_name"]} if "team_name" in p.keys() else {})


@router.get("/me")
def me(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    roles = all_(conn, "SELECT event_id, role FROM event_members WHERE user_id = ?", (user["id"],))
    return {"id": user["id"], "name": user["name"], "email": user["email"], "is_admin": bool(user["is_admin"]),
            "roles": [dict(r) for r in roles]}


@router.get("/events")
def events(conn=Depends(get_conn)):
    return [{"id": e["id"], "name": e["name"], "phase": services.phase(e),
             "submissions_open_at": e["submissions_open_at"], "submissions_close_at": e["submissions_close_at"],
             "results_published_at": e["results_published_at"]} for e in services.list_events(conn)]


@router.get("/events/{event_id}")
def event(event_id: str, conn=Depends(get_conn)):
    e = services.get_event(conn, event_id)
    return {"id": e["id"], "name": e["name"], "tagline": e["tagline"], "phase": services.phase(e),
            "submissions_open_at": e["submissions_open_at"], "submissions_close_at": e["submissions_close_at"],
            "tracks": [dict(t) for t in services.tracks(conn, event_id)],
            "criteria": [{k: c[k] for k in ("key", "name", "weight", "min_score", "max_score")}
                         for c in services.criteria(conn, event_id)],
            "prizes": [dict(p) for p in all_(conn, "SELECT id, name, reward, track_id FROM prizes WHERE event_id = ? "
                                                   "ORDER BY position", (event_id,))]}


@router.get("/events/{event_id}/projects")
def event_projects(event_id: str, track: str = "", q: str = "", conn=Depends(get_conn)):
    services.get_event(conn, event_id)
    total, rows = services.gallery(conn, event_id=event_id, track=track or None, q=q or None, limit=1000)
    return {"total": total, "projects": [_project_json(r) for r in rows]}


class SubmissionIn(BaseModel):
    title: str = Field(max_length=120)
    tagline: str = Field("", max_length=160)
    summary: str = Field("", max_length=160, description="Alias for tagline, as in the fixtures")
    description: str = ""
    track_id: str = ""
    repo_url: str = ""
    demo_video_url: str = ""
    live_url: str = ""
    tags: list[str] = []
    answers: dict[str, str] = {}
    submit: bool = False


@router.post("/events/{event_id}/submissions")
async def submit_project(event_id: str, request: Request, conn=Depends(get_conn)):
    """Create or update your team's project. `submit: true` submits it.

    Order of checks is deliberate: event exists, you are signed in, the
    deadline has not passed, then everything else. A late request is refused
    for being late.
    """
    ev = services.get_event(conn, event_id)
    user = require_user(load_user(conn, request))
    services.assert_submissions_open(ev)
    if "judge" in event_roles(conn, user, event_id):
        raise HTTPException(403, "Judges cannot submit to an event they judge.")
    try:
        body = SubmissionIn.model_validate(await request.json())
    except Exception as e:  # noqa: BLE001 - surface validation as a 422
        raise HTTPException(422, f"Invalid submission body: {e}")
    team = services.team_for(conn, event_id, user["id"])
    if not team:
        raise HTTPException(403, "Join or create a team before submitting a project.")
    values = body.model_dump()
    values["tagline"] = values["tagline"] or values.pop("summary", "")
    with transaction(conn):
        pid, errors = upsert_project(conn, user, ev, team, values, parse_tags(",".join(body.tags)), body.answers,
                                     "submit" if body.submit else "draft")
    if errors:
        raise HTTPException(422, " ".join(errors))
    p = one(conn, "SELECT * FROM projects WHERE id = ?", (pid,))
    return JSONResponse(_project_json(p), status_code=201)


# --- judging ------------------------------------------------------------------

def _review_rows(conn, judge_id: str, event_id: str | None):
    sql = ("SELECT r.id, r.event_id, r.project_id, p.title, r.status, r.comment, r.submitted_at, r.updated_at "
           "FROM reviews r JOIN projects p ON p.id = r.project_id WHERE r.judge_id = ?")
    params = [judge_id]
    if event_id:
        sql += " AND r.event_id = ?"
        params.append(event_id)
    out = []
    for r in all_(conn, sql + " ORDER BY r.event_id, p.title", params):
        crit = services.criteria(conn, r["event_id"])
        vals = services.review_values(conn, r["id"])
        from .scoring import weighted_score
        ws = weighted_score(vals, crit)
        out.append(dict(r) | {"scores": vals, "weighted": round(ws, 4) if ws is not None else None})
    return out


@router.get("/judge/scores")
def judge_scores(request: Request, judge: str = "", event: str = "", conn=Depends(get_conn)):
    """Your own reviews. Passing ?judge=<someone else> is refused with 403
    unless you organize every event that judge's reviews belong to."""
    user = require_user(load_user(conn, request))
    target = judge or user["id"]
    if target == user["id"]:
        if not one(conn, "SELECT 1 FROM event_members WHERE user_id = ? AND role = 'judge' LIMIT 1", (user["id"],)):
            raise HTTPException(403, "Only judges have scores.")
        return {"judge": user["id"], "reviews": _review_rows(conn, user["id"], event or None)}
    # Someone else's scores: organizers only, and only for events they organize.
    target_row = one(conn, "SELECT id FROM users WHERE id = ?", (target,))
    if not target_row:
        raise HTTPException(403, "You can only read your own scores.")
    events_of_target = [r["event_id"] for r in all_(conn, "SELECT DISTINCT event_id FROM event_members "
                                                          "WHERE user_id = ? AND role = 'judge'", (target,))]
    if event:
        events_of_target = [e for e in events_of_target if e == event]
    if not events_of_target or not all(is_organizer(conn, user, e) for e in events_of_target):
        raise HTTPException(403, "You can only read your own scores.")
    with transaction(conn):
        audit.record(conn, actor=user, event_id=event or events_of_target[0], action="scores.read", subject=target,
                     summary=f"{user['name']} read the scores of judge "
                             f"{one(conn, 'SELECT name FROM users WHERE id = ?', (target,))['name']}")
    return {"judge": target, "reviews": _review_rows(conn, target, event or None)}


class ReviewIn(BaseModel):
    scores: dict[str, int]
    comment: str = ""
    submit: bool = False


@router.put("/judge/reviews/{event_id}/{project_id}")
async def put_review(event_id: str, project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    assignment_or_403(conn, user, event_id, project_id)
    ev = services.get_event(conn, event_id)
    try:
        body = ReviewIn.model_validate(await request.json())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Invalid review body: {e}")
    with transaction(conn):
        errors = save_review(conn, user, ev, project_id, body.scores, body.comment, body.submit)
    if errors:
        raise HTTPException(422, " ".join(errors))
    return {"ok": True, "reviews": _review_rows(conn, user["id"], event_id)}


@router.get("/judge/assignments")
def my_assignments(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    rows = all_(conn, "SELECT a.event_id, a.project_id, p.title, r.status FROM assignments a "
                      "JOIN projects p ON p.id = a.project_id LEFT JOIN reviews r ON r.judge_id = a.judge_id "
                      "AND r.project_id = a.project_id WHERE a.judge_id = ? ORDER BY a.event_id, p.title", (user["id"],))
    return [dict(r) for r in rows]


# --- organizer ----------------------------------------------------------------

@router.get("/events/{event_id}/progress")
def progress(event_id: str, request: Request, conn=Depends(get_conn)):
    require_role(conn, load_user(conn, request), event_id, "organizer")
    prog = services.progress(conn, event_id)
    return {"totals": dict(prog["totals"]), "judges": [dict(j) for j in prog["judges"]],
            "tracks": [dict(t) for t in prog["tracks"]]}


@router.post("/events/{event_id}/assignments/auto")
def api_auto_assign(event_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    require_role(conn, user, event_id, "organizer")
    if not request.headers.get("content-type", "").startswith("application/json") and \
            not request.headers.get("authorization"):
        raise HTTPException(415, "Send Content-Type: application/json.")
    with transaction(conn):
        r = services.auto_assign(conn, event_id, user)
    return {"created": len(r["created"]), "short": r["short"], "target": r["target"]}


def _results_json(conn, event_id, full: bool):
    t = services.results(conn, event_id)
    rows = t["project_rows"]
    projects = []
    for r in t["projects"]:
        item = {"rank": r.rank, "project": r.project_id, "title": rows[r.project_id]["title"],
                "track": rows[r.project_id]["track_name"], "track_rank": t["track_rank"][r.project_id],
                "score": round(r.normalized, 4), "reviews": r.n}
        if full:
            item |= {"raw_mean": round(r.raw_mean, 4), "raw_rank": r.raw_rank, "stderr": round(r.stderr, 4)}
        projects.append(item)
    out = {"method": "additive judge-bias model with ridge shrinkage; see JUDGING.md", "projects": projects}
    if full:
        out |= {"mu": round(t["mu"], 4), "residual_sd": round(t["residual_sd"], 4), "shrinkage": t["shrinkage"],
                "judges": [{"judge": j.judge_id, "reviews": j.n, "mean_given": round(j.mean_given, 4),
                            "spread": round(j.spread, 4), "bias": round(j.bias, 4), "flags": j.flags}
                           for j in t["judges"]]}
    return out


@router.get("/events/{event_id}/results")
def results(event_id: str, request: Request, conn=Depends(get_conn)):
    """Organizers always; everyone else only after publication, and without
    per-judge detail."""
    ev = services.get_event(conn, event_id)
    user = load_user(conn, request)
    if is_organizer(conn, user, event_id):
        return _results_json(conn, event_id, full=True)
    if not ev["results_published_at"]:
        raise HTTPException(403 if user else 401, "Results are not published yet.")
    return _results_json(conn, event_id, full=False)


# --- exports ------------------------------------------------------------------

def _csv(filename: str, header: list[str], rows) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


EXPORTS = ("reviews", "results", "projects", "teams", "judges", "assignments", "audit")


@router.get("/events/{event_id}/export/{kind}.csv")
def export_csv(event_id: str, kind: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    require_role(conn, user, event_id, "organizer")
    if kind not in EXPORTS:
        raise HTTPException(404, f"Unknown export. Try one of: {', '.join(EXPORTS)}.")
    with transaction(conn):
        audit.record(conn, actor=user, event_id=event_id, action="export.csv", subject=kind,
                     summary=f"{user['name']} exported {kind}.csv")
    name = f"{event_id}-{kind}.csv"
    crit = services.criteria(conn, event_id)
    if kind == "reviews":
        from .scoring import weighted_score
        rows = []
        for r in all_(conn, "SELECT r.*, u.name judge_name, p.title FROM reviews r JOIN users u ON u.id = r.judge_id "
                            "JOIN projects p ON p.id = r.project_id WHERE r.event_id = ? ORDER BY p.id, u.id", (event_id,)):
            vals = services.review_values(conn, r["id"])
            ws = weighted_score(vals, crit)
            rows.append([r["project_id"], r["title"], r["judge_id"], r["judge_name"], r["status"],
                         *[vals.get(c["key"], "") for c in crit], f"{ws:.4f}" if ws is not None else "",
                         r["comment"], r["submitted_at"] or ""])
        return _csv(name, ["project_id", "project_title", "judge_id", "judge_name", "status",
                           *[c["key"] for c in crit], "weighted_score", "comment", "submitted_at"], rows)
    if kind == "results":
        t = services.results(conn, event_id)
        pr = t["project_rows"]
        return _csv(name, ["rank", "project_id", "title", "team", "track", "track_rank", "reviews", "raw_mean",
                           "raw_rank", "normalized_score", "stderr", "rank_change"],
                    [[r.rank, r.project_id, pr[r.project_id]["title"], pr[r.project_id]["team_name"],
                      pr[r.project_id]["track_name"], t["track_rank"][r.project_id], r.n, f"{r.raw_mean:.4f}",
                      r.raw_rank, f"{r.normalized:.4f}", f"{r.stderr:.4f}", r.movement] for r in t["projects"]])
    if kind == "projects":
        return _csv(name, ["id", "team_id", "team", "track", "title", "tagline", "repo_url", "demo_video_url",
                           "live_url", "status", "duplicate_of", "submitted_at"],
                    [[p["id"], p["team_id"], p["team_name"], p["track_name"], p["title"], p["tagline"], p["repo_url"],
                      p["demo_video_url"], p["live_url"], p["status"], p["duplicate_of"] or "", p["submitted_at"] or ""]
                     for p in all_(conn, "SELECT p.*, tm.name team_name, tr.name track_name FROM projects p "
                                         "JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks tr ON tr.id = p.track_id "
                                         "WHERE p.event_id = ? ORDER BY p.id", (event_id,))])
    if kind == "teams":
        return _csv(name, ["team_id", "team", "member_email", "member_name", "joined_at"],
                    [[r["tid"], r["tname"], r["email"], r["name"], r["joined_at"]] for r in
                     all_(conn, "SELECT t.id tid, t.name tname, u.email, u.name, m.joined_at FROM teams t "
                                "JOIN team_members m ON m.team_id = t.id JOIN users u ON u.id = m.user_id "
                                "WHERE t.event_id = ? ORDER BY t.id, m.joined_at", (event_id,))])
    if kind == "judges":
        prog = services.progress(conn, event_id)
        jt = services.judge_tracks_map(conn, event_id)
        return _csv(name, ["judge_id", "name", "email", "tracks", "assigned", "submitted", "drafts", "last_active"],
                    [[j["id"], j["name"], j["email"], " ".join(sorted(jt.get(j["id"], []))), j["assigned"], j["done"],
                      j["drafts"], j["last_active"] or ""] for j in prog["judges"]])
    if kind == "assignments":
        return _csv(name, ["judge_id", "project_id", "source", "created_at", "review_status"],
                    [[a["judge_id"], a["project_id"], a["source"], a["created_at"], a["status"] or "not started"] for a in
                     all_(conn, "SELECT a.*, r.status FROM assignments a LEFT JOIN reviews r ON r.judge_id = a.judge_id "
                                "AND r.project_id = a.project_id WHERE a.event_id = ? ORDER BY a.project_id, a.judge_id",
                          (event_id,))])
    return _csv(name, ["seq", "at", "actor", "action", "subject", "summary", "hash"],
                [[a["seq"], a["at"], a["actor_name"], a["action"], a["subject"], a["summary"], a["hash"]] for a in
                 all_(conn, "SELECT * FROM audit_log WHERE event_id = ? ORDER BY seq", (event_id,))])


@router.get("/events/{event_id}/export.json")
def export_json(event_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    require_role(conn, user, event_id, "organizer")
    with transaction(conn):
        audit.record(conn, actor=user, event_id=event_id, action="export.json", subject=event_id,
                     summary=f"{user['name']} exported the full event as JSON")
    return JSONResponse(export_event(conn, event_id),
                        headers={"Content-Disposition": f'attachment; filename="{event_id}.json"'})


@router.get("/events/{event_id}/audit")
def audit_json(event_id: str, request: Request, limit: int = 200, conn=Depends(get_conn)):
    require_role(conn, load_user(conn, request), event_id, "organizer")
    rows = all_(conn, "SELECT seq, at, actor_name, action, subject, summary, hash FROM audit_log WHERE event_id = ? "
                      "ORDER BY seq DESC LIMIT ?", (event_id, min(limit, 1000)))
    return {"chain": audit.verify_chain(conn), "entries": [dict(r) for r in rows]}

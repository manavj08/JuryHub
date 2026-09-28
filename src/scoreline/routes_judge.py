"""The judge console. A judge only ever touches their own assignments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from . import audit, services
from .db import all_, now, one, transaction
from .scoring import weighted_score
from .security import check_csrf, event_roles, load_user, require_role, require_user
from .web import get_conn, redirect, render

router = APIRouter()


def assignment_or_403(conn, user, event_id, project_id):
    """The isolation rule in one place: no assignment row, no access."""
    require_role(conn, user, event_id, "judge")
    a = one(conn, "SELECT * FROM assignments WHERE event_id = ? AND judge_id = ? AND project_id = ?",
            (event_id, user["id"], project_id))
    if not a:
        raise HTTPException(403, "This project is not assigned to you.")
    return a


def save_review(conn, user, ev, project_id: str, values: dict, comment: str, submit: bool):
    """Shared by the form and the API. Returns list of errors (empty on success)."""
    if ev["results_published_at"]:
        raise HTTPException(403, "Results are published, so reviews are locked.")
    crit = services.criteria(conn, ev["id"])
    review = one(conn, "SELECT * FROM reviews WHERE judge_id = ? AND project_id = ?", (user["id"], project_id))
    if review and review["status"] == "submitted":
        raise HTTPException(409, "You already submitted this review. Ask an organizer to reopen it.")
    clean, errors = {}, []
    for c in crit:
        raw = values.get(c["key"])
        if raw in (None, ""):
            continue
        try:
            v = int(raw)
        except (TypeError, ValueError):
            errors.append(f"{c['name']} must be a whole number.")
            continue
        if not c["min_score"] <= v <= c["max_score"]:
            errors.append(f"{c['name']} must be between {c['min_score']} and {c['max_score']}.")
            continue
        clean[c["key"]] = v
    if submit:
        missing = [c["name"] for c in crit if c["key"] not in clean]
        if missing:
            errors.append("Score every criterion before submitting: " + ", ".join(missing) + ".")
    if errors:
        return errors
    t = now()
    rid = review["id"] if review else f"rev_{user['id']}_{project_id}"
    if review:
        conn.execute("UPDATE reviews SET comment = ?, updated_at = ? WHERE id = ?", (comment[:4000], t, rid))
    else:
        conn.execute("INSERT INTO reviews(id, event_id, judge_id, project_id, status, comment, created_at, updated_at) "
                     "VALUES (?,?,?,?, 'draft', ?,?,?)", (rid, ev["id"], user["id"], project_id, comment[:4000], t, t))
    by_key = {c["key"]: c["id"] for c in crit}
    for k, v in clean.items():
        conn.execute("INSERT INTO review_scores(review_id, criterion_id, value) VALUES (?,?,?) "
                     "ON CONFLICT(review_id, criterion_id) DO UPDATE SET value = excluded.value", (rid, by_key[k], v))
    title = one(conn, "SELECT title FROM projects WHERE id = ?", (project_id,))["title"]
    if submit:
        conn.execute("UPDATE reviews SET status = 'submitted', submitted_at = ? WHERE id = ?", (t, rid))
        ws = weighted_score(clean, crit)
        audit.record(conn, actor=user, event_id=ev["id"], action="review.submit", subject=project_id,
                     summary=f"{user['name']} submitted a review of “{title}”",
                     detail={"review": rid, "scores": clean, "weighted": round(ws, 4) if ws else None})
    else:
        audit.record(conn, actor=user, event_id=ev["id"], action="review.save", subject=project_id,
                     summary=f"{user['name']} saved a draft review of “{title}”", detail={"review": rid})
    return []


@router.get("/judge")
def queue(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    events = all_(conn, "SELECT e.* FROM events e JOIN event_members em ON em.event_id = e.id "
                        "WHERE em.user_id = ? AND em.role = 'judge' ORDER BY e.submissions_close_at DESC", (user["id"],))
    if not events:
        raise HTTPException(403, "You are not a judge on any event.")
    groups = []
    for ev in events:
        rows = all_(conn,
                    "SELECT p.id, p.title, p.tagline, tm.name team_name, tr.name track_name, p.track_id, "
                    " r.status review_status, r.updated_at, p.duplicate_of "
                    "FROM assignments a JOIN projects p ON p.id = a.project_id JOIN teams tm ON tm.id = p.team_id "
                    "LEFT JOIN tracks tr ON tr.id = p.track_id "
                    "LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id "
                    "WHERE a.event_id = ? AND a.judge_id = ? "
                    "ORDER BY (r.status = 'submitted'), (r.status IS NULL), p.title",
                    (ev["id"], user["id"]))
        done = sum(1 for r in rows if r["review_status"] == "submitted")
        groups.append({"event": ev, "rows": rows, "done": done, "phase": services.phase(ev),
                       "track_ix": services.track_index(conn, ev["id"])})
    return render(request, conn, "judge_queue.html", user=user, groups=groups)


@router.get("/judge/{event_id}/{project_id}")
def scoresheet(event_id: str, project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    assignment_or_403(conn, user, event_id, project_id)
    return _sheet(request, conn, user, event_id, project_id)


def _sheet(request, conn, user, event_id, project_id, errors=None, values=None, comment=None, status_code=200):
    ev = services.get_event(conn, event_id)
    p = one(conn, "SELECT p.*, tm.name team_name, tr.name track_name FROM projects p JOIN teams tm ON tm.id = p.team_id "
                  "LEFT JOIN tracks tr ON tr.id = p.track_id WHERE p.id = ?", (project_id,))
    review = one(conn, "SELECT * FROM reviews WHERE judge_id = ? AND project_id = ?", (user["id"], project_id))
    if values is None:
        values = services.review_values(conn, review["id"]) if review else {}
    if comment is None:
        comment = review["comment"] if review else ""
    queue_rows = all_(conn, "SELECT a.project_id, p.title, r.status FROM assignments a JOIN projects p ON p.id = a.project_id "
                            "LEFT JOIN reviews r ON r.project_id = a.project_id AND r.judge_id = a.judge_id "
                            "WHERE a.event_id = ? AND a.judge_id = ? ORDER BY p.title", (event_id, user["id"]))
    ids = [r["project_id"] for r in queue_rows]
    i = ids.index(project_id)
    nxt = next((r["project_id"] for r in queue_rows[i + 1:] + queue_rows[:i] if r["status"] != "submitted"), None)
    tags = [r["tag"] for r in all_(conn, "SELECT tag FROM project_tags WHERE project_id = ?", (project_id,))]
    answers = all_(conn, "SELECT q.prompt, a.body FROM answers a JOIN questions q ON q.id = a.question_id "
                         "WHERE a.project_id = ? AND a.body <> '' ORDER BY q.position", (project_id,))
    images = all_(conn, "SELECT * FROM project_images WHERE project_id = ? ORDER BY position", (project_id,))
    return render(request, conn, "judge_review.html", user=user, event=ev, p=p, review=review, values=values,
                  criteria=services.criteria(conn, event_id), next_id=nxt, position=i + 1, count=len(ids),
                  done=sum(1 for r in queue_rows if r["status"] == "submitted"), tags=tags, answers=answers,
                  images=images, errors=errors or [], comment=comment,
                  track_ix=services.track_index(conn, event_id), status_code=status_code)


@router.post("/judge/{event_id}/{project_id}")
async def scoresheet_save(event_id: str, project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    assignment_or_403(conn, user, event_id, project_id)
    await check_csrf(conn, request)
    ev = services.get_event(conn, event_id)
    form = await request.form()
    crit = services.criteria(conn, event_id)
    values = {c["key"]: form.get(f"c_{c['key']}") for c in crit}
    comment = str(form.get("comment", "")).strip()
    submit = form.get("action") == "submit"
    with transaction(conn):
        errors = save_review(conn, user, ev, project_id, values, comment, submit)
    if errors:
        # Re-render with the judge's unsaved input kept.
        kept = {k: int(v) for k, v in values.items() if v and str(v).isdigit()}
        return _sheet(request, conn, user, event_id, project_id, errors=errors, values=kept,
                      comment=comment, status_code=400)
    if submit:
        nxt = str(form.get("next_id", ""))
        return redirect(f"/judge/{event_id}/{nxt}" if nxt else "/judge", "Review submitted.")
    return redirect(f"/judge/{event_id}/{project_id}", "Draft saved. Nobody else can see it.")


@router.post("/judge/{event_id}/{project_id}/conflict")
async def declare_conflict(event_id: str, project_id: str, request: Request, conn=Depends(get_conn)):
    user = load_user(conn, request)
    assignment_or_403(conn, user, event_id, project_id)
    await check_csrf(conn, request)
    reason = str((await request.form()).get("reason", "")).strip()[:300] or "declared by judge"
    p = one(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
    review = one(conn, "SELECT status FROM reviews WHERE judge_id = ? AND project_id = ?", (user["id"], project_id))
    if review and review["status"] == "submitted":
        raise HTTPException(409, "You already submitted this review. Contact an organizer to withdraw it.")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO conflicts(event_id, judge_id, team_id, reason, created_at) VALUES (?,?,?,?,?)",
                     (event_id, user["id"], p["team_id"], reason, now()))
        conn.execute("DELETE FROM assignments WHERE judge_id = ? AND project_id = ?", (user["id"], project_id))
        audit.record(conn, actor=user, event_id=event_id, action="conflict.declare", subject=project_id,
                     summary=f"{user['name']} declared a conflict with “{p['title']}” and was unassigned",
                     detail={"team": p["team_id"], "reason": reason})
    return redirect("/judge", "Conflict recorded. The project was removed from your queue.")

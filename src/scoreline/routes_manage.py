"""Organizer console. Every handler starts with require_role(..., 'organizer')."""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from . import audit, services
from .db import all_, fmt_ts, new_id, now, one, parse_ts, transaction
from .importer import import_fixtures
from .security import (check_csrf, hash_password, load_user, require_admin, require_role, require_user,
                       token_hash, any_role)
from .web import get_conn, redirect, render

router = APIRouter(prefix="/manage")


def organizer(conn, request, event_id):
    user = load_user(conn, request)
    require_role(conn, user, event_id, "organizer")
    return user, services.get_event(conn, event_id)


def _dt_input(value: str) -> str | None:
    """<input type=datetime-local> gives '2026-03-01T18:00'; we treat it as UTC."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return fmt_ts(parse_ts(value if value.endswith("Z") else value + ("Z" if len(value) > 16 else ":00Z")))
    except ValueError:
        raise HTTPException(400, f"“{value}” is not a date and time.")


@router.get("")
def my_events(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    if user["is_admin"]:
        events = all_(conn, "SELECT * FROM events ORDER BY submissions_close_at DESC")
    else:
        events = all_(conn, "SELECT e.* FROM events e JOIN event_members em ON em.event_id = e.id "
                            "WHERE em.user_id = ? AND em.role = 'organizer' ORDER BY e.submissions_close_at DESC",
                      (user["id"],))
    if not events and not user["is_admin"]:
        raise HTTPException(403, "You do not organize any events.")
    return render(request, conn, "manage_events.html", user=user, events=events, phase_of=services.phase)


@router.get("/new")
def new_event_form(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    if not any_role(conn, user, "organizer"):
        raise HTTPException(403, "Only organizers and admins can create events.")
    return render(request, conn, "manage_new.html", user=user, values={}, errors=[])


@router.post("/new")
async def new_event(request: Request, conn=Depends(get_conn)):
    user = require_user(load_user(conn, request))
    if not any_role(conn, user, "organizer"):
        raise HTTPException(403, "Only organizers and admins can create events.")
    await check_csrf(conn, request)
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("name", "tagline", "opens", "closes", "tracks")}
    errors = []
    if not values["name"]:
        errors.append("Name the event.")
    opens, closes = _dt_input(values["opens"]), _dt_input(values["closes"])
    if not opens or not closes:
        errors.append("Set when submissions open and close.")
    elif opens >= closes:
        errors.append("Submissions must close after they open.")
    track_names = [t.strip() for t in values["tracks"].splitlines() if t.strip()]
    if errors:
        return render(request, conn, "manage_new.html", user=user, values=values, errors=errors, status_code=400)
    slug = re.sub(r"[^a-z0-9]+", "-", values["name"].lower()).strip("-")[:30] or "event"
    eid = f"evt_{slug}_{secrets.token_hex(2)}"
    with transaction(conn):
        conn.execute("INSERT INTO events(id, name, tagline, submissions_open_at, submissions_close_at, created_at) "
                     "VALUES (?,?,?,?,?,?)", (eid, values["name"], values["tagline"], opens, closes, now()))
        conn.execute("INSERT INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (eid, user["id"], "organizer", now()))
        for i, name in enumerate(track_names or ["General"]):
            conn.execute("INSERT INTO tracks(id, event_id, name, position) VALUES (?,?,?,?)",
                         (new_id("trk"), eid, name[:80], i))
        for i, (key, name, w, g) in enumerate([
            ("functionality", "Functionality", 0.4, "Does it run, and does it do what the team says?"),
            ("quality", "Quality", 0.35, "Is it built well enough that you would maintain it?"),
            ("innovation", "Innovation", 0.25, "Is there an idea here you have not seen before?"),
        ]):
            conn.execute("INSERT INTO criteria(id, event_id, key, name, guidance, weight, position) VALUES (?,?,?,?,?,?,?)",
                         (new_id("crit"), eid, key, name, g, w, i))
        audit.record(conn, actor=user, event_id=eid, action="event.create", subject=eid,
                     summary=f"{user['name']} created {values['name']}",
                     detail={"opens": opens, "closes": closes, "tracks": track_names})
    return redirect(f"/manage/{eid}/settings", "Event created. Review the rubric and add prizes next.")


@router.get("/{event_id}")
def overview(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    prog = services.progress(conn, event_id)
    return render(request, conn, "manage_overview.html", user=user, event=ev, prog=prog,
                  anomalies=services.anomalies(conn, event_id), coverage=services.coverage(conn, event_id)[:8],
                  recent=all_(conn, "SELECT * FROM audit_log WHERE event_id = ? ORDER BY seq DESC LIMIT 8", (event_id,)),
                  section="overview")


# --- settings -----------------------------------------------------------------

@router.get("/{event_id}/settings")
def settings(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    return render(request, conn, "manage_settings.html", user=user, event=ev, section="settings",
                  tracks=services.tracks(conn, event_id), criteria=services.criteria(conn, event_id),
                  prizes=all_(conn, "SELECT * FROM prizes WHERE event_id = ? ORDER BY position", (event_id,)),
                  questions=all_(conn, "SELECT * FROM questions WHERE event_id = ? ORDER BY position", (event_id,)),
                  has_reviews=bool(one(conn, "SELECT 1 FROM reviews WHERE event_id = ? LIMIT 1", (event_id,))))


@router.post("/{event_id}/settings/event")
async def save_event(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    f = await request.form()
    name = str(f.get("name", "")).strip() or ev["name"]
    opens = _dt_input(str(f.get("opens", ""))) or ev["submissions_open_at"]
    closes = _dt_input(str(f.get("closes", ""))) or ev["submissions_close_at"]
    if opens >= closes:
        return redirect(f"/manage/{event_id}/settings", "Submissions must close after they open.")
    try:
        size = max(1, min(20, int(f.get("max_team_size", ev["max_team_size"]))))
        per = max(1, min(20, int(f.get("reviews_per_project", ev["reviews_per_project"]))))
        shrink = max(0.0, min(50.0, float(f.get("bias_shrinkage", ev["bias_shrinkage"]))))
    except ValueError:
        return redirect(f"/manage/{event_id}/settings", "Team size, reviews and shrinkage must be numbers.")
    new = {"name": name, "tagline": str(f.get("tagline", "")).strip(), "description": str(f.get("description", "")).strip(),
           "submissions_open_at": opens, "submissions_close_at": closes, "max_team_size": size,
           "reviews_per_project": per, "bias_shrinkage": shrink, "is_public": 1 if f.get("is_public") else 0}
    changed = {k: [ev[k], v] for k, v in new.items() if ev[k] != v}
    with transaction(conn):
        conn.execute("UPDATE events SET name=?, tagline=?, description=?, submissions_open_at=?, submissions_close_at=?, "
                     "max_team_size=?, reviews_per_project=?, bias_shrinkage=?, is_public=? WHERE id=?",
                     (*new.values(), event_id))
        if changed:
            audit.record(conn, actor=user, event_id=event_id, action="event.update", subject=event_id,
                         summary=f"{user['name']} changed {', '.join(k.replace('_', ' ') for k in changed)}",
                         detail=changed)
    return redirect(f"/manage/{event_id}/settings", "Event details saved.")


@router.post("/{event_id}/settings/criteria")
async def save_criteria(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    f = await request.form()
    crit = services.criteria(conn, event_id)
    changes = {}
    with transaction(conn):
        for c in crit:
            try:
                w = float(f.get(f"w_{c['id']}", c["weight"]))
            except ValueError:
                raise HTTPException(400, f"The weight for {c['name']} must be a number.")
            if w <= 0:
                raise HTTPException(400, f"The weight for {c['name']} must be above zero. Remove the criterion instead.")
            name = str(f.get(f"n_{c['id']}", c["name"])).strip() or c["name"]
            guidance = str(f.get(f"g_{c['id']}", c["guidance"])).strip()
            if (w, name, guidance) != (c["weight"], c["name"], c["guidance"]):
                changes[c["key"]] = {"weight": [c["weight"], w], "name": [c["name"], name]}
                conn.execute("UPDATE criteria SET weight=?, name=?, guidance=? WHERE id=?", (w, name, guidance, c["id"]))
        new_name = str(f.get("new_name", "")).strip()
        if new_name:
            if one(conn, "SELECT 1 FROM reviews WHERE event_id = ? LIMIT 1", (event_id,)):
                raise HTTPException(409, "Judging has started, so criteria can be reweighted but not added.")
            key = re.sub(r"[^a-z0-9]+", "_", new_name.lower()).strip("_")[:30] or new_id("c")
            conn.execute("INSERT INTO criteria(id, event_id, key, name, guidance, weight, position) VALUES (?,?,?,?,?,?,?)",
                         (new_id("crit"), event_id, key, new_name, str(f.get("new_guidance", "")).strip(),
                          float(f.get("new_weight") or 1), len(crit)))
            changes[key] = {"added": True}
        if changes:
            audit.record(conn, actor=user, event_id=event_id, action="rubric.update", subject=event_id,
                         summary=f"{user['name']} changed the rubric ({', '.join(changes)})", detail=changes)
    return redirect(f"/manage/{event_id}/settings#rubric", "Rubric saved. Results recalculate from the new weights.")


@router.post("/{event_id}/settings/criteria/{crit_id}/delete")
async def delete_criterion(event_id: str, crit_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    if one(conn, "SELECT 1 FROM reviews WHERE event_id = ? LIMIT 1", (event_id,)):
        raise HTTPException(409, "Judging has started, so criteria cannot be removed.")
    if one(conn, "SELECT COUNT(*) n FROM criteria WHERE event_id = ?", (event_id,))["n"] <= 1:
        raise HTTPException(409, "A rubric needs at least one criterion.")
    with transaction(conn):
        c = one(conn, "SELECT * FROM criteria WHERE id = ? AND event_id = ?", (crit_id, event_id))
        conn.execute("DELETE FROM criteria WHERE id = ? AND event_id = ?", (crit_id, event_id))
        audit.record(conn, actor=user, event_id=event_id, action="rubric.remove", subject=crit_id,
                     summary=f"{user['name']} removed the criterion {c['name'] if c else crit_id}")
    return redirect(f"/manage/{event_id}/settings#rubric", "Criterion removed.")


@router.post("/{event_id}/settings/{kind}")
async def add_item(event_id: str, kind: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    f = await request.form()
    name = str(f.get("name", "")).strip()[:120]
    if not name:
        return redirect(f"/manage/{event_id}/settings#{kind}", "Give it a name first.")
    with transaction(conn):
        if kind == "tracks":
            pos = one(conn, "SELECT COUNT(*) n FROM tracks WHERE event_id = ?", (event_id,))["n"]
            conn.execute("INSERT INTO tracks(id, event_id, name, description, position) VALUES (?,?,?,?,?)",
                         (new_id("trk"), event_id, name, str(f.get("description", "")).strip(), pos))
        elif kind == "prizes":
            pos = one(conn, "SELECT COUNT(*) n FROM prizes WHERE event_id = ?", (event_id,))["n"]
            conn.execute("INSERT INTO prizes(id, event_id, track_id, name, reward, position) VALUES (?,?,?,?,?,?)",
                         (new_id("prz"), event_id, str(f.get("track_id", "")) or None, name,
                          str(f.get("reward", "")).strip(), pos))
        elif kind == "questions":
            pos = one(conn, "SELECT COUNT(*) n FROM questions WHERE event_id = ?", (event_id,))["n"]
            conn.execute("INSERT INTO questions(id, event_id, prompt, required, position) VALUES (?,?,?,?,?)",
                         (new_id("q"), event_id, name, 1 if f.get("required") else 0, pos))
        else:
            raise HTTPException(404, "Unknown setting.")
        audit.record(conn, actor=user, event_id=event_id, action=f"{kind}.add", subject=event_id,
                     summary=f"{user['name']} added {kind[:-1]} “{name}”")
    return redirect(f"/manage/{event_id}/settings#{kind}", "Added.")


@router.post("/{event_id}/settings/{kind}/{item_id}/delete")
async def delete_item(event_id: str, kind: str, item_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    table = {"tracks": "tracks", "prizes": "prizes", "questions": "questions"}.get(kind)
    if not table:
        raise HTTPException(404, "Unknown setting.")
    if kind == "tracks" and one(conn, "SELECT 1 FROM projects WHERE track_id = ? LIMIT 1", (item_id,)):
        raise HTTPException(409, "Projects are entered in this track, so it cannot be removed.")
    with transaction(conn):
        conn.execute(f"DELETE FROM {table} WHERE id = ? AND event_id = ?", (item_id, event_id))
        audit.record(conn, actor=user, event_id=event_id, action=f"{kind}.remove", subject=item_id,
                     summary=f"{user['name']} removed a {kind[:-1]}")
    return redirect(f"/manage/{event_id}/settings#{kind}", "Removed.")


# --- judges and assignment ------------------------------------------------------

@router.get("/{event_id}/judges")
def judges(event_id: str, request: Request, conn=Depends(get_conn), invite: str = ""):
    user, ev = organizer(conn, request, event_id)
    prog = services.progress(conn, event_id)
    jt = services.judge_tracks_map(conn, event_id)
    return render(request, conn, "manage_judges.html", user=user, event=ev, section="judges", prog=prog,
                  tracks=services.tracks(conn, event_id), jt=jt, coverage=services.coverage(conn, event_id),
                  invite_link=invite, track_ix=services.track_index(conn, event_id),
                  conflicts=all_(conn, "SELECT c.*, u.name judge_name, t.name team_name FROM conflicts c "
                                       "JOIN users u ON u.id = c.judge_id JOIN teams t ON t.id = c.team_id "
                                       "WHERE c.event_id = ? ORDER BY c.created_at DESC", (event_id,)))


@router.post("/{event_id}/judges/invite")
async def invite_judge(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    f = await request.form()
    email = str(f.get("email", "")).strip().lower()
    name = str(f.get("name", "")).strip() or email.split("@")[0]
    track_ids = [t for t in f.getlist("tracks") if t]
    if "@" not in email:
        return redirect(f"/manage/{event_id}/judges", "Enter the judge's email address.")
    token = None
    with transaction(conn):
        u = one(conn, "SELECT * FROM users WHERE email = ?", (email,))
        if not u:
            uid = new_id("usr")
            conn.execute("INSERT INTO users(id, email, name, created_at) VALUES (?,?,?,?)", (uid, email, name, now()))
        else:
            uid = u["id"]
        if one(conn, "SELECT 1 FROM event_members WHERE event_id = ? AND user_id = ? AND role = 'participant'",
               (event_id, uid)):
            return redirect(f"/manage/{event_id}/judges", f"{email} is competing in this event and cannot judge it.")
        conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (event_id, uid, "judge", now()))
        conn.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event_id, uid))
        for t in track_ids:
            conn.execute("INSERT INTO judge_tracks(event_id, user_id, track_id) VALUES (?,?,?)", (event_id, uid, t))
        if not u or not u["password_hash"]:
            token = secrets.token_urlsafe(24)
            conn.execute("INSERT INTO invites(token_hash, user_id, event_id, purpose, created_at) VALUES (?,?,?,?,?)",
                         (token_hash(token), uid, event_id, "judge", now()))
        audit.record(conn, actor=user, event_id=event_id, action="judge.invite", subject=uid,
                     summary=f"{user['name']} invited {name} as a judge", detail={"tracks": track_ids})
    if token:
        base = str(request.base_url).rstrip("/")
        from urllib.parse import quote
        return redirect(f"/manage/{event_id}/judges?invite={quote(base + '/invite/' + token)}",
                        f"{name} added. Send them the one-time link below; there is no email server.")
    return redirect(f"/manage/{event_id}/judges", f"{name} already has an account and can start judging.")


@router.post("/{event_id}/judges/auto-assign")
async def run_auto_assign(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    with transaction(conn):
        result = services.auto_assign(conn, event_id, user)
    msg = f"Created {len(result['created'])} assignments."
    if result["short"]:
        msg += f" {len(result['short'])} projects still have fewer than {result['target']} judges; add judges to their tracks."
    return redirect(f"/manage/{event_id}/judges", msg)


@router.post("/{event_id}/judges/assign")
async def manual_assign(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    f = await request.form()
    judge_id, project_id = str(f.get("judge_id", "")), str(f.get("project_id", "")).strip()
    p = one(conn, "SELECT * FROM projects WHERE id = ? AND event_id = ?", (project_id, event_id))
    if not p or judge_id not in services.judge_ids(conn, event_id):
        return redirect(f"/manage/{event_id}/judges", "Pick a judge and a project from this event.")
    with transaction(conn):
        conn.execute("INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) VALUES (?,?,?,?,?)",
                     (event_id, judge_id, project_id, "manual", now()))
        audit.record(conn, actor=user, event_id=event_id, action="assignment.create", subject=project_id,
                     summary=f"{user['name']} assigned “{p['title']}” to a judge", detail={"judge": judge_id})
    return redirect(f"/manage/{event_id}/judges", "Assigned.")


@router.post("/{event_id}/judges/{judge_id}/tracks")
async def set_judge_tracks(event_id: str, judge_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    track_ids = [t for t in (await request.form()).getlist("tracks") if t]
    with transaction(conn):
        conn.execute("DELETE FROM judge_tracks WHERE event_id = ? AND user_id = ?", (event_id, judge_id))
        for t in track_ids:
            conn.execute("INSERT INTO judge_tracks(event_id, user_id, track_id) VALUES (?,?,?)", (event_id, judge_id, t))
        audit.record(conn, actor=user, event_id=event_id, action="judge.tracks", subject=judge_id,
                     summary=f"{user['name']} changed a judge's tracks", detail={"tracks": track_ids})
    return redirect(f"/manage/{event_id}/judges", "Tracks updated. Existing assignments are unchanged.")


# --- results ------------------------------------------------------------------

@router.get("/{event_id}/results")
def results(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    table = services.results(conn, event_id)
    dups = all_(conn, "SELECT p.id, p.title, p.submitted_at, p.duplicate_of, o.title orig_title, "
                      "o.submitted_at orig_submitted_at, tm.name team_name FROM projects p "
                      "JOIN projects o ON o.id = p.duplicate_of JOIN teams tm ON tm.id = p.team_id "
                      "WHERE p.event_id = ?", (event_id,))
    return render(request, conn, "manage_results.html", user=user, event=ev, section="results", table=table,
                  dups=dups, criteria=services.criteria(conn, event_id), track_ix=services.track_index(conn, event_id))


@router.post("/{event_id}/results/{action}")
async def publish(event_id: str, action: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    if action not in ("publish", "unpublish"):
        raise HTTPException(404, "Unknown action.")
    if action == "publish" and services.phase(ev) in ("open", "upcoming"):
        raise HTTPException(409, "Submissions are still open. Publish after they close.")
    with transaction(conn):
        if action == "publish":
            table = services.results(conn, event_id)
            snapshot = [{"rank": r.rank, "project": r.project_id, "score": round(r.normalized, 4)}
                        for r in table["projects"]]
            conn.execute("UPDATE events SET results_published_at = ? WHERE id = ?", (now(), event_id))
            audit.record(conn, actor=user, event_id=event_id, action="results.publish", subject=event_id,
                         summary=f"{user['name']} published results. Scores are now locked.",
                         detail={"ranking": snapshot, "shrinkage": ev["bias_shrinkage"],
                                 "chain_head": one(conn, "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1")["hash"]})
        else:
            conn.execute("UPDATE events SET results_published_at = NULL WHERE id = ?", (event_id,))
            audit.record(conn, actor=user, event_id=event_id, action="results.unpublish", subject=event_id,
                         summary=f"{user['name']} took results down. Scores are editable again.")
    return redirect(f"/manage/{event_id}/results",
                    "Results published. Scores are locked." if action == "publish" else "Results hidden again.")


@router.post("/{event_id}/duplicates/{project_id}")
async def resolve_duplicate(event_id: str, project_id: str, request: Request, conn=Depends(get_conn)):
    """Swap which copy is canonical, or confirm the current choice."""
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    choice = str((await request.form()).get("keep", ""))
    dup = one(conn, "SELECT * FROM projects WHERE id = ? AND event_id = ? AND duplicate_of IS NOT NULL",
              (project_id, event_id))
    if not dup:
        raise HTTPException(404, "That project is not marked as a duplicate.")
    with transaction(conn):
        if choice == "newer":
            orig = dup["duplicate_of"]
            conn.execute("UPDATE projects SET duplicate_of = ? WHERE id = ?", (project_id, orig))
            conn.execute("UPDATE projects SET duplicate_of = NULL WHERE id = ?", (project_id,))
            conn.execute("UPDATE projects SET duplicate_of = ? WHERE duplicate_of = ? AND id <> ?", (project_id, orig, orig))
            summary = f"{user['name']} kept {project_id} and set {orig} aside as the duplicate"
        else:
            summary = f"{user['name']} confirmed {project_id} as a duplicate of {dup['duplicate_of']}"
        audit.record(conn, actor=user, event_id=event_id, action="duplicate.resolve", subject=project_id,
                     summary=summary, detail={"keep": choice or "original"})
    return redirect(f"/manage/{event_id}/results#duplicates", "Duplicate resolved. The ranking has been recalculated.")


@router.post("/{event_id}/reviews/{review_id}/reopen")
async def reopen_review(event_id: str, review_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    await check_csrf(conn, request)
    if ev["results_published_at"]:
        raise HTTPException(409, "Unpublish results before reopening a review.")
    r = one(conn, "SELECT * FROM reviews WHERE id = ? AND event_id = ?", (review_id, event_id))
    if not r:
        raise HTTPException(404, "No such review.")
    with transaction(conn):
        conn.execute("UPDATE reviews SET status = 'draft', submitted_at = NULL WHERE id = ?", (review_id,))
        audit.record(conn, actor=user, event_id=event_id, action="review.reopen", subject=r["project_id"],
                     summary=f"{user['name']} reopened a review so the judge can change it",
                     detail={"review": review_id, "judge": r["judge_id"]})
    return redirect(f"/manage/{event_id}/judges", "Review reopened.")


# --- audit and data -----------------------------------------------------------

@router.get("/{event_id}/audit")
def audit_page(event_id: str, request: Request, conn=Depends(get_conn), action: str = "", page: int = 1):
    user, ev = organizer(conn, request, event_id)
    params = [event_id]
    where = "event_id = ?"
    if action:
        where += " AND action LIKE ?"
        params.append(action + "%")
    page = max(1, page)
    rows = all_(conn, f"SELECT * FROM audit_log WHERE {where} ORDER BY seq DESC LIMIT 100 OFFSET ?",
                params + [(page - 1) * 100])
    total = one(conn, f"SELECT COUNT(*) n FROM audit_log WHERE {where}", params)["n"]
    kinds = [r["k"] for r in all_(conn, "SELECT DISTINCT substr(action, 1, instr(action, '.') - 1) k FROM audit_log "
                                        "WHERE event_id = ? ORDER BY k", (event_id,))]
    return render(request, conn, "manage_audit.html", user=user, event=ev, section="audit", rows=rows, total=total,
                  page=page, kinds=kinds, action=action, chain=audit.verify_chain(conn),
                  head=one(conn, "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"))


@router.get("/{event_id}/data")
def data_page(event_id: str, request: Request, conn=Depends(get_conn)):
    user, ev = organizer(conn, request, event_id)
    return render(request, conn, "manage_data.html", user=user, event=ev, section="data")


@router.post("/import")
async def import_event(request: Request, conn=Depends(get_conn)):
    """Admins can load a fixtures-shaped JSON file as a new event."""
    user = require_admin(load_user(conn, request))
    await check_csrf(conn, request)
    f = await request.form()
    upload = f.get("file")
    try:
        data = json.loads((await upload.read()).decode()) if hasattr(upload, "read") else None
        if not data or "event" not in data:
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return redirect("/manage", "That file is not a fixtures-shaped JSON document.")
    if one(conn, "SELECT 1 FROM events WHERE id = ?", (data["event"]["id"],)):
        data["event"]["id"] = f"{data['event']['id']}_{secrets.token_hex(2)}"
    with transaction(conn):
        report = import_fixtures(conn, data, actor=user)
        conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (data["event"]["id"], user["id"], "organizer", now()))
    return redirect(f"/manage/{data['event']['id']}",
                    f"Imported {report['counts']['projects']} projects; {len(report['duplicates'])} flagged as duplicates.")


@router.get("/-/admin")
def admin_page(request: Request, conn=Depends(get_conn), q: str = ""):
    user = require_admin(load_user(conn, request))
    like = f"%{q}%"
    users = all_(conn, "SELECT u.*, (SELECT group_concat(em.role || ':' || em.event_id, ', ') FROM event_members em "
                       "WHERE em.user_id = u.id) roles FROM users u WHERE u.email LIKE ? OR u.name LIKE ? "
                       "ORDER BY u.is_admin DESC, u.name LIMIT 200", (like, like))
    events = all_(conn, "SELECT * FROM events ORDER BY submissions_close_at DESC")
    return render(request, conn, "admin.html", user=user, users=users, events=events, q=q)


@router.post("/-/admin/organizer")
async def grant_organizer(request: Request, conn=Depends(get_conn)):
    user = require_admin(load_user(conn, request))
    await check_csrf(conn, request)
    f = await request.form()
    email, event_id = str(f.get("email", "")).strip().lower(), str(f.get("event_id", ""))
    target = one(conn, "SELECT * FROM users WHERE email = ?", (email,))
    services.get_event(conn, event_id)
    if not target:
        return redirect("/manage/-/admin", "No account with that email. Ask them to sign up first.")
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (event_id, target["id"], "organizer", now()))
        audit.record(conn, actor=user, event_id=event_id, action="role.grant", subject=target["id"],
                     summary=f"{user['name']} made {target['name']} an organizer")
    return redirect("/manage/-/admin", f"{target['name']} is now an organizer of that event.")

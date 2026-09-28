"""Domain logic shared by the HTML pages and the JSON API."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import HTTPException

from . import audit
from .db import all_, now, one, parse_ts
from .scoring import rank_projects, weighted_score

# --- events -------------------------------------------------------------------


def get_event(conn, event_id: str):
    ev = one(conn, "SELECT * FROM events WHERE id = ?", (event_id,))
    if not ev:
        raise HTTPException(404, "No event with that id.")
    return ev


def phase(ev) -> str:
    t = datetime.now(timezone.utc)
    if ev["results_published_at"]:
        return "results"
    if t < parse_ts(ev["submissions_open_at"]):
        return "upcoming"
    if t < parse_ts(ev["submissions_close_at"]):
        return "open"
    return "judging"


PHASES = [
    ("upcoming", "Registration"),
    ("open", "Submissions open"),
    ("judging", "Judging"),
    ("results", "Results"),
]


def submissions_open(ev) -> bool:
    return phase(ev) == "open"


def assert_submissions_open(ev) -> None:
    """The one place the deadline is checked in Python. The database trigger
    `projects_frozen_after_close` is the backstop."""
    p = phase(ev)
    if p == "upcoming":
        raise HTTPException(403, f"Submissions for {ev['name']} have not opened yet.")
    if p != "open":
        raise HTTPException(403, f"Submissions for {ev['name']} closed at {ev['submissions_close_at']}.")


def list_events(conn, user=None):
    return all_(conn, "SELECT * FROM events WHERE is_public = 1 ORDER BY submissions_close_at DESC")


def tracks(conn, event_id):
    return all_(conn, "SELECT * FROM tracks WHERE event_id = ? ORDER BY position, name", (event_id,))


def criteria(conn, event_id):
    return [dict(r) for r in all_(conn, "SELECT * FROM criteria WHERE event_id = ? ORDER BY position, key",
                                  (event_id,))]


def track_index(conn, event_id) -> dict[str, int]:
    """Stable 0..7 colour slot per track."""
    return {t["id"]: i % 8 for i, t in enumerate(tracks(conn, event_id))}


# --- gallery ------------------------------------------------------------------

SORTS = {
    "title": "p.title COLLATE NOCASE ASC, p.id",
    "newest": "p.submitted_at DESC, p.id",
    "oldest": "p.submitted_at ASC, p.id",
}


def gallery(conn, *, event_id=None, track=None, tag=None, q=None, sort="title", limit=60, offset=0):
    where = ["p.status = 'submitted'", "p.duplicate_of IS NULL", "e.is_public = 1"]
    params: list = []
    if event_id:
        where.append("p.event_id = ?")
        params.append(event_id)
    if track:
        where.append("p.track_id = ?")
        params.append(track)
    if tag:
        where.append("EXISTS (SELECT 1 FROM project_tags t WHERE t.project_id = p.id AND t.tag = ?)")
        params.append(tag)
    if q:
        like = f"%{q.strip()}%"
        where.append("(p.title LIKE ? OR p.tagline LIKE ? OR p.description LIKE ? OR tm.name LIKE ? "
                     "OR EXISTS (SELECT 1 FROM project_tags t WHERE t.project_id = p.id AND t.tag LIKE ?))")
        params += [like] * 5
    sql_where = " AND ".join(where)
    order = SORTS.get(sort, SORTS["title"])
    total = one(conn, f"SELECT COUNT(*) n FROM projects p JOIN teams tm ON tm.id = p.team_id "
                      f"JOIN events e ON e.id = p.event_id WHERE {sql_where}", params)["n"]
    rows = all_(conn,
                f"SELECT p.*, tm.name AS team_name, tr.name AS track_name, e.name AS event_name, "
                f"(SELECT group_concat(tag, ',') FROM project_tags t WHERE t.project_id = p.id) AS tags "
                f"FROM projects p JOIN teams tm ON tm.id = p.team_id JOIN events e ON e.id = p.event_id "
                f"LEFT JOIN tracks tr ON tr.id = p.track_id WHERE {sql_where} ORDER BY {order} "
                f"LIMIT ? OFFSET ?", params + [limit, offset])
    return total, rows


def popular_tags(conn, event_id=None, limit=16):
    sql = ("SELECT t.tag, COUNT(*) n FROM project_tags t JOIN projects p ON p.id = t.project_id "
           "WHERE p.status = 'submitted' AND p.duplicate_of IS NULL")
    params = []
    if event_id:
        sql += " AND p.event_id = ?"
        params.append(event_id)
    sql += " GROUP BY t.tag ORDER BY n DESC, t.tag LIMIT ?"
    return all_(conn, sql, params + [limit])


# --- teams and projects ---------------------------------------------------------

def team_for(conn, event_id, user_id):
    return one(conn, "SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id "
                     "WHERE t.event_id = ? AND m.user_id = ?", (event_id, user_id))


def team_members(conn, team_id):
    return all_(conn, "SELECT u.id, u.name, u.email, m.joined_at FROM team_members m "
                      "JOIN users u ON u.id = m.user_id WHERE m.team_id = ? ORDER BY m.joined_at", (team_id,))


def project_for_team(conn, team_id):
    return one(conn, "SELECT * FROM projects WHERE team_id = ? AND duplicate_of IS NULL "
                     "AND status <> 'withdrawn' ORDER BY created_at LIMIT 1", (team_id,))


# --- judging --------------------------------------------------------------------

def review_values(conn, review_id) -> dict[str, int]:
    return {r["key"]: r["value"] for r in all_(conn,
            "SELECT c.key, s.value FROM review_scores s JOIN criteria c ON c.id = s.criterion_id "
            "WHERE s.review_id = ?", (review_id,))}


def eligible_reviews(conn, event_id):
    """(judge, project, weighted score) for every submitted review of a live project."""
    crit = criteria(conn, event_id)
    rows = all_(conn,
                "SELECT r.id, r.judge_id, r.project_id FROM reviews r JOIN projects p ON p.id = r.project_id "
                "WHERE r.event_id = ? AND r.status = 'submitted' AND p.status = 'submitted' "
                "AND p.duplicate_of IS NULL", (event_id,))
    values: dict[str, dict[str, int]] = defaultdict(dict)
    for s in all_(conn, "SELECT s.review_id, c.key, s.value FROM review_scores s "
                        "JOIN criteria c ON c.id = s.criterion_id WHERE c.event_id = ?", (event_id,)):
        values[s["review_id"]][s["key"]] = s["value"]
    out = []
    for r in rows:
        ws = weighted_score(values[r["id"]], crit)
        if ws is not None:
            out.append((r["judge_id"], r["project_id"], ws))
    return out


def results(conn, event_id):
    ev = get_event(conn, event_id)
    reviews = eligible_reviews(conn, event_id)
    submitted = {r["id"]: r["submitted_at"] or "" for r in
                 all_(conn, "SELECT id, submitted_at FROM projects WHERE event_id = ?", (event_id,))}
    table = rank_projects(reviews, ev["bias_shrinkage"], submitted)
    projects = {r["id"]: r for r in all_(conn,
                "SELECT p.id, p.title, p.track_id, tm.name team_name, tr.name track_name FROM projects p "
                "JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks tr ON tr.id = p.track_id "
                "WHERE p.event_id = ?", (event_id,))}
    judges = {r["id"]: r for r in all_(conn, "SELECT id, name, email FROM users")}
    # Per-track ranks, from the same normalized scores.
    per_track: dict[str, int] = defaultdict(int)
    track_rank = {}
    for r in table["projects"]:
        tid = projects[r.project_id]["track_id"]
        per_track[tid] += 1
        track_rank[r.project_id] = per_track[tid]
    table["project_rows"] = projects
    table["judge_rows"] = judges
    table["track_rank"] = track_rank
    return table


def judge_ids(conn, event_id):
    return [r["user_id"] for r in all_(conn, "SELECT user_id FROM event_members WHERE event_id = ? "
                                             "AND role = 'judge' ORDER BY user_id", (event_id,))]


def judge_tracks_map(conn, event_id) -> dict[str, set[str]]:
    m: dict[str, set[str]] = defaultdict(set)
    for r in all_(conn, "SELECT user_id, track_id FROM judge_tracks WHERE event_id = ?", (event_id,)):
        m[r["user_id"]].add(r["track_id"])
    return m


def detect_conflicts(conn, event_id, actor=None) -> int:
    """Record a conflict wherever a judge's email is on a team. Idempotent."""
    rows = all_(conn,
                "SELECT DISTINCT em.user_id judge_id, tm.team_id FROM event_members em "
                "JOIN team_members tm ON tm.user_id = em.user_id AND tm.event_id = em.event_id "
                "WHERE em.event_id = ? AND em.role = 'judge'", (event_id,))
    added = 0
    for r in rows:
        cur = conn.execute("INSERT OR IGNORE INTO conflicts(event_id, judge_id, team_id, reason, created_at) "
                           "VALUES (?,?,?,?,?)", (event_id, r["judge_id"], r["team_id"], "judge is a team member", now()))
        added += cur.rowcount
    return added


def auto_assign(conn, event_id: str, actor, target: int | None = None) -> dict:
    """Top every live project up to `target` assigned judges.

    Greedy, least-loaded first, restricted to judges on the project's track
    and without a conflict. Ties break on a hash of (judge, project) so the
    spread is even but fully reproducible. Existing assignments are kept.
    """
    ev = get_event(conn, event_id)
    target = target or ev["reviews_per_project"]
    judges = judge_ids(conn, event_id)
    jt = judge_tracks_map(conn, event_id)
    conflicts = {(r["judge_id"], r["team_id"]) for r in
                 all_(conn, "SELECT judge_id, team_id FROM conflicts WHERE event_id = ?", (event_id,))}
    load = defaultdict(int)
    assigned = defaultdict(set)
    for a in all_(conn, "SELECT judge_id, project_id FROM assignments WHERE event_id = ?", (event_id,)):
        load[a["judge_id"]] += 1
        assigned[a["project_id"]].add(a["judge_id"])
    projects = all_(conn, "SELECT id, team_id, track_id, title FROM projects WHERE event_id = ? "
                          "AND status = 'submitted' AND duplicate_of IS NULL ORDER BY id", (event_id,))
    # Most under-covered projects choose first.
    projects = sorted(projects, key=lambda p: (len(assigned[p["id"]]), p["id"]))
    created, short = [], []
    for p in projects:
        need = target - len(assigned[p["id"]])
        if need <= 0:
            continue
        pool = [j for j in judges
                if j not in assigned[p["id"]]
                and (not jt.get(j) or p["track_id"] in jt[j])
                and (j, p["team_id"]) not in conflicts]
        pool.sort(key=lambda j: (load[j], hashlib.sha256(f"{j}:{p['id']}".encode()).hexdigest()))
        for j in pool[:need]:
            conn.execute("INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) "
                         "VALUES (?,?,?,?,?)", (event_id, j, p["id"], "auto", now()))
            load[j] += 1
            assigned[p["id"]].add(j)
            created.append((j, p["id"]))
        if len(assigned[p["id"]]) < target:
            short.append({"project": p["id"], "title": p["title"], "have": len(assigned[p["id"]])})
    audit.record(conn, actor=actor, event_id=event_id, action="assignment.auto",
                 summary=(f"{actor['name']} auto-assigned " if actor else "Auto-assigned ") + f"{len(created)} reviews to bring every project to {target} judges",
                 detail={"created": len(created), "short": short, "target": target})
    return {"created": created, "short": short, "target": target}


def progress(conn, event_id):
    """Per-judge and per-track completion for the organizer dashboard."""
    judges = all_(conn,
                  "SELECT u.id, u.name, u.email, "
                  " (SELECT COUNT(*) FROM assignments a WHERE a.judge_id = u.id AND a.event_id = ?) assigned, "
                  " (SELECT COUNT(*) FROM reviews r WHERE r.judge_id = u.id AND r.event_id = ? AND r.status='submitted') done, "
                  " (SELECT COUNT(*) FROM reviews r WHERE r.judge_id = u.id AND r.event_id = ? AND r.status='draft') drafts, "
                  " (SELECT MAX(r.updated_at) FROM reviews r JOIN assignments a ON a.judge_id = r.judge_id AND a.project_id = r.project_id "
                  "  WHERE r.judge_id = u.id AND r.event_id = ? AND a.source <> 'import') last_active "
                  "FROM event_members em JOIN users u ON u.id = em.user_id "
                  "WHERE em.event_id = ? AND em.role = 'judge' ORDER BY u.name",
                  (event_id,) * 5)
    # Who needs chasing first: not started, then least far along.
    judges = sorted(judges, key=lambda j: (j["assigned"] == 0, (j["done"] / j["assigned"]) if j["assigned"] else 1,
                                           j["drafts"] > 0, j["name"]))
    track_rows = all_(conn,
                      "SELECT t.id, t.name, "
                      " (SELECT COUNT(*) FROM projects p WHERE p.track_id = t.id AND p.status='submitted' AND p.duplicate_of IS NULL) projects, "
                      " (SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id = a.project_id WHERE p.track_id = t.id AND p.duplicate_of IS NULL) assigned, "
                      " (SELECT COUNT(*) FROM reviews r JOIN projects p ON p.id = r.project_id WHERE p.track_id = t.id AND r.status='submitted' AND p.duplicate_of IS NULL) done "
                      "FROM tracks t WHERE t.event_id = ? ORDER BY t.position, t.name", (event_id,))
    totals = one(conn,
                 "SELECT (SELECT COUNT(*) FROM projects WHERE event_id = ? AND status='submitted' AND duplicate_of IS NULL) projects, "
                 " (SELECT COUNT(*) FROM projects WHERE event_id = ? AND status='draft') drafts, "
                 " (SELECT COUNT(*) FROM assignments a JOIN projects p ON p.id=a.project_id WHERE a.event_id = ? AND p.duplicate_of IS NULL) assigned, "
                 " (SELECT COUNT(*) FROM reviews r JOIN projects p ON p.id=r.project_id WHERE r.event_id = ? AND r.status='submitted' AND p.duplicate_of IS NULL) done, "
                 " (SELECT COUNT(*) FROM teams WHERE event_id = ?) teams",
                 (event_id,) * 5)
    return {"judges": judges, "tracks": track_rows, "totals": totals}


def coverage(conn, event_id):
    """Reviews per live project, lowest first."""
    return all_(conn,
                "SELECT p.id, p.title, tr.name track_name, "
                " (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) assigned, "
                " (SELECT COUNT(*) FROM reviews r WHERE r.project_id = p.id AND r.status='submitted') done "
                "FROM projects p LEFT JOIN tracks tr ON tr.id = p.track_id "
                "WHERE p.event_id = ? AND p.status='submitted' AND p.duplicate_of IS NULL "
                "ORDER BY done, assigned, p.title", (event_id,))


def anomalies(conn, event_id):
    """Things an organizer should look at before publishing, in plain words."""
    ev = get_event(conn, event_id)
    out = []
    for d in all_(conn, "SELECT p.id, p.title, p.duplicate_of, o.title orig_title FROM projects p "
                        "JOIN projects o ON o.id = p.duplicate_of WHERE p.event_id = ?", (event_id,)):
        out.append({"kind": "duplicate", "subject": d["id"],
                    "text": f"“{d['title']}” ({d['id']}) looks like a second copy of {d['duplicate_of']}. "
                            f"It is excluded from the gallery and the ranking until you decide."})
    reviews = eligible_reviews(conn, event_id)
    by_j = defaultdict(list)
    for j, p, s in reviews:
        by_j[j].append(s)
    names = {r["id"]: r["name"] for r in all_(conn, "SELECT id, name FROM users")}
    for j, scores in sorted(by_j.items()):
        if len(scores) >= 2 and max(scores) - min(scores) < 1e-9:
            out.append({"kind": "flat_judge", "subject": j,
                        "text": f"{names.get(j, j)} gave the same score ({scores[0]:.2f}) to all "
                                f"{len(scores)} projects they reviewed. Their reviews still count, but they "
                                f"do not separate projects. Normalization handles the level; the spread is "
                                f"something to raise with them."})
    from .scoring import components
    groups = components(reviews)
    if len(groups) > 1:
        out.append({"kind": "disconnected", "subject": "",
                    "text": f"The judging splits into {len(groups)} groups that share no judge (largest has "
                            f"{len(groups[0])} projects). Adjusted scores compare fairly within a group but not "
                            f"across groups. Assign a few judges across groups before publishing an overall ranking."})
    target = ev["reviews_per_project"]
    thin = [c for c in coverage(conn, event_id) if c["done"] < target]
    if thin:
        out.append({"kind": "coverage", "subject": "",
                    "text": f"{len(thin)} projects have fewer than {target} finished reviews. "
                            f"Their normalized scores carry wider error bars."})
    return out

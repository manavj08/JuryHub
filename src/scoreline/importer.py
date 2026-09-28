"""Bulk import and export in the DOGFOOD fixtures shape.

The fixture format is the lingua franca: anything exported here can be
re-imported into a fresh Scoreline (or read by anyone else's tool), which is
the "leave as easily as you arrived" path.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta

from . import audit
from .db import fmt_ts, now, one, parse_ts, all_

DEFAULT_WEIGHTS = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}
DEFAULT_GUIDANCE = {
    "functionality": "Does it run, and does it do what the team says it does?",
    "quality": "Is the code, design and documentation something you would maintain?",
    "innovation": "Is there an idea here you have not seen done this way before?",
}


def _name_from_email(email: str) -> str:
    """A readable placeholder until the person sets their own name.
    priya1@ -> Priya, member7_1@ -> Member 7.1, ada.lovelace@ -> Ada Lovelace."""
    local = email.split("@")[0].lower()
    if m := re.fullmatch(r"([a-z]+)\d+", local):
        return m.group(1).capitalize()
    if m := re.fullmatch(r"([a-z]+)(\d+)_(\d+)", local):
        return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}"
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    return " ".join(p.capitalize() for p in parts) or email


def invite_code() -> str:
    return secrets.token_urlsafe(9)


def ensure_user(conn, email: str, name: str | None, password_hash: str | None):
    row = one(conn, "SELECT * FROM users WHERE email = ?", (email,))
    if row:
        return row["id"], False
    uid = f"usr_{secrets.token_hex(5)}"
    conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                 (uid, email, name or _name_from_email(email), password_hash, now()))
    return uid, True


def import_fixtures(conn, data: dict, *, actor=None, password_hash: str | None = None) -> dict:
    """Load a fixtures-shaped document. Call inside a transaction.

    Judges keep their fixture ids as user ids (jdg_01...), so a judge id in an
    export means the same person as in the input.
    """
    report = {"warnings": [], "duplicates": [], "skipped_scores": 0}
    ev = data["event"]
    close = ev["submissions_close"]
    opens = ev.get("submissions_open") or fmt_ts(parse_ts(close) - timedelta(hours=72))
    conn.execute(
        "INSERT INTO events(id, name, tagline, description, submissions_open_at, submissions_close_at, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (ev["id"], ev["name"], ev.get("tagline", ""), ev.get("description", ""), opens, close, now()))
    eid = ev["id"]

    for i, t in enumerate(data.get("tracks", [])):
        conn.execute("INSERT INTO tracks(id, event_id, name, position) VALUES (?,?,?,?)",
                     (t["id"], eid, t["name"], i))

    # Rubric: taken from the file when it carries one (a Scoreline export
    # does), otherwise every criterion key that appears in any score, with
    # the documented default weights.
    declared = {c["key"]: c for c in data.get("criteria", [])}
    keys: list[str] = list(declared)
    for s in data.get("scores", []):
        for k in s.get("criteria", {}):
            if k not in keys:
                keys.append(k)
    crit_id = {}
    for i, k in enumerate(keys):
        c = declared.get(k, {})
        cid = f"{eid}_crit_{k}"
        crit_id[k] = cid
        conn.execute("INSERT INTO criteria(id, event_id, key, name, guidance, weight, min_score, max_score, position) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, eid, k, c.get("name") or k.replace("_", " ").capitalize(),
                      c.get("guidance", DEFAULT_GUIDANCE.get(k, "")), c.get("weight", DEFAULT_WEIGHTS.get(k, 1.0)),
                      c.get("min", 1), c.get("max", 5), i))

    judge_emails = set()
    for j in data.get("judges", []):
        email = j["email"].lower()
        judge_emails.add(email)
        existing = one(conn, "SELECT id FROM users WHERE email = ?", (email,))
        if existing:
            uid = existing["id"]
        else:
            uid = j["id"]
            conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                         (uid, email, j["name"], password_hash, now()))
        conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                     (eid, uid, "judge", now()))
        for tid in j.get("tracks", []):
            conn.execute("INSERT OR IGNORE INTO judge_tracks(event_id, user_id, track_id) VALUES (?,?,?)",
                         (eid, uid, tid))
    judge_uid = {j["id"]: (one(conn, "SELECT id FROM users WHERE email = ?", (j["email"].lower(),))["id"])
                 for j in data.get("judges", [])}

    for t in data.get("teams", []):
        conn.execute("INSERT INTO teams(id, event_id, name, invite_code, created_at) VALUES (?,?,?,?,?)",
                     (t["id"], eid, t["name"], invite_code(), now()))
        for email in t.get("members", []):
            email = email.lower()
            uid, _ = ensure_user(conn, email, None, password_hash)
            if email in judge_emails:
                report["warnings"].append(f"{email} is both a judge and on team {t['id']}; recorded as a conflict")
                conn.execute("INSERT OR IGNORE INTO conflicts(event_id, judge_id, team_id, reason, created_at) "
                             "VALUES (?,?,?,?,?)", (eid, uid, t["id"], "judge is listed as a team member", now()))
                continue
            conn.execute("INSERT OR IGNORE INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                         (eid, uid, "participant", now()))
            conn.execute("INSERT OR IGNORE INTO team_members(event_id, team_id, user_id, joined_at) VALUES (?,?,?,?)",
                         (eid, t["id"], uid, now()))

    # Duplicate detection: same team plus same title or same repository.
    # The earliest submission stays canonical; later copies are kept as
    # evidence, marked duplicate_of, and left for the organizer to resolve.
    seen: dict[tuple, str] = {}
    projects = sorted(data.get("projects", []), key=lambda p: (p.get("submitted_at") or "", p["id"]))
    for p in projects:
        k_title = (p["team"], p["title"].strip().lower())
        k_repo = (p["team"], (p.get("repo_url") or "").strip().lower().rstrip("/"))
        dup_of = seen.get(k_title) or (seen.get(k_repo) if k_repo[1] else None)
        if not dup_of:
            # One live project per team: a second, different project from the
            # same team is also treated as a duplicate rather than dropped.
            dup_of = seen.get((p["team"], "__any__"))
        conn.execute(
            "INSERT INTO projects(id, event_id, team_id, track_id, title, tagline, description, repo_url, "
            "demo_video_url, live_url, status, duplicate_of, source, submitted_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], eid, p["team"], p.get("track"), p["title"], p.get("summary", ""), p.get("description", ""),
             p.get("repo_url", ""), p.get("demo_video_url", ""), p.get("live_url", ""), "submitted", dup_of,
             "import", p.get("submitted_at"), p.get("submitted_at") or now(), p.get("submitted_at") or now()))
        for tag in p.get("tags", []):
            conn.execute("INSERT OR IGNORE INTO project_tags(project_id, tag) VALUES (?,?)", (p["id"], tag))
        if dup_of:
            report["duplicates"].append({"project": p["id"], "duplicate_of": dup_of})
        else:
            seen[k_title] = seen[k_repo] = seen[(p["team"], "__any__")] = p["id"]

    known_projects = {p["id"] for p in data.get("projects", [])}
    for i, s in enumerate(data.get("scores", [])):
        jid = judge_uid.get(s["judge"])
        if not jid or s["project"] not in known_projects:
            report["skipped_scores"] += 1
            continue
        conn.execute("INSERT OR IGNORE INTO assignments(event_id, judge_id, project_id, source, created_at) "
                     "VALUES (?,?,?,?,?)", (eid, jid, s["project"], "import", now()))
        rid = f"rev_{s['judge']}_{s['project']}"
        conn.execute("INSERT INTO reviews(id, event_id, judge_id, project_id, status, comment, created_at, "
                     "updated_at, submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rid, eid, jid, s["project"], "submitted", s.get("comment", ""), now(), now(), now()))
        for k, v in s.get("criteria", {}).items():
            conn.execute("INSERT INTO review_scores(review_id, criterion_id, value) VALUES (?,?,?)",
                         (rid, crit_id[k], int(v)))

    counts = {k: len(data.get(k, [])) for k in ("tracks", "judges", "teams", "projects", "scores")}
    report["counts"] = counts
    audit.record(conn, actor=actor, event_id=eid, action="import.fixtures",
                 summary=f"Imported {counts['projects']} projects, {counts['judges']} judges and "
                         f"{counts['scores'] - report['skipped_scores']} reviews; "
                         f"{len(report['duplicates'])} possible duplicate{'' if len(report['duplicates']) == 1 else 's'} flagged",
                 detail=report)
    return report


def export_event(conn, event_id: str) -> dict:
    """The whole event in fixtures shape, plus the extra fields we store."""
    ev = one(conn, "SELECT * FROM events WHERE id = ?", (event_id,))
    out = {
        "format": "dogfood-fixtures/1 + scoreline extensions",
        "exported_at": now(),
        "event": {"id": ev["id"], "name": ev["name"], "tagline": ev["tagline"], "description": ev["description"],
                  "submissions_open": ev["submissions_open_at"], "submissions_close": ev["submissions_close_at"],
                  "results_published_at": ev["results_published_at"]},
        "tracks": [{"id": t["id"], "name": t["name"]} for t in
                   all_(conn, "SELECT * FROM tracks WHERE event_id = ? ORDER BY position", (event_id,))],
        "criteria": [{"key": c["key"], "name": c["name"], "guidance": c["guidance"], "weight": c["weight"],
                      "min": c["min_score"], "max": c["max_score"]} for c in
                     all_(conn, "SELECT * FROM criteria WHERE event_id = ? ORDER BY position", (event_id,))],
        "judges": [], "teams": [], "projects": [], "scores": [],
    }
    for j in all_(conn, "SELECT u.id, u.name, u.email FROM event_members em JOIN users u ON u.id = em.user_id "
                        "WHERE em.event_id = ? AND em.role = 'judge' ORDER BY u.id", (event_id,)):
        tr = [r["track_id"] for r in all_(conn, "SELECT track_id FROM judge_tracks WHERE event_id = ? AND user_id = ?",
                                          (event_id, j["id"]))]
        out["judges"].append({"id": j["id"], "name": j["name"], "email": j["email"], "tracks": tr})
    for t in all_(conn, "SELECT * FROM teams WHERE event_id = ? ORDER BY id", (event_id,)):
        members = [r["email"] for r in all_(conn, "SELECT u.email FROM team_members m JOIN users u ON u.id = m.user_id "
                                                  "WHERE m.team_id = ? ORDER BY m.joined_at, u.email", (t["id"],))]
        out["teams"].append({"id": t["id"], "name": t["name"], "members": members})
    for p in all_(conn, "SELECT * FROM projects WHERE event_id = ? ORDER BY id", (event_id,)):
        tags = [r["tag"] for r in all_(conn, "SELECT tag FROM project_tags WHERE project_id = ? ORDER BY tag", (p["id"],))]
        out["projects"].append({"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"],
                                "summary": p["tagline"], "description": p["description"], "repo_url": p["repo_url"],
                                "demo_video_url": p["demo_video_url"], "live_url": p["live_url"], "tags": tags,
                                "status": p["status"], "duplicate_of": p["duplicate_of"],
                                "submitted_at": p["submitted_at"]})
    for r in all_(conn, "SELECT * FROM reviews WHERE event_id = ? AND status = 'submitted' ORDER BY project_id, judge_id",
                  (event_id,)):
        vals = {x["key"]: x["value"] for x in all_(conn, "SELECT c.key, s.value FROM review_scores s JOIN criteria c "
                                                         "ON c.id = s.criterion_id WHERE s.review_id = ? ORDER BY c.position",
                                                         (r["id"],))}
        out["scores"].append({"judge": r["judge_id"], "project": r["project_id"], "criteria": vals,
                              "comment": r["comment"]})
    return out

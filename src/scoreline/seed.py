"""First-boot setup: schema, fixture import, demo accounts, demo sessions.

Run by the container entrypoint (`python -m scoreline.seed`) before the web server
starts. Idempotent: a second boot only refreshes the demo sessions.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import audit
from .db import connect, fmt_ts, get_meta, migrate, now, one, set_meta, transaction
from .importer import import_fixtures, invite_code
from .security import create_session, hash_password

# Fixed tokens so the committed .dogfood.toml works on every fresh clone.
# Disable with SCORELINE_DEMO_SESSIONS=0 before running a real event.
DEMO_SESSIONS = {
    "organizer": ("organizer@example.org", "demo-organizer-7f2a"),
    "judge_a": ("diego.herrera@example.org", "demo-judge-a-91bc"),
    "judge_b": ("jonas.vogel@example.org", "demo-judge-b-44de"),
    "participant": ("priya1@example.org", "demo-participant-2e88"),
    "admin": ("admin@example.org", "demo-admin-5c31"),
}


def fixtures_path() -> Path:
    return Path(os.environ.get("SCORELINE_FIXTURES", "data/fixtures.json"))


def seed(conn, *, verbose=True) -> None:
    migrate(conn)
    demo_password = os.environ.get("SCORELINE_DEMO_PASSWORD", "dogfood2026")
    first_boot = get_meta(conn, "seeded") is None
    if first_boot:
        pw = hash_password(demo_password)  # one hash shared by demo accounts; see README
        with transaction(conn):
            audit.record(conn, actor=None, action="system.init", summary="Database created")
            conn.execute("INSERT INTO users(id, email, name, password_hash, is_admin, created_at) VALUES (?,?,?,?,1,?)",
                         ("usr_admin", "admin@example.org", "Site admin", pw, now()))
            conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                         ("usr_organizer", "organizer@example.org", "Morgan Ellis", pw, now()))

            path = fixtures_path()
            if path.exists():
                data = json.loads(path.read_text())
                report = import_fixtures(conn, data, password_hash=pw)
                eid = data["event"]["id"]
                conn.execute("UPDATE events SET tagline = ?, description = ? WHERE id = ?",
                             ("The shared DOGFOOD fixture event, loaded on first boot.",
                              "Forty teams, eight tracks, thirty judges and a full set of scores, "
                              "including the awkward cases: a duplicate submission, a judge who gave "
                              "every project the same score, and batches nobody finished.", eid))
                conn.execute("INSERT INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                             (eid, "usr_organizer", "organizer", now()))
                for i, (name, reward) in enumerate([("Overall winner", "$800"), ("Runner-up", "$500"),
                                                    ("Third place", "$350")]):
                    conn.execute("INSERT INTO prizes(id, event_id, name, reward, position) VALUES (?,?,?,?,?)",
                                 (f"{eid}_prize_{i + 1}", eid, name, reward, i))
                if verbose:
                    print(f"[seed] imported {path}: {report['counts']} "
                          f"duplicates={report['duplicates']} skipped_scores={report['skipped_scores']}")
            elif verbose:
                print(f"[seed] no fixtures at {path}; starting empty")

            _practice_event(conn)
            set_meta(conn, "seeded", now())

    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE label = 'demo'")
        if os.environ.get("SCORELINE_DEMO_SESSIONS", "1") == "1":
            for role, (email, token) in DEMO_SESSIONS.items():
                user = one(conn, "SELECT id FROM users WHERE email = ?", (email,))
                if user:
                    create_session(conn, user["id"], token=token, label="demo", days=365)

    if verbose:
        _banner(demo_password)


def _practice_event(conn) -> None:
    """A second event that is open right now, so the full lifecycle can be
    demonstrated without editing dates."""
    t = datetime.now(timezone.utc).replace(microsecond=0, second=0)
    eid = "evt_practice"
    conn.execute(
        "INSERT INTO events(id, name, tagline, description, submissions_open_at, submissions_close_at, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (eid, "Practice Round", "Open for submissions now. Try the whole flow here.",
         "A sandbox event. Create a team, invite someone with the link, save a draft, "
         "submit before the deadline, then switch to the organizer account to assign judges.",
         fmt_ts(t - timedelta(days=1)), fmt_ts(t + timedelta(days=14)), now()))
    for i, name in enumerate(["Tools for organizers", "Open data", "Anything else"]):
        conn.execute("INSERT INTO tracks(id, event_id, name, position) VALUES (?,?,?,?)",
                     (f"{eid}_trk_{i + 1}", eid, name, i))
    for i, (key, name, w, g) in enumerate([
        ("impact", "Impact", 0.4, "Would the people it is for actually use it?"),
        ("execution", "Execution", 0.4, "Does it work end to end, and is it built to last?"),
        ("presentation", "Presentation", 0.2, "Could a stranger understand it from the page and the demo?"),
    ]):
        conn.execute("INSERT INTO criteria(id, event_id, key, name, guidance, weight, position) VALUES (?,?,?,?,?,?,?)",
                     (f"{eid}_crit_{key}", eid, key, name, g, w, i))
    conn.execute("INSERT INTO questions(id, event_id, prompt, required, position) VALUES (?,?,?,?,?)",
                 (f"{eid}_q1", eid, "What would you build next with another week?", 0, 0))
    conn.execute("INSERT INTO prizes(id, event_id, name, reward, position) VALUES (?,?,?,?,?)",
                 (f"{eid}_prize_1", eid, "Best practice run", "Bragging rights", 0))
    conn.execute("INSERT INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                 (eid, "usr_organizer", "organizer", now()))
    # Two fixture judges also judge the practice round, so a submission there
    # can be assigned and scored in the demo.
    for email in ("diego.herrera@example.org", "jonas.vogel@example.org"):
        u = one(conn, "SELECT id FROM users WHERE email = ?", (email,))
        if u:
            conn.execute("INSERT INTO event_members(event_id, user_id, role, created_at) VALUES (?,?,?,?)",
                         (eid, u["id"], "judge", now()))
    audit.record(conn, actor=None, event_id=eid, action="event.create", summary="Created the Practice Round event")


def _banner(pw: str) -> None:
    port = os.environ.get("PORT", "8080")
    lines = [
        "",
        "Scoreline is seeded.",
        f"Open http://localhost:{port}",
        f"Every demo account uses the password: {pw}",
        "  admin@example.org          administrator",
        "  organizer@example.org      organizer of both events",
        "  diego.herrera@example.org  judge (fixture judge jdg_24)",
        "  jonas.vogel@example.org    judge (fixture judge jdg_26)",
        "  priya1@example.org         participant, team NorthKiln",
    ]
    if os.environ.get("SCORELINE_DEMO_SESSIONS", "1") == "1":
        lines.append("Checker headers for .dogfood.toml:")
        for role, (_, token) in DEMO_SESSIONS.items():
            lines.append(f"  {role:<12} = \"Cookie: session={token}\"")
    print("\n".join(lines), flush=True)


def main() -> int:
    conn = connect()
    seed(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

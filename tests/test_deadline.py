"""The deadline holds in the application and, underneath it, in the database."""

import sqlite3

import pytest

from conftest import JUDGE_A, PARTICIPANT, csrf_for


def test_late_api_submission_refused_for_being_late(client):
    r = client.post("/api/events/evt_01/submissions", headers=PARTICIPANT,
                    json={"title": "dogfood-late-submission-probe", "summary": "probe"})
    assert r.status_code == 403
    assert "closed" in r.json()["error"]


def test_late_refusal_comes_before_body_validation(client):
    # Even an invalid body is refused as late, not as invalid.
    r = client.post("/api/events/evt_01/submissions", headers=PARTICIPANT, json={"nonsense": True})
    assert r.status_code == 403


def test_late_form_post_refused(client):
    r = client.post("/events/evt_01/project", headers=PARTICIPANT, data={"title": "late", "action": "draft"})
    assert r.status_code == 403


def test_database_trigger_blocks_edits_after_close(conn):
    with pytest.raises(sqlite3.IntegrityError, match="submissions_closed"):
        conn.execute("UPDATE projects SET title = 'sneaky' WHERE id = 'prj_01'")


def test_database_trigger_blocks_late_portal_insert(conn):
    with pytest.raises(sqlite3.IntegrityError, match="submissions_closed"):
        conn.execute("INSERT INTO projects(id, event_id, team_id, title, source, created_at, updated_at) "
                     "VALUES ('prj_late', 'evt_01', 'tm_01', 'late', 'portal', 'x', 'x')")


def test_open_event_accepts_draft_then_submit(client):
    # Participant is on a team in evt_01 only, so form a team in the practice round first.
    token = csrf_for(client, PARTICIPANT, "/events/evt_practice/team")
    r = client.post("/events/evt_practice/team", headers=PARTICIPANT, data={"csrf": token, "name": "StillTrail"})
    assert r.status_code == 303
    r = client.post("/api/events/evt_practice/submissions", headers=PARTICIPANT,
                    json={"title": "Kiln Notes", "tagline": "Notes for kilns", "repo_url": "https://example.org/k",
                          "track_id": "evt_practice_trk_1", "tags": ["Python", "sqlite"]})
    assert r.status_code == 201 and r.json()["status"] == "draft"
    # Submitting validates the fuller set of fields.
    r = client.post("/api/events/evt_practice/submissions", headers=PARTICIPANT,
                    json={"title": "Kiln Notes", "submit": True})
    assert r.status_code == 422
    r = client.post("/api/events/evt_practice/submissions", headers=PARTICIPANT,
                    json={"title": "Kiln Notes", "tagline": "Notes for kilns", "repo_url": "https://example.org/k",
                          "track_id": "evt_practice_trk_1", "submit": True})
    assert r.status_code == 201 and r.json()["status"] == "submitted"
    # Same team, same project: a second call updates rather than duplicates.
    listing = client.get("/api/events/evt_practice/projects").json()
    assert [p["title"] for p in listing["projects"]].count("Kiln Notes") == 1


def test_judges_cannot_compete_in_events_they_judge(client):
    # jdg_24 judges the practice round too.
    r = client.post("/api/events/evt_practice/submissions", headers=JUDGE_A, json={"title": "x"})
    assert r.status_code == 403


def test_trigger_forbids_judge_also_being_participant(conn):
    with pytest.raises(sqlite3.IntegrityError, match="judge cannot also be a participant"):
        conn.execute("INSERT INTO event_members(event_id, user_id, role, created_at) "
                     "VALUES ('evt_01', 'jdg_24', 'participant', 'x')")

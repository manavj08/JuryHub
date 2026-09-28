"""Fixture import edge cases, export round trip, assignment and the audit chain."""

import json
import sqlite3

import pytest

from conftest import ORG, csrf_for


def test_all_fixture_rows_imported(conn):
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM projects WHERE event_id = 'evt_01'").fetchone()[0] == 41
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE event_id = 'evt_01'").fetchone()[0] == 126
    assert conn.execute("SELECT COUNT(*) FROM teams WHERE event_id = 'evt_01'").fetchone()[0] == 40
    assert count("criteria") >= 3


def test_duplicate_submission_flagged_not_dropped(conn):
    row = conn.execute("SELECT duplicate_of FROM projects WHERE id = 'prj_41'").fetchone()
    assert row["duplicate_of"] == "prj_07"
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE project_id = 'prj_41'").fetchone()[0] == 4


def test_duplicate_hidden_from_gallery_and_ranking(client):
    html = client.get("/projects?q=Dry+Harbour").text
    assert html.count('href="/projects/prj_') == 1
    ranked = [p["project"] for p in client.get("/api/events/evt_01/results", headers=ORG).json()["projects"]]
    assert "prj_41" not in ranked and "prj_07" in ranked and len(ranked) == 40


def test_team_names_are_not_unique(conn):
    n = conn.execute("SELECT COUNT(*) FROM teams WHERE name = 'StillTrail'").fetchone()[0]
    assert n == 3


def test_gallery_is_server_rendered_with_fixture_titles(client):
    html = client.get("/projects").text
    assert "Glass Signal" in html and "Deep Compass" in html


def test_export_json_round_trips(client, conn):
    from scoreline.importer import import_fixtures
    data = client.get("/api/events/evt_01/export.json", headers=ORG).json()
    assert len(data["projects"]) == 41 and len(data["scores"]) == 126
    data["event"]["id"] = "evt_copy"
    for t in data["tracks"]:
        t["id"] = "c_" + t["id"]
    for p in data["projects"]:
        p["track"] = "c_" + p["track"]
        p["id"] = "c_" + p["id"]
        p["team"] = "c_" + p["team"]
    for t in data["teams"]:
        t["id"] = "c_" + t["id"]
    for j in data["judges"]:
        j["tracks"] = ["c_" + t for t in j["tracks"]]
    for s in data["scores"]:
        s["project"] = "c_" + s["project"]
    conn.execute("BEGIN")
    report = import_fixtures(conn, data)
    conn.execute("ROLLBACK")
    assert report["skipped_scores"] == 0
    assert len(report["duplicates"]) == 1
    # The rubric, weights included, survives the trip.
    conn.execute("BEGIN")
    data["criteria"][0]["weight"] = 0.9
    data["event"]["id"] = "evt_copy2"
    for t in data["tracks"]:
        t["id"] = "d" + t["id"]
    for p in data["projects"]:
        p["id"], p["team"], p["track"] = "d" + p["id"], "d" + p["team"], "d" + p["track"]
    for t in data["teams"]:
        t["id"] = "d" + t["id"]
    for j in data["judges"]:
        j["tracks"] = ["d" + x for x in j["tracks"]]
    for s in data["scores"]:
        s["project"] = "d" + s["project"]
    import_fixtures(conn, data)
    w = conn.execute("SELECT weight FROM criteria WHERE event_id = 'evt_copy2' AND key = ?",
                     (data["criteria"][0]["key"],)).fetchone()["weight"]
    conn.execute("ROLLBACK")
    assert w == 0.9


def test_auto_assign_respects_tracks_and_reaches_target(client, conn):
    token = csrf_for(client, ORG, "/manage/evt_01/judges")
    r = client.post("/manage/evt_01/judges/auto-assign", headers=ORG, data={"csrf": token})
    assert r.status_code == 303
    thin = conn.execute(
        "SELECT p.id FROM projects p WHERE p.event_id = 'evt_01' AND p.duplicate_of IS NULL "
        "AND (SELECT COUNT(*) FROM assignments a WHERE a.project_id = p.id) < 3").fetchall()
    assert thin == []
    off_track = conn.execute(
        "SELECT a.judge_id, a.project_id FROM assignments a JOIN projects p ON p.id = a.project_id "
        "WHERE a.event_id = 'evt_01' AND NOT EXISTS (SELECT 1 FROM judge_tracks jt "
        "WHERE jt.user_id = a.judge_id AND jt.track_id = p.track_id)").fetchall()
    assert off_track == []


def test_assignment_trigger_refuses_off_track_judge(conn):
    # jdg_01 only judges Accessibility (trk_03); prj_01 is Security.
    with pytest.raises(sqlite3.IntegrityError, match="track"):
        conn.execute("INSERT INTO assignments(event_id, judge_id, project_id, source, created_at) "
                     "VALUES ('evt_01', 'jdg_01', 'prj_01', 'manual', 'x')")


def test_audit_chain_verifies(conn):
    from scoreline.audit import verify_chain
    result = verify_chain(conn)
    assert result["ok"] and result["count"] > 3


def test_audit_log_is_append_only(conn):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_log SET summary = 'nothing happened' WHERE seq = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_tampering_breaks_the_chain(conn):
    from scoreline.audit import verify_chain
    conn.execute("BEGIN")
    conn.execute("DROP TRIGGER audit_no_update")          # what an attacker with file access could do
    conn.execute("UPDATE audit_log SET summary = 'edited' WHERE seq = 2")
    broken = verify_chain(conn)
    conn.execute("ROLLBACK")
    assert not broken["ok"] and broken["broken_at"] == 2


def test_publish_locks_scores_and_reveals_results(client, conn):
    token = csrf_for(client, ORG, "/manage/evt_01/results")
    assert client.post("/manage/evt_01/results/publish", headers=ORG, data={"csrf": token}).status_code == 303
    assert client.get("/events/evt_01/results").status_code == 200
    public = client.get("/api/events/evt_01/results").json()
    assert "judges" not in public and len(public["projects"]) == 40
    with pytest.raises(sqlite3.IntegrityError, match="results_published"):
        conn.execute("UPDATE review_scores SET value = 1 WHERE review_id = 'rev_jdg_24_prj_12'")
    snap = conn.execute("SELECT detail FROM audit_log WHERE action = 'results.publish'").fetchone()
    assert len(json.loads(snap["detail"])["ranking"]) == 40

"""Role isolation lives in the backend. These are the curl-level checks."""

from conftest import ADMIN, JUDGE_A, JUDGE_B, ORG, PARTICIPANT


def test_judge_reads_own_scores(client):
    r = client.get("/api/judge/scores", headers=JUDGE_A)
    assert r.status_code == 200
    body = r.json()
    assert body["judge"] == "jdg_24"
    assert len(body["reviews"]) == 11
    assert all(rv["weighted"] is not None for rv in body["reviews"])


def test_judge_cannot_read_peer_scores(client):
    assert client.get("/api/judge/scores?judge=jdg_24", headers=JUDGE_B).status_code == 403


def test_peer_probe_for_unknown_judge_is_also_refused(client):
    # Same answer whether or not the judge exists, so ids cannot be enumerated.
    assert client.get("/api/judge/scores?judge=nobody", headers=JUDGE_B).status_code == 403


def test_participant_is_not_a_judge(client):
    assert client.get("/api/judge/scores", headers=PARTICIPANT).status_code == 403


def test_anonymous_api_gets_401_json_not_a_redirect(client):
    r = client.get("/api/judge/scores")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")


def test_anonymous_page_redirects_to_login(client):
    r = client.get("/manage/evt_01")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_organizer_can_read_a_judges_scores_and_it_is_audited(client, conn):
    assert client.get("/api/judge/scores?judge=jdg_26", headers=ORG).status_code == 200
    row = conn.execute("SELECT * FROM audit_log WHERE action = 'scores.read' ORDER BY seq DESC").fetchone()
    assert row and row["subject"] == "jdg_26"


def test_judge_cannot_open_unassigned_scoresheet(client):
    # prj_12 is assigned to jdg_24 (judge A) but not to jdg_26 (judge B).
    assert client.get("/judge/evt_01/prj_12", headers=JUDGE_B).status_code == 403
    assert client.get("/judge/evt_01/prj_12", headers=JUDGE_A).status_code == 200


def test_judge_cannot_write_review_for_unassigned_project(client):
    r = client.put("/api/judge/reviews/evt_01/prj_12", headers=JUDGE_B,
                   json={"scores": {"functionality": 5, "quality": 5, "innovation": 5}, "submit": True})
    assert r.status_code == 403


def test_submitted_review_cannot_be_rewritten_by_judge(client):
    r = client.put("/api/judge/reviews/evt_01/prj_12", headers=JUDGE_A,
                   json={"scores": {"functionality": 1, "quality": 1, "innovation": 1}})
    assert r.status_code == 409


def test_only_organizers_export(client):
    for who in (JUDGE_A, PARTICIPANT):
        assert client.get("/api/events/evt_01/export/reviews.csv", headers=who).status_code == 403
    r = client.get("/api/events/evt_01/export/reviews.csv", headers=ORG)
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    assert header.startswith("project_id,project_title,judge_id")


def test_results_hidden_until_published(client):
    assert client.get("/api/events/evt_01/results", headers=PARTICIPANT).status_code == 403
    assert client.get("/api/events/evt_01/results").status_code == 401
    assert client.get("/events/evt_01/results").status_code == 404
    assert client.get("/api/events/evt_01/results", headers=ORG).status_code == 200


def test_organizer_console_refuses_judges_and_participants(client):
    for who in (JUDGE_A, PARTICIPANT):
        assert client.get("/manage/evt_01", headers=who).status_code == 403
        assert client.get("/api/events/evt_01/progress", headers=who).status_code == 403
    assert client.get("/manage/evt_01", headers=ADMIN).status_code == 200


def test_forms_without_csrf_token_are_refused(client):
    r = client.post("/manage/evt_01/results/publish", headers=ORG, data={"csrf": "wrong"})
    assert r.status_code == 403

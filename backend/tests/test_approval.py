"""Phase 7 approval workflow tests: PATCH /api/briefings/{id}/approve|reject,
the edit-and-recheck route, and the review queue. Uses the `api_client`/
`fake_db` fixtures -- no real Prisma/LLM calls (compliance_verdict.evaluate
is monkeypatched so the edit-recheck test doesn't call a live judge)."""

import os

os.environ.setdefault("MOCK_DB", "1")

from tests.conftest import FakeBriefing, FakeClientProfile  # noqa: E402


def _seed_client_and_briefing(fake_db, status="needs_human_review", violations=None, owner="owner-A"):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId=owner)
    briefing = FakeBriefing("b1", "c1", status=status, complianceViolations=violations or [])
    fake_db.briefing._rows.append(briefing)
    return briefing


def test_approve_requires_override_reason_when_violations_unresolved(api_client, fake_db):
    _seed_client_and_briefing(fake_db, violations=[{"rule": "DIRECTIVE_ADVICE"}])
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/approve", json={})

    assert res.status_code == 400
    assert "override_reason" in res.json()["detail"]


def test_approve_with_override_reason_succeeds_and_is_audited(api_client, fake_db):
    _seed_client_and_briefing(fake_db, violations=[{"rule": "DIRECTIVE_ADVICE"}])
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/approve", json={"override_reason": "Advisor manually verified wording."})

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["overrideReason"] == "Advisor manually verified wording."
    audit_actions = [a["action"] for a in fake_db.auditlog.created]
    assert "briefing_approved" in audit_actions
    logged = next(a for a in fake_db.auditlog.created if a["action"] == "briefing_approved")
    assert logged["metadata"]["override_reason"] == "Advisor manually verified wording."


def test_approve_clean_briefing_needs_no_override_reason(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="compliant", violations=[])
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/approve", json={})

    assert res.status_code == 200
    assert res.json()["status"] == "approved"


def test_approve_already_approved_briefing_rejected(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="approved", violations=[])
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/approve", json={})
    assert res.status_code == 400


def test_approve_is_owner_only_cross_tenant_404(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="compliant")
    client = api_client(user_id="attacker-B")

    res = client.patch("/api/briefings/b1/approve", json={})

    assert res.status_code == 404


def test_reject_sets_terminal_status_and_is_audited(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="needs_human_review")
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/reject", json={"reason": "Too aggressive in tone."})

    assert res.status_code == 200
    assert res.json()["status"] == "rejected"
    assert any(a["action"] == "briefing_rejected" for a in fake_db.auditlog.created)


def test_reject_twice_is_rejected_terminal_state(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="rejected")
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1/reject", json={})
    assert res.status_code == 400


def test_edit_and_recheck_reruns_compliance_engine(api_client, fake_db, monkeypatch):
    import api
    from compliance.verdict import CombinedVerdict

    _seed_client_and_briefing(fake_db, status="needs_human_review", violations=[{"rule": "DIRECTIVE_ADVICE"}])

    monkeypatch.setattr(
        api.compliance_verdict,
        "evaluate",
        lambda draft, run_id=None: CombinedVerdict(compliant=True, violations=[], critic_model="rules+fake"),
    )
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1", json={"content": "A cleaned-up, compliant draft."})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "compliant"
    assert body["complianceViolations"] == []
    assert body["content"] == "A cleaned-up, compliant draft."
    assert len(fake_db.briefingrevision.created) == 1


def test_edit_cannot_touch_a_finalized_briefing(api_client, fake_db):
    _seed_client_and_briefing(fake_db, status="approved")
    client = api_client(user_id="owner-A")

    res = client.patch("/api/briefings/b1", json={"content": "trying to sneak an edit in"})
    assert res.status_code == 400


def test_review_queue_lists_only_needs_human_review_for_owned_clients(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.clientprofile._store["c2"] = FakeClientProfile("c2", clerkUserId="other-user")
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="needs_human_review"))
    fake_db.briefing._rows.append(FakeBriefing("b2", "c1", status="compliant"))
    fake_db.briefing._rows.append(FakeBriefing("b3", "c2", status="needs_human_review"))

    client = api_client(user_id="owner-A")
    res = client.get("/api/review-queue")

    assert res.status_code == 200
    ids = {b["id"] for b in res.json()}
    assert ids == {"b1"}

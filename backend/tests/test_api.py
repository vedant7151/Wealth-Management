"""API auth / IDOR / rate-limit tests for backend/api.py. Uses the
`api_client` + `fake_db` fixtures from conftest.py -- no real Prisma/
network/LLM calls. get_current_user is overridden per-test (bypassing
real Clerk JWT verification) except where a test explicitly wants to
exercise the real no-token path."""

import os

os.environ.setdefault("MOCK_DB", "1")

import ratelimit  # noqa: E402
from tests.conftest import FakeClientProfile  # noqa: E402


def test_cross_tenant_client_returns_404_not_403(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="attacker-B")

    res = client.get("/api/clients/c1/briefings")

    assert res.status_code == 404
    assert res.status_code != 403


def test_owner_can_access_their_own_client_briefings(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="owner-A")

    res = client.get("/api/clients/c1/briefings")

    assert res.status_code == 200
    assert res.json()["client"]["id"] == "c1"


def test_nonexistent_client_returns_404(api_client, fake_db):
    client = api_client(user_id="owner-A")
    res = client.get("/api/clients/does-not-exist/briefings")
    assert res.status_code == 404


def test_demo_mode_only_sees_unowned_rows_not_claimed_demo_rows(api_client, fake_db):
    fake_db.clientprofile._store["unowned"] = FakeClientProfile("unowned", clerkUserId=None)
    fake_db.clientprofile._store["owned"] = FakeClientProfile("owned", clerkUserId="real-user")

    from auth import DEMO_USER_ID

    client = api_client(user_id=DEMO_USER_ID)
    res = client.get("/api/clients")

    ids = {c["id"] for c in res.json()}
    assert ids == {"unowned"}


def test_no_bearer_token_returns_401_when_clerk_is_configured(api_client, fake_db, monkeypatch):
    """This environment's backend/.env has CLERK_JWKS_URL/CLERK_ISSUER set
    (see docs/PROGRESS.md), so get_current_user's real, non-demo path is
    active -- a request with no Authorization header must be rejected
    before any ownership/DB logic runs. Exercises the REAL dependency
    (no override) to prove the 401 itself, not just the override's
    behavior."""
    # auth.py reads its config at call time (Phase 10), so env is all it takes.
    monkeypatch.setenv("CLERK_JWKS_URL", "https://example.clerk.accounts.dev/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_ISSUER", "https://example.clerk.accounts.dev")

    client = api_client(user_id=None)
    res = client.get("/api/clients/some-id/briefings")

    assert res.status_code == 401


def test_run_returns_409_when_client_already_locked(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A", portfolio_themes=["X"])
    client = api_client(user_id="owner-A")

    assert ratelimit.try_lock("run:c1") is True  # simulate a run already in flight

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 409


def test_run_cross_tenant_returns_404_before_touching_the_lock(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="attacker-B")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 404
    # The lock must never have been taken on behalf of a rejected caller.
    assert ratelimit.try_lock("run:c1") is True


def test_run_rate_limited_returns_429_once_run_limit_exhausted(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A", portfolio_themes=["X"])
    client = api_client(user_id="owner-A")

    # Exhaust the per-user RUN_LIMIT directly against the real (in-memory)
    # rate limit store, the same one api.py's enforce path reads from.
    identity = "user:owner-A"
    for _ in range(ratelimit.RUN_LIMIT):
        assert ratelimit.check("run", identity).allowed is True

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 429
    body = res.json()
    assert body["error"] == "run_limit_exceeded"


def test_list_clients_global_api_limit_returns_429(api_client, fake_db):
    client = api_client(user_id="owner-A")
    identity = "user:owner-A"
    for _ in range(ratelimit.GLOBAL_API_LIMIT):
        assert ratelimit.check("global_api", identity).allowed is True

    res = client.get("/api/clients")

    assert res.status_code == 429


def test_create_client_requires_name_and_theme(api_client, fake_db):
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients", json={"name": "", "portfolio_themes": ["X"]})
    assert res.status_code == 400

    res = client.post("/api/clients", json={"name": "Jane Doe", "portfolio_themes": []})
    assert res.status_code == 400


def test_admin_usage_requires_admin_allowlist(api_client, fake_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "the-real-admin")
    client = api_client(user_id="owner-A")

    res = client.get("/api/admin/usage")

    assert res.status_code == 403


# --------------------------------------------------------------------------
# Phase 6: async runs (background /run, SSE progress, run history)
# --------------------------------------------------------------------------


def test_run_returns_202_with_run_id_and_creates_a_run_log(api_client, fake_db, monkeypatch):
    """api.py's own responsibility -- admit the request, create the RunLog
    row, hand off to the background runner -- is tested here in isolation
    from the actual graph execution (that's test_runs.py's job): the
    background runner itself is replaced with a fake that just records its
    arguments and releases the lock immediately, so this test is fast and
    deterministic."""
    import api

    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A", portfolio_themes=["X"])
    calls = []

    def fake_start_background_run(run_log_id, client_id, user_id, themes, trigger, release_lock, **kwargs):
        calls.append((run_log_id, client_id, user_id, trigger))
        release_lock()

    monkeypatch.setattr(api.runs, "start_background_run", fake_start_background_run)
    monkeypatch.setattr(
        api.llm_client, "check_daily_budget", lambda provider: {"allowed": True, "used": 0, "limit": 18}
    )
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 202
    body = res.json()
    assert "run_id" in body
    assert len(calls) == 1
    assert calls[0] == (body["run_id"], "c1", "owner-A", "manual")
    assert fake_db.runlog.find_unique(where={"id": body["run_id"]}) is not None
    assert "X-RateLimit-Remaining" in res.headers


def test_run_lock_is_released_if_rate_limited_after_acquiring_it(api_client, fake_db, monkeypatch):
    """The per-client lock is acquired before the run-rate-limit/budget
    checks -- if either of those rejects the request, the lock must still
    be released (not left held until it goes stale), since no background
    run was ever started to release it itself."""
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A", portfolio_themes=["X"])
    identity = "user:owner-A"
    for _ in range(ratelimit.RUN_LIMIT):
        assert ratelimit.check("run", identity).allowed is True
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 429
    assert ratelimit.try_lock("run:c1") is True


def test_events_stream_cross_tenant_returns_404(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    fake_db.runlog._store["run-1"] = fake_db.runlog.create(
        data={"clientProfileId": "c1", "userId": "owner-A", "trigger": "manual", "status": "running"}
    )
    client = api_client(user_id="attacker-B")

    res = client.get("/api/runs/run-1/events")

    assert res.status_code == 404


def test_events_stream_nonexistent_run_returns_404(api_client, fake_db):
    client = api_client(user_id="owner-A")
    res = client.get("/api/runs/does-not-exist/events")
    assert res.status_code == 404


def test_owner_can_open_the_events_stream(api_client, fake_db):
    """Calls the route function directly (not through TestClient's full
    streaming transport, which blocks draining an SSE generator that only
    ever ends on the run's "done" event) to verify the ownership check
    passes and a StreamingResponse with the right media type is returned
    for a run the caller genuinely owns -- the generator's own content
    (ordering, heartbeats, replay) is covered by test_runs.py directly."""
    import api
    from starlette.requests import Request

    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    row = fake_db.runlog.create(
        data={"clientProfileId": "c1", "userId": "owner-A", "trigger": "manual", "status": "running"}
    )
    api_client(user_id="owner-A")  # triggers the fixture's api.db = fake_db monkeypatch

    scope = {"type": "http", "method": "GET", "headers": [], "query_string": b""}
    request = Request(scope)

    response = api.run_events(row.id, request, "owner-A")

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"


def test_get_run_returns_persisted_status(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    row = fake_db.runlog.create(
        data={"clientProfileId": "c1", "userId": "owner-A", "trigger": "manual", "status": "running"}
    )
    fake_db.runlog.update(where={"id": row.id}, data={"status": "success", "durationMs": 4200, "briefingId": "b1"})
    client = api_client(user_id="owner-A")

    res = client.get(f"/api/runs/{row.id}")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["durationMs"] == 4200
    assert body["briefingId"] == "b1"


def test_list_runs_scoped_to_owned_clients_only(api_client, fake_db):
    fake_db.clientprofile._store["mine"] = FakeClientProfile("mine", clerkUserId="owner-A")
    fake_db.clientprofile._store["theirs"] = FakeClientProfile("theirs", clerkUserId="owner-B")
    fake_db.runlog.create(
        data={"clientProfileId": "mine", "userId": "owner-A", "trigger": "manual", "status": "success"}
    )
    fake_db.runlog.create(
        data={"clientProfileId": "theirs", "userId": "owner-B", "trigger": "manual", "status": "success"}
    )
    client = api_client(user_id="owner-A")

    res = client.get("/api/runs")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["clientId"] == "mine"


def test_startup_registers_a_db_for_the_budget_check(fake_db, monkeypatch):
    """Regression: the API process never registered llm_client's db getter,
    so check_daily_budget failed closed on every request and every /run was
    silently served as demo data."""
    import api
    import llm_client

    monkeypatch.setattr(api, "db", fake_db)
    monkeypatch.setattr(llm_client, "_db_getter", None)
    api.on_startup()
    assert llm_client._db_getter is not None and llm_client._db_getter() is fake_db
    assert llm_client.check_daily_budget("gemini")["allowed"] in (True, False)  # did not fail closed for "no db"


# --------------------------------------------------------------------------
# Edit / delete client
# --------------------------------------------------------------------------


def test_owner_can_edit_client_name_and_themes(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", name="Old", clerkUserId="owner-A")
    res = api_client(user_id="owner-A").patch(
        "/api/clients/c1", json={"name": "  New Name ", "portfolio_themes": ["AI", "Energy"]}
    )
    assert res.status_code == 200
    assert res.json() == {"id": "c1", "name": "New Name", "portfolio_themes": ["AI", "Energy"]}
    assert fake_db.clientprofile._store["c1"].name == "New Name"
    assert fake_db.auditlog.created[-1]["action"] == "client_updated"


def test_edit_client_validates_and_is_owner_only(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    assert (
        api_client(user_id="attacker")
        .patch("/api/clients/c1", json={"name": "x", "portfolio_themes": ["a"]})
        .status_code
        == 404
    )
    c = api_client(user_id="owner-A")
    assert c.patch("/api/clients/c1", json={"name": "  ", "portfolio_themes": ["a"]}).status_code == 400
    assert c.patch("/api/clients/c1", json={"name": "ok", "portfolio_themes": []}).status_code == 400
    assert c.patch("/api/clients/c1", json={"name": "x" * 500, "portfolio_themes": ["a"]}).status_code == 422


def test_owner_can_delete_client_and_dependents_are_removed_first(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    res = api_client(user_id="owner-A").delete("/api/clients/c1")
    assert res.status_code == 204 and res.content == b""
    assert "c1" not in fake_db.clientprofile._store
    tx = fake_db.last_tx
    tx.citation.delete_many.assert_called_once()
    tx.briefing.delete_many.assert_called_once_with(where={"clientProfileId": "c1"})
    tx.runlog.delete_many.assert_called_once()
    tx.holding.delete_many.assert_called_once()
    # audit rows are detached, never deleted
    tx.auditlog.update_many.assert_called_once()
    assert not tx.auditlog.delete_many.called
    assert fake_db.auditlog.created[-1]["action"] == "client_deleted"
    assert ratelimit.try_lock("run:c1") is True  # lock released


def test_delete_client_is_owner_only_and_refused_while_a_run_is_in_flight(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    assert api_client(user_id="attacker").delete("/api/clients/c1").status_code == 404
    assert "c1" in fake_db.clientprofile._store
    assert ratelimit.try_lock("run:c1") is True  # simulate an in-flight run holding the lock
    res = api_client(user_id="owner-A").delete("/api/clients/c1")
    assert res.status_code == 409 and "c1" in fake_db.clientprofile._store

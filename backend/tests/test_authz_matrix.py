"""Authorization matrix: EVERY route x {no token, other tenant, owner, admin,
demo mode}, executed. A route lacking an ownership/role check fails here.

Expectations:
  - no token (Clerk configured)  -> 401 on every /api/* route; /healthz, /readyz open
  - other tenant                 -> 404 on every resource route (never 403/200),
                                    empty list on list routes, 403 on admin route
  - owner                        -> never 401/403/404
  - admin (ADMIN_USER_IDS)       -> admin route 200; NOT a superuser on tenant data (404)
  - demo mode (auth unset)       -> sees only UNOWNED rows; a Clerk-owned row is 404
"""

import pytest

import api
import runs
from conftest import FakeBriefing, FakeClientProfile

OWNER, OTHER, ADMIN = "owner-A", "attacker-B", "admin-C"

# (id, method, path, kwargs, kind)  kind: resource | list | admin | me
ROUTES = [
    ("clients-list", "GET", "/api/clients", {}, "list"),
    ("clients-create", "POST", "/api/clients", {"json": {"name": "N", "portfolio_themes": ["t"]}}, "me"),
    ("client-patch", "PATCH", "/api/clients/c1", {"json": {"name": "N2", "portfolio_themes": ["t"]}}, "resource"),
    ("client-briefings", "GET", "/api/clients/c1/briefings", {}, "resource"),
    ("client-holdings-get", "GET", "/api/clients/c1/holdings", {}, "resource"),
    (
        "client-holdings-post",
        "POST",
        "/api/clients/c1/holdings",
        {"files": {"file": ("h.csv", b"ticker,sector,weight\nNVDA,Semis,10\n", "text/csv")}},
        "resource",
    ),
    ("client-run", "POST", "/api/clients/c1/run", {}, "resource"),
    ("run-get", "GET", "/api/runs/r1", {}, "resource"),
    ("run-events", "GET", "/api/runs/r1/events", {}, "resource"),
    ("runs-list", "GET", "/api/runs", {}, "list"),
    ("briefing-approve", "PATCH", "/api/briefings/b1/approve", {"json": {"override_reason": "reviewed"}}, "resource"),
    ("briefing-reject", "PATCH", "/api/briefings/b1/reject", {"json": {"reason": "no"}}, "resource"),
    ("briefing-edit", "PATCH", "/api/briefings/b1", {"json": {"content": "edited text"}}, "resource"),
    ("briefing-pdf", "GET", "/api/briefings/b1/pdf", {}, "resource"),
    ("briefing-deliver", "POST", "/api/briefings/b1/deliver", {"json": {"recipient": "a@allowed.test"}}, "resource"),
    ("review-queue", "GET", "/api/review-queue", {}, "list"),
    ("me-limits", "GET", "/api/me/limits", {}, "me"),
    ("admin-usage", "GET", "/api/admin/usage", {}, "admin"),
    # client DELETE last: it removes the fixture client
    ("client-delete", "DELETE", "/api/clients/c1", {}, "resource"),
]


def _routes_covered():
    return {(m, p) for _, m, p, _, _ in ROUTES}


def test_matrix_covers_every_api_route():
    declared = set()
    for r in api.app.routes:
        if getattr(r, "path", "").startswith("/api"):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                declared.add((m, r.path))
    norm = {
        (m, p.replace("c1", "{client_id}").replace("r1", "{run_id}").replace("b1", "{briefing_id}"))
        for m, p in _routes_covered()
    }
    assert declared <= norm, f"routes missing from the authz matrix: {sorted(declared - norm)}"


@pytest.fixture
def world(fake_db, monkeypatch):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId=OWNER)
    fake_db.briefing._rows.append(FakeBriefing("b1", "c1", status="needs_human_review"))
    fake_db.runlog._store["r1"] = fake_db.runlog.create(
        data={"clientProfileId": "c1", "userId": OWNER, "trigger": "manual", "status": "success"}
    )
    fake_db.runlog._store["r1"].id = "r1"
    runs.publish("r1", "done", status="success", duration_ms=1, briefing_id=None, error_code=None)  # SSE ends at once
    monkeypatch.setattr(api.runs, "start_background_run", lambda *a, **k: k["release_lock"]())
    monkeypatch.setattr(
        api.llm_client, "check_daily_budget", lambda provider: {"allowed": True, "used": 0, "limit": 18}
    )
    monkeypatch.setattr(api.delivery, "email_delivery_enabled", lambda: True)
    monkeypatch.setattr(api.delivery, "recipient_domain_allowed", lambda r: True)
    monkeypatch.setenv("ADMIN_USER_IDS", ADMIN)
    return fake_db


def _call(client, method, path, kwargs):
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize("rid,method,path,kwargs,kind", ROUTES, ids=[r[0] for r in ROUTES])
def test_no_token_is_401_when_auth_is_configured(rid, method, path, kwargs, kind, world, api_client, monkeypatch):
    monkeypatch.setenv("CLERK_JWKS_URL", "https://x.clerk.accounts.dev/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_ISSUER", "https://x.clerk.accounts.dev")
    res = _call(api_client(user_id=None), method, path, kwargs)
    assert res.status_code == 401, (rid, res.status_code)


@pytest.mark.parametrize("rid,method,path,kwargs,kind", ROUTES, ids=[r[0] for r in ROUTES])
def test_other_tenant_never_reaches_another_tenants_data(rid, method, path, kwargs, kind, world, api_client):
    res = _call(api_client(user_id=OTHER), method, path, kwargs)
    if kind == "resource":
        assert res.status_code == 404, (rid, res.status_code)  # 404, never 403/200: existence not confirmed
    elif kind == "admin":
        assert res.status_code == 403, (rid, res.status_code)
    elif kind == "list":
        assert res.status_code == 200 and res.json() == [], (rid, res.status_code, res.text[:80])
    else:
        assert res.status_code in (200, 201), (rid, res.status_code)
    # and nothing of the owner's was touched
    assert "c1" in world.clientprofile._store


@pytest.mark.parametrize("rid,method,path,kwargs,kind", ROUTES, ids=[r[0] for r in ROUTES])
def test_owner_is_allowed(rid, method, path, kwargs, kind, world, api_client):
    if kind == "admin":
        pytest.skip("owner is not an admin; covered by the admin test")
    res = _call(api_client(user_id=OWNER), method, path, kwargs)
    assert res.status_code not in (401, 403, 404), (rid, res.status_code, res.text[:120])


def test_admin_route_requires_admin_and_admin_is_not_a_tenant_superuser(world, api_client):
    assert api_client(user_id=OWNER).get("/api/admin/usage").status_code == 403
    assert api_client(user_id=ADMIN).get("/api/admin/usage").status_code == 200
    admin = api_client(user_id=ADMIN)
    for _, method, path, kwargs, kind in ROUTES:
        if kind == "resource" and method != "DELETE":
            assert _call(admin, method, path, kwargs).status_code == 404, (method, path)


def test_demo_mode_sees_only_unowned_rows(world, api_client, monkeypatch):
    world.clientprofile._store["demo1"] = FakeClientProfile("demo1", clerkUserId=None)
    client = api_client(user_id="demo-user")  # what auth returns when Clerk is unset
    assert client.get("/api/clients/c1/briefings").status_code == 404  # Clerk-owned: invisible
    assert client.get("/api/clients/demo1/briefings").status_code == 200
    assert [c["id"] for c in client.get("/api/clients").json()] == ["demo1"]


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health_endpoints_are_open_and_leak_nothing_sensitive(path, world, api_client):
    res = api_client(user_id=None).get(path)
    assert res.status_code in (200, 503)
    assert "DATABASE_URL" not in res.text and "postgres" not in res.text.lower()

"""Phase 10: request-id, security headers, body limit, uniform errors, metrics,
pagination caps, input validation."""

import json
import re

import pytest

import api
from conftest import FakeClientProfile


@pytest.fixture
def client(api_client, fake_db):
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A", portfolio_themes=["AI"])
    return api_client(user_id="owner-A")


def test_every_response_has_request_id_and_security_headers(client):
    res = client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{32}", res.headers["x-request-id"])
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert "strict-transport-security" not in res.headers  # plain HTTP in dev: no HSTS


def test_hsts_only_over_tls(client):
    res = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in res.headers["strict-transport-security"]


def test_inbound_request_id_is_propagated_but_junk_is_replaced(client):
    assert (
        client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"}).headers["x-request-id"] == "trace-abc-12345"
    )
    bad = client.get("/healthz", headers={"X-Request-ID": "x\ny<script>"})
    assert bad.headers["x-request-id"] != "x\ny<script>"


def test_api_responses_are_not_cacheable(client):
    assert client.get("/api/clients").headers["cache-control"] == "no-store"


def test_error_schema_is_uniform_and_carries_request_id(client):
    res = client.get("/api/clients/does-not-exist/briefings")
    body = res.json()
    assert res.status_code == 404
    assert set(body) >= {"error", "message", "detail", "request_id"}
    assert body["error"] == "not_found"
    assert body["request_id"] == res.headers["x-request-id"]


def test_unhandled_exception_returns_500_without_stack_trace(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail /srv/app/api.py line 42")

    monkeypatch.setattr(api.db.clientprofile, "find_many", boom)
    from fastapi.testclient import TestClient

    res = TestClient(api.app, raise_server_exceptions=False).get("/api/clients")
    # (TestClient above has no dependency overrides cleared: the fixture's override is still active)
    assert res.status_code == 500
    body = res.json()
    assert body["error"] == "internal_error" and body["message"] == "Internal server error"
    assert "secret internal detail" not in res.text and "Traceback" not in res.text
    assert body["request_id"] == res.headers["x-request-id"]


def test_validation_error_is_422_with_field_list_and_no_input_echo(client):
    res = client.post("/api/clients", json={"name": "x" * 500, "portfolio_themes": ["AI"]})
    body = res.json()
    assert res.status_code == 422 and body["error"] == "validation_error"
    assert body["fields"][0]["field"].endswith("name")
    assert "x" * 50 not in res.text


def test_oversized_declared_body_is_rejected_413(client):
    res = client.post(
        "/api/clients", content=b"{" + b" " * (2 * 1024 * 1024) + b"}", headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 413 and res.json()["error"] == "payload_too_large"


def test_oversized_chunked_body_is_rejected_413(client):
    def chunks():
        for _ in range(40):
            yield b" " * 65536  # no Content-Length -> counted as it streams

    res = client.post("/api/clients", content=chunks(), headers={"Content-Type": "application/json"})
    assert res.status_code == 413


def test_pagination_cap_is_enforced(client):
    assert client.get("/api/clients?limit=1000").status_code == 422
    assert client.get("/api/clients?limit=0").status_code == 422
    assert client.get("/api/clients?offset=-1").status_code == 422
    assert client.get("/api/clients?limit=100").status_code == 200
    assert client.get("/api/runs?limit=101").status_code == 422
    assert client.get("/api/clients/c1/briefings?limit=500").status_code == 422


def test_pagination_slices_results(client, fake_db):
    for i in range(5):
        fake_db.clientprofile._store[f"x{i}"] = FakeClientProfile(f"x{i}", clerkUserId="owner-A")
    assert len(client.get("/api/clients?limit=2").json()) == 2
    assert len(client.get("/api/clients?limit=100&offset=4").json()) == 2  # 6 total, skip 4


def test_name_and_themes_are_unicode_normalized_and_cleaned(client):
    res = client.post(
        "/api/clients",
        json={"name": "  Ｊane​   Doe‮ ", "portfolio_themes": ["ＡＩ  infra​", "ai infra", "Energy"]},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Jane Doe"  # fullwidth -> ASCII, ZWSP/RLO stripped, whitespace collapsed
    assert body["portfolio_themes"] == ["AI infra", "Energy"]  # case-insensitive dedupe


def test_theme_count_and_length_caps(client):
    assert (
        client.post("/api/clients", json={"name": "A", "portfolio_themes": [f"t{i}" for i in range(11)]}).status_code
        == 422
    )
    assert client.post("/api/clients", json={"name": "A", "portfolio_themes": ["y" * 500]}).status_code == 422
    assert (
        client.post("/api/clients", json={"name": "A", "portfolio_themes": [f"t{i}" for i in range(10)]}).status_code
        == 201
    )


def test_client_count_per_account_is_capped(client, fake_db, monkeypatch):
    monkeypatch.setattr(api.settings, "max_clients_per_user", 2)
    assert client.post("/api/clients", json={"name": "A", "portfolio_themes": ["t"]}).status_code in (201, 400)
    # c1 already exists (1) + the one above = 2 -> third is refused
    res = client.post("/api/clients", json={"name": "B", "portfolio_themes": ["t"]})
    assert res.status_code == 400 and "maximum" in res.json()["message"]


def test_edit_content_length_cap(client, fake_db, monkeypatch):
    from conftest import FakeBriefing

    fake_db.briefing._rows.append(FakeBriefing("b1", "c1"))
    monkeypatch.setattr(api.settings, "max_briefing_chars", 1000)
    res = client.patch("/api/briefings/b1", json={"content": "z" * 5000})
    assert res.status_code == 422


# ------------------------------- metrics --------------------------------


def test_metrics_endpoint_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(api.settings, "metrics_token", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and "http_requests_total" in ok.text


def test_metrics_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(api.settings, "metrics_enabled", False)
    assert client.get("/metrics").status_code == 404


def test_metrics_use_route_templates_not_raw_paths(client, monkeypatch):
    monkeypatch.setattr(api.settings, "metrics_token", None)
    client.get("/api/clients/some-unique-id-123/briefings")
    text = client.get("/metrics").text
    assert 'route="/api/clients/{client_id}/briefings"' in text
    assert "some-unique-id-123" not in text


def test_rate_limit_rejection_is_counted_and_response_has_headers(client, monkeypatch):
    import ratelimit

    monkeypatch.setattr(api.settings, "metrics_token", None)
    for _ in range(ratelimit.POLICIES["global_api"].limit):
        client.get("/api/clients")
    res = client.get("/api/clients")
    assert res.status_code == 429
    assert res.headers["retry-after"] and res.headers["x-ratelimit-limit"] == str(
        ratelimit.POLICIES["global_api"].limit
    )
    body = res.json()
    assert body["error"] == "rate_limited" and body["request_id"]
    assert 'rate_limit_rejections_total{policy="global_api"}' in client.get("/metrics").text


def test_docs_are_available_in_dev_only():
    assert api.app.docs_url == "/docs"  # settings.is_prod is False under test; prod disables it (see settings tests)


def test_json_body_roundtrip_sanity(client):
    # guards the middleware's receive-wrapper: a normal small body still parses
    res = client.post(
        "/api/clients",
        content=json.dumps({"name": "N", "portfolio_themes": ["t"]}),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 201

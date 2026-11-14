"""The API booted under APP_ENV=production (a real subprocess, so the prod
settings are in force at import): docs/openapi disabled, /metrics needs its
token, CORS rejects an unlisted origin, security headers + HSTS present, body
cap enforced, errors carry no stack trace. No network, no database (FakeDB)."""

import json
import os
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROD_ENV = {
    "APP_ENV": "production",
    "DEBUG": "false",
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "CORS_ORIGINS": "https://app.example.com",
    "CLERK_JWKS_URL": "https://x.clerk.accounts.dev/.well-known/jwks.json",
    "CLERK_ISSUER": "https://x.clerk.accounts.dev",
    "GEMINI_API_KEY": "k1",
    "TAVILY_API_KEY": "k2",
    "GROQ_API_KEY": "k3",
    "HUGGINGFACE_API_KEY": "k4",
    "METRICS_TOKEN": "metrics-token-for-test",
    "ADMIN_USER_IDS": "user_admin1",
    "DEMO_MODE": "false",
    "MOCK_DB": "",
    "RATELIMIT_STORE": "memory",
    "LOG_LEVEL": "INFO",
}

SCRIPT = r"""
import json, sys
sys.path.insert(0, "tests")
import conftest as fakes
import api
from fastapi.testclient import TestClient
api.db = fakes.FakeDB()
c = TestClient(api.app, raise_server_exceptions=False)
out = {}
out["docs"] = c.get("/docs").status_code
out["openapi"] = c.get("/openapi.json").status_code
out["metrics_no_token"] = c.get("/metrics").status_code
out["metrics_bad_token"] = c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code
out["metrics_ok"] = c.get("/metrics", headers={"Authorization": "Bearer metrics-token-for-test"}).status_code
h = c.get("/healthz", headers={"X-Forwarded-Proto": "https"})
out["hsts"] = h.headers.get("strict-transport-security")
out["nosniff"] = h.headers.get("x-content-type-options")
out["frame"] = h.headers.get("x-frame-options")
out["referrer"] = h.headers.get("referrer-policy")
out["csp"] = h.headers.get("content-security-policy")
good = c.options("/api/clients", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
bad = c.options("/api/clients", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
out["cors_allowed_origin"] = good.headers.get("access-control-allow-origin")
out["cors_evil_origin"] = bad.headers.get("access-control-allow-origin")
out["no_token_401"] = c.get("/api/clients").status_code
big = c.post("/api/clients", content=b" " * (2 * 1024 * 1024), headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
out["body_413_or_401"] = big.status_code
out["demo_banner_mode"] = c.get("/healthz").json().get("demoMode")
r404 = c.get("/nope")
out["404_has_trace"] = "Traceback" in r404.text
out["404_schema"] = sorted(r404.json())
print("RESULT" + json.dumps(out))
"""


def _run():
    env = {**os.environ, **PROD_ENV}
    r = subprocess.run(
        [sys.executable, "-c", SCRIPT], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180
    )
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")]
    assert line, (r.stdout[-400:], r.stderr[-600:])
    return json.loads(line[0][len("RESULT") :])


def test_production_profile_http_surface():
    o = _run()
    assert o["docs"] == 404 and o["openapi"] == 404  # no interactive docs / schema in prod
    assert o["metrics_no_token"] == 401 and o["metrics_bad_token"] == 401 and o["metrics_ok"] == 200
    assert "max-age=" in (o["hsts"] or "")
    assert o["nosniff"] == "nosniff" and o["frame"] == "DENY" and o["referrer"] == "no-referrer"
    assert "frame-ancestors 'none'" in o["csp"]
    assert o["cors_allowed_origin"] == "https://app.example.com"
    assert o["cors_evil_origin"] is None  # an unlisted origin gets no CORS grant
    assert o["no_token_401"] == 401
    assert o["body_413_or_401"] == 413  # body cap is checked before auth
    assert o["404_has_trace"] is False and {"error", "message", "request_id"} <= set(o["404_schema"])

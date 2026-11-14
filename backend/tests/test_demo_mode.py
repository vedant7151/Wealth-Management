"""Phase 8 demo-mode tests: config resolution (demo_mode.py, pure), the
`/run` fallback paths (forced DEMO_MODE and quota-exhausted), `/healthz`/
`/readyz`, and the graceful-shutdown drain. No live LLM/Tavily/HF/Groq
calls -- demo mode's whole point is to avoid them."""

import os

os.environ.setdefault("MOCK_DB", "1")

import demo_mode  # noqa: E402
import runs  # noqa: E402
from tests.conftest import FakeClientProfile  # noqa: E402


# --------------------------------------------------------------------------
# demo_mode.py config resolution (pure, no DB/API)
# --------------------------------------------------------------------------


def test_demo_mode_true_forces_demo_regardless_of_key(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")
    assert demo_mode.should_use_demo_data() is True


def test_demo_mode_false_disables_demo_regardless_of_key(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert demo_mode.should_use_demo_data() is False


def test_demo_mode_auto_activates_when_no_key_configured(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert demo_mode.should_use_demo_data() is True


def test_demo_mode_auto_stays_off_when_key_configured(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")
    assert demo_mode.should_use_demo_data() is False


def test_configured_mode_reports_the_raw_setting(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    assert demo_mode.configured_mode() == "true"
    monkeypatch.setenv("DEMO_MODE", "garbage")
    assert demo_mode.configured_mode() == "auto"


def test_pick_fixture_is_consistent_for_the_same_seed():
    first = demo_mode.pick_fixture(seed="client-123")
    second = demo_mode.pick_fixture(seed="client-123")
    assert first["content"] == second["content"]


def test_pick_fixture_content_is_never_empty():
    fixture = demo_mode.pick_fixture(seed="any-client")
    assert fixture["content"].strip()
    assert "source" in fixture


# --------------------------------------------------------------------------
# /run demo-mode fallback (API-level, fake_db)
# --------------------------------------------------------------------------


def test_run_uses_demo_fixture_when_demo_mode_forced(api_client, fake_db, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 202
    body = res.json()
    assert body["isDemo"] is True
    assert "run_id" in body
    saved = fake_db.briefing.find_unique(where={"id": fake_db.briefing._rows[0].id})
    assert saved.isDemo is True
    assert saved.content.strip()


def test_run_falls_back_to_demo_when_daily_budget_exhausted(api_client, fake_db, monkeypatch):
    import api

    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr(
        api.llm_client, "check_daily_budget", lambda provider: {"allowed": False, "used": 18, "limit": 18}
    )
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 202
    assert res.json()["isDemo"] is True


def test_run_uses_real_background_run_when_demo_mode_off_and_budget_ok(api_client, fake_db, monkeypatch):
    import api

    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr(
        api.llm_client, "check_daily_budget", lambda provider: {"allowed": True, "used": 1, "limit": 18}
    )
    started = {}
    monkeypatch.setattr(api.runs, "start_background_run", lambda *a, **k: started.update(called=True) or object())
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="owner-A")
    client = api_client(user_id="owner-A")

    res = client.post("/api/clients/c1/run")

    assert res.status_code == 202
    assert res.json()["isDemo"] is False
    assert started.get("called") is True


# --------------------------------------------------------------------------
# /healthz, /readyz
# --------------------------------------------------------------------------


def test_healthz_is_dependency_free_and_reports_configured_mode(api_client, fake_db, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "auto")
    client = api_client(user_id=None)
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "demoMode": "auto"}


def test_readyz_reports_db_limiter_and_breaker_state(api_client, fake_db):
    client = api_client(user_id=None)
    res = client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["db"] == "ok"
    assert "rate_limit_store" in body
    assert "circuit_breakers" in body
    assert "active_runs" in body


# --------------------------------------------------------------------------
# Graceful shutdown drain (runs.py)
# --------------------------------------------------------------------------


def test_wait_for_active_runs_returns_zero_once_thread_finishes():
    import threading
    import time

    done = threading.Event()

    def _worker():
        time.sleep(0.05)
        done.set()

    t = threading.Thread(target=_worker, daemon=True)
    with runs._active_threads_lock:
        runs._active_threads.append(t)
    t.start()

    still_running = runs.wait_for_active_runs(timeout_seconds=2)

    assert still_running == 0
    assert done.is_set()
    with runs._active_threads_lock:
        runs._active_threads.clear()


def test_wait_for_active_runs_reports_still_running_after_timeout():
    import threading
    import time

    def _worker():
        time.sleep(1)

    t = threading.Thread(target=_worker, daemon=True)
    with runs._active_threads_lock:
        runs._active_threads.append(t)
    t.start()

    still_running = runs.wait_for_active_runs(timeout_seconds=0.05)

    assert still_running == 1
    with runs._active_threads_lock:
        runs._active_threads.clear()
    t.join(timeout=2)

"""Phase 10: prove no race lets a 4th run through (and the other admission
limits hold) under CONCURRENT requests.

What these tests do and do not prove:
  - In-memory store + the API-level admission path are exercised with real
    threads released together by a barrier -> real concurrency, real
    assertions on exact counts.
  - The Postgres store's SQL can't run here (no database). The unit tests
    below pin its *protocol* (advisory lock taken inside a transaction
    BEFORE the count statement); the SQL itself is exercised against a real
    Postgres by backend/scripts/verify_ratelimit_pg.py (results recorded in
    docs/PROGRESS.md)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Request

import api
import ratelimit
from conftest import FakeClientProfile


def _race(n, fn):
    """Runs fn(i) on n threads released at the same instant."""
    barrier = threading.Barrier(n)

    def wrapped(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


# ------------------------------ store level -------------------------------


def test_memory_store_never_admits_more_than_the_limit_under_contention():
    results = _race(64, lambda i: ratelimit.check("run", "user:racer").allowed)
    assert sum(results) == ratelimit.RUN_LIMIT == 3


def test_memory_store_lock_has_exactly_one_winner():
    results = _race(32, lambda i: ratelimit.try_lock("run:same-client"))
    assert sum(results) == 1


def test_limits_are_per_identity_under_contention():
    results = _race(40, lambda i: (i % 4, ratelimit.check("run", f"user:{i % 4}").allowed))
    for ident in range(4):
        assert sum(1 for who, ok in results if who == ident and ok) == 3


# --------------- Postgres store protocol (call-order contract) ------------


class _Tx:
    def __init__(self, log):
        self.log = log

    def execute_raw(self, sql, *args):
        self.log.append(("tx.execute", sql.split("(")[0].strip()[:40], args[:1]))
        return 1

    def query_raw(self, sql, *args):
        self.log.append(("tx.query", " ".join(sql.split())[:30]))
        # canned result: cnt_before=0, inserted
        return [{"cnt_before": 0, "oldest_ts": None, "inserted_ts": "2026-01-01T00:00:00"}]


class _TxCtx:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append(("BEGIN",))
        return _Tx(self.log)

    def __exit__(self, *exc):
        self.log.append(("COMMIT",))
        return False


class _FakePgDB:
    def __init__(self):
        self.log = []

    def tx(self, **kw):
        return _TxCtx(self.log)

    def query_raw(self, sql, *args):
        self.log.append(("db.query", " ".join(sql.split())[:30]))
        return [{"cnt_before": 0, "oldest_ts": None, "inserted_ts": "2026-01-01T00:00:00"}]

    def execute_raw(self, sql, *args):
        self.log.append(("db.execute", sql[:20]))
        return 1


def test_pg_strict_hit_takes_advisory_lock_before_counting_inside_a_transaction():
    db = _FakePgDB()
    store = ratelimit._PostgresStore(lambda: db)
    store.hit("run:user:a", 3600, 3, strict=True)
    kinds = [e[0] for e in db.log]
    assert kinds == ["BEGIN", "tx.execute", "tx.query", "COMMIT"]
    assert "pg_advisory_xact_lock" in db.log[1][1]
    assert db.log[1][2] == ("run:user:a",)  # lock is keyed per rate-limit key


def test_pg_non_strict_hit_stays_a_single_round_trip():
    db = _FakePgDB()
    ratelimit._PostgresStore(lambda: db).hit("global_api:user:a", 60, 120)
    assert [e[0] for e in db.log] == ["db.query"]


def test_check_uses_strict_path_only_for_fail_closed_policies(monkeypatch):
    db = _FakePgDB()
    monkeypatch.setattr(ratelimit, "_configured_store", ratelimit._PostgresStore(lambda: db))
    ratelimit.check("run", "user:a")
    assert db.log[0][0] == "BEGIN"
    db.log.clear()
    ratelimit.check("global_api", "user:a")
    assert db.log[0][0] == "db.query"


def test_pg_try_lock_is_advisory_locked_transaction():
    db = _FakePgDB()
    ratelimit._PostgresStore(lambda: db).try_lock("run:c1", 300)
    kinds = [e[0] for e in db.log]
    assert kinds[0] == "BEGIN" and kinds[-1] == "COMMIT"
    assert "pg_advisory_xact_lock" in db.log[1][1]
    # stale-lock cleanup and the insert both happen AFTER the lock
    assert [k for k in kinds if k.startswith("tx.")] == ["tx.execute", "tx.execute", "tx.query"]


# ------------------------- API-level /run admission ----------------------


@pytest.fixture
def run_env(api_client, fake_db, monkeypatch):
    """Per-request user via X-Test-User; fake background runner that never
    releases the lock (a run that stays in flight) and counts in-flight."""
    state = {"started": [], "in_flight": 0, "demo": 0, "budget_used": 0}

    def _user_from_header(request: Request) -> str:
        return request.headers["x-test-user"]

    def fake_start(run_log_id, client_id, user_id, themes, trigger, release_lock, **kw):
        state["started"].append(client_id)
        state["in_flight"] += 1

    def fake_demo(db, run_log_id, client_id, release_lock):
        state["demo"] += 1
        release_lock()

    monkeypatch.setattr(api.runs, "start_background_run", fake_start)
    monkeypatch.setattr(api.runs, "start_demo_run", fake_demo)
    monkeypatch.setattr(api.runs, "active_run_count", lambda: state["in_flight"])

    def budget(provider):
        return {"allowed": state["budget_used"] < 18, "used": state["budget_used"], "limit": 18}

    # The real check-then-start window is the RunLog INSERT (a network round
    # trip) between "read in-flight count" and "thread registered". A slow
    # fake insert makes that window wide enough that removing the admission
    # lock makes these tests fail (verified by mutation -- see PROGRESS.md).
    real_create = fake_db.runlog.create

    def slow_create(data):
        time.sleep(0.02)
        return real_create(data)

    monkeypatch.setattr(fake_db.runlog, "create", slow_create)
    monkeypatch.setattr(api.llm_client, "check_daily_budget", budget)
    client = api_client(user_id="ignored")
    api.app.dependency_overrides[api.get_current_user] = _user_from_header
    return client, state, fake_db


def test_exactly_three_runs_admitted_when_one_user_races_across_many_clients(run_env, monkeypatch):
    client, state, fake_db = run_env
    monkeypatch.setattr(api.settings, "max_concurrent_runs", 100)
    for i in range(12):
        fake_db.clientprofile._store[f"c{i}"] = FakeClientProfile(f"c{i}", clerkUserId="user-a")

    codes = _race(12, lambda i: client.post(f"/api/clients/c{i}/run", headers={"x-test-user": "user-a"}).status_code)

    assert sorted(codes).count(202) == 3, codes  # RUN_LIMIT=3, never a 4th
    assert codes.count(429) == 9
    assert len(state["started"]) == 3


def test_same_client_double_submit_starts_exactly_one_run(run_env, monkeypatch):
    client, state, fake_db = run_env
    monkeypatch.setattr(api.settings, "max_concurrent_runs", 100)
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="user-a")

    codes = _race(16, lambda i: client.post("/api/clients/c1/run", headers={"x-test-user": "user-a"}).status_code)

    assert codes.count(202) == 1, codes
    assert codes.count(409) == 15
    assert state["started"] == ["c1"]


def test_daily_budget_counts_in_flight_runs_so_a_burst_cannot_overshoot(run_env, monkeypatch):
    """15 of 18 budget units used; 20 DIFFERENT users each fire one run at
    once. ApiCallLog alone would let all 20 through (they all read used=15).
    Counting each in-flight run as one reserved unit admits exactly 3."""
    client, state, fake_db = run_env
    monkeypatch.setattr(api.settings, "max_concurrent_runs", 100)
    state["budget_used"] = 15
    for i in range(20):
        fake_db.clientprofile._store[f"c{i}"] = FakeClientProfile(f"c{i}", clerkUserId=f"user-{i}")

    codes = _race(20, lambda i: client.post(f"/api/clients/c{i}/run", headers={"x-test-user": f"user-{i}"}).status_code)

    assert all(c == 202 for c in codes)  # over-budget runs degrade to demo fixtures, never a raw error
    assert len(state["started"]) == 3, state
    assert state["demo"] == 17


def test_concurrent_run_capacity_cap_returns_503_without_burning_the_users_quota(run_env, monkeypatch):
    client, state, fake_db = run_env
    monkeypatch.setattr(api.settings, "max_concurrent_runs", 2)
    state["in_flight"] = 2
    fake_db.clientprofile._store["c1"] = FakeClientProfile("c1", clerkUserId="user-a")

    res = client.post("/api/clients/c1/run", headers={"x-test-user": "user-a"})

    assert res.status_code == 503 and res.headers["retry-after"] == "30"
    assert ratelimit.peek("run", "user:user-a").remaining == ratelimit.RUN_LIMIT  # quota untouched
    assert ratelimit.try_lock("run:c1") is True  # per-client lock was released


def test_rate_limit_headers_present_on_rejection_and_success(run_env, monkeypatch):
    client, state, fake_db = run_env
    monkeypatch.setattr(api.settings, "max_concurrent_runs", 100)
    for i in range(4):
        fake_db.clientprofile._store[f"c{i}"] = FakeClientProfile(f"c{i}", clerkUserId="user-a")
    ok = client.post("/api/clients/c0/run", headers={"x-test-user": "user-a"})
    assert ok.headers["x-ratelimit-limit"] == "3" and ok.headers["x-ratelimit-remaining"] == "2"
    for i in (1, 2):
        client.post(f"/api/clients/c{i}/run", headers={"x-test-user": "user-a"})
    denied = client.post("/api/clients/c3/run", headers={"x-test-user": "user-a"})
    assert denied.status_code == 429
    assert denied.json()["error"] == "run_limit_exceeded"
    assert int(denied.headers["retry-after"]) > 0
    assert denied.headers["x-ratelimit-remaining"] == "0"

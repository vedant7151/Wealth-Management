"""Scheduler per-client failure isolation (Phase 5) + idempotency (Phase 6).
Drives scheduler.run_all_clients with a fake `step` object instead of real
Inngest infrastructure -- no network/DB/LLM calls."""

import asyncio
import os

os.environ.setdefault("MOCK_DB", "1")
# scheduler.py's module-level `serve(...)` call needs a signing key present
# at import time (Inngest's webhook-signature verification setup) -- a
# throwaway test value is fine since this test never serves real requests.
os.environ.setdefault("INNGEST_SIGNING_KEY", "signkey-test-0123456789abcdef0123456789abcdef")
os.environ.setdefault("INNGEST_DEV", "1")

import scheduler  # noqa: E402


class FakeStep:
    """Minimal stand-in for inngest.Step: runs the given sync/async
    callable immediately (no memoization/retries needed for this test)."""

    async def run(self, step_id, fn):
        result = fn()
        if asyncio.iscoroutine(result):
            result = await result
        return result


class FakeRunLogRow:
    def __init__(self, id, clientProfileId, idempotencyKey, status="running"):
        self.id = id
        self.clientProfileId = clientProfileId
        self.idempotencyKey = idempotencyKey
        self.status = status
        self.briefingId = None
        self.errorCode = None
        self.finishedAt = None


class FakeRunLogTable:
    def __init__(self):
        self.rows: dict = {}
        self._counter = 0

    def find_many(self, where=None, take=None):
        where = where or {}
        rows = [r for r in self.rows.values() if all(getattr(r, k, None) == v for k, v in where.items())]
        return rows[:take] if take else rows

    def create(self, data):
        self._counter += 1
        row = FakeRunLogRow(
            id=f"run-{self._counter}",
            clientProfileId=data["clientProfileId"],
            idempotencyKey=data.get("idempotencyKey"),
            status=data.get("status", "running"),
        )
        self.rows[row.id] = row
        return row

    def update(self, where, data):
        row = self.rows[where["id"]]
        for k, v in data.items():
            setattr(row, k, v)
        return row


class FakeBriefingTableForScheduler:
    def find_first(self, where=None):
        return None


class FakeSchedulerDB:
    def __init__(self):
        self.runlog = FakeRunLogTable()
        self.briefing = FakeBriefingTableForScheduler()


def test_one_client_failure_does_not_block_the_others(monkeypatch):
    profiles = [
        {"id": "ok-1", "themes": ["AI infrastructure"]},
        {"id": "boom", "themes": ["Semiconductors"]},
        {"id": "ok-2", "themes": ["Energy transition"]},
    ]

    def fake_run_agent(profile, run_log_id):
        if profile["id"] == "boom":
            raise RuntimeError("simulated Tavily/Gemini outage")
        return f"briefing for {profile['id']}"

    monkeypatch.setattr(scheduler, "run_agent", fake_run_agent)

    result = asyncio.run(scheduler.run_all_clients(profiles, FakeStep()))

    assert result["briefings_processed"] == 2
    assert len(result["failures"]) == 1
    assert result["failures"][0]["client_id"] == "boom"
    assert "simulated Tavily/Gemini outage" in result["failures"][0]["error"]
    assert result["skipped_idempotent"] == []


def test_all_clients_succeed_reports_zero_failures(monkeypatch):
    profiles = [{"id": "a", "themes": ["X"]}, {"id": "b", "themes": ["Y"]}]
    monkeypatch.setattr(scheduler, "run_agent", lambda profile, run_log_id: "ok briefing")

    result = asyncio.run(scheduler.run_all_clients(profiles, FakeStep()))

    assert result["briefings_processed"] == 2
    assert result["failures"] == []


def test_empty_profile_list_is_a_no_op():
    result = asyncio.run(scheduler.run_all_clients([], FakeStep()))
    assert result == {
        "status": "success",
        "briefings_processed": 0,
        "failures": [],
        "skipped_idempotent": [],
    }


def test_idempotent_cron_double_fire_skips_second_run(monkeypatch):
    """Simulates the same cron event firing twice for the same client on
    the same day: the second `run_all_clients` call must skip the client
    (not run the agent a second time) because a non-failed RunLog with
    that client+date's idempotency key already exists."""
    db = FakeSchedulerDB()
    profiles = [{"id": "client-x", "themes": ["AI infrastructure"]}]
    calls = {"n": 0}

    def fake_run_agent(profile, run_log_id):
        calls["n"] += 1
        return "a briefing"

    monkeypatch.setattr(scheduler, "run_agent", fake_run_agent)

    first = asyncio.run(scheduler.run_all_clients(profiles, FakeStep(), db=db))
    second = asyncio.run(scheduler.run_all_clients(profiles, FakeStep(), db=db))

    assert calls["n"] == 1
    assert first["briefings_processed"] == 1
    assert second["briefings_processed"] == 0
    assert len(second["skipped_idempotent"]) == 1
    assert second["skipped_idempotent"][0]["client_id"] == "client-x"


def test_idempotency_does_not_block_a_retry_after_a_failure(monkeypatch):
    """A previously FAILED run for the same client+day must NOT be treated
    as already-done -- the whole point of idempotency here is skipping a
    duplicate SUCCESSFUL (or still in-flight) run, not preventing a retry
    of a client that genuinely failed earlier today."""
    db = FakeSchedulerDB()
    profiles = [{"id": "client-y", "themes": ["Semiconductors"]}]
    attempt = {"n": 0}

    def flaky_run_agent(profile, run_log_id):
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise RuntimeError("transient outage")
        return "recovered briefing"

    monkeypatch.setattr(scheduler, "run_agent", flaky_run_agent)

    first = asyncio.run(scheduler.run_all_clients(profiles, FakeStep(), db=db))
    second = asyncio.run(scheduler.run_all_clients(profiles, FakeStep(), db=db))

    assert first["failures"][0]["client_id"] == "client-y"
    assert second["briefings_processed"] == 1
    assert second["skipped_idempotent"] == []
    assert attempt["n"] == 2


def test_recover_stuck_runs_marks_stale_running_rows_failed():
    import datetime

    import agent
    import runs

    os.environ["MOCK_DB"] = "1"
    mock_db = agent.MockDB()
    stale_row = mock_db.runlog.create(
        data={"clientProfileId": "c1", "userId": None, "trigger": "cron", "status": "running", "idempotencyKey": None}
    )
    mock_db.runlog._rows[stale_row.id]["startedAt"] = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=runs.RUN_TIMEOUT_SECONDS + 60
    )
    fresh_row = mock_db.runlog.create(
        data={"clientProfileId": "c1", "userId": None, "trigger": "cron", "status": "running", "idempotencyKey": None}
    )

    recovered = runs.recover_stuck_runs(mock_db)

    assert recovered == 1
    assert str(mock_db.runlog.find_unique(where={"id": stale_row.id}).status) == "failed"
    assert str(mock_db.runlog.find_unique(where={"id": fresh_row.id}).status) == "running"

"""Phase 10: retention, log redaction, timeouts, statement timeout,
dead-letter (failed-run) handling, outbound-timeout audit."""

import datetime
import json
import os
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("INNGEST_SIGNING_KEY", "signkey-test-0123456789abcdef0123456789abcdef")
os.environ.setdefault("INNGEST_DEV", "1")

import db_utils  # noqa: E402
import llm_client  # noqa: E402
import logging_config  # noqa: E402
import retention  # noqa: E402
import runs  # noqa: E402
import scheduler  # noqa: E402
import settings as S  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent

# ------------------------------- retention --------------------------------


class _RetDB:
    def __init__(self, counts=None, fail_on=None):
        self.sql = []
        self.counts = dict(counts or {})
        self.fail_on = fail_on

    def query_raw(self, sql, *args):
        self.sql.append(sql)
        table = re.search(r'FROM "(\w+)"', sql).group(1)
        return [{"cnt": self.counts.get(table, 0)}]

    def execute_raw(self, sql, *args):
        self.sql.append(sql)
        table = re.search(r'DELETE FROM "(\w+)"', sql).group(1)
        if table == self.fail_on:
            raise RuntimeError("boom")
        n = min(self.counts.get(table, 0), retention.BATCH_SIZE)
        self.counts[table] = self.counts.get(table, 0) - n
        return n


def _settings():
    return S.load_settings({"MOCK_DB": "1"})


def test_retention_only_ever_touches_the_four_allowlisted_tables():
    db = _RetDB({t: 3 for t in retention.ALLOWED_TABLES})
    retention.purge(db, _settings(), dry_run=False)
    touched = {re.search(r'"(\w+)"', q).group(1) for q in db.sql}
    assert touched == {"DocumentChunk", "SearchCache", "ApiCallLog", "RateLimitHit"}
    for protected in retention.PROTECTED_TABLES:
        assert not any(f'"{protected}"' in q for q in db.sql)
    assert {"ClientProfile", "Briefing", "AuditLog"} <= retention.PROTECTED_TABLES
    assert not (retention.ALLOWED_TABLES & retention.PROTECTED_TABLES)


def test_retention_source_never_names_protected_tables_in_sql():
    src = (BACKEND / "retention.py").read_text(encoding="utf-8")
    sql_lines = [ln for ln in src.splitlines() if "DELETE FROM" in ln]
    assert sql_lines and all("{table}" in ln for ln in sql_lines)


def test_retention_dry_run_deletes_nothing():
    db = _RetDB({"DocumentChunk": 10, "ApiCallLog": 4})
    report = retention.purge(db, _settings(), dry_run=True)
    assert report["DocumentChunk"] == 10 and report["ApiCallLog"] == 4
    assert all(q.lstrip().upper().startswith("SELECT") for q in db.sql)


def test_retention_batches_and_uses_per_table_cutoffs():
    db = _RetDB({"DocumentChunk": retention.BATCH_SIZE * 2 + 7})
    report = retention.purge(db, _settings(), dry_run=False)
    assert report["DocumentChunk"] == retention.BATCH_SIZE * 2 + 7
    assert sum('DELETE FROM "DocumentChunk"' in q for q in db.sql) == 3


def test_retention_failure_on_one_table_does_not_stop_the_rest():
    db = _RetDB({t: 2 for t in retention.ALLOWED_TABLES}, fail_on="SearchCache")
    report = retention.purge(db, _settings(), dry_run=False)
    assert report["SearchCache"] == -1
    assert report["DocumentChunk"] == 2 and report["ApiCallLog"] == 2 and report["RateLimitHit"] == 2


def test_retention_cron_is_noop_unless_enabled():
    db = _RetDB({"DocumentChunk": 5})
    assert scheduler.run_retention(db=db, settings=_settings()) == {"status": "disabled"}
    assert db.sql == []
    enabled = S.load_settings({"MOCK_DB": "1", "RETENTION_ENABLED": "true"})
    out = scheduler.run_retention(db=_RetDB({"DocumentChunk": 5}), settings=enabled)
    assert out["status"] == "ok" and out["deleted"]["DocumentChunk"] == 5


# ----------------------------- log redaction ------------------------------


# Fake secrets are assembled from pieces so no complete key-shaped literal
# exists in the repo (scripts/ci/check_repo_hygiene.py would rightly flag one).
FAKE_SECRETS = [
    "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "gsk_" + "abcdefghijklmnopqrstuvwxyz123456",
    "tvly-" + "dev-abcdefghijklmnop12345",
    "hf_" + "abcdefghijklmnopqrstuvwxyz1234",
    "sk-" + "abcdefghijklmnopqrstuvwxyz1234",
    "eyJ" + "hbGciOiJSUzI1NiJ9." + "eyJzdWIiOiJ1c2VyXzEyMyJ9." + "c2lnbmF0dXJlLXNpZ25hdHVyZQ",
]


@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_redaction_scrubs_known_secret_shapes(secret):
    out = logging_config.redact(f"calling provider with key={secret} now")
    assert secret not in out and "REDACTED" in out


def test_redaction_scrubs_bearer_query_tokens_and_db_passwords():
    out = logging_config.redact(
        "Authorization: Bearer abcdefghijkl1234 GET /api/runs/1/events?token=SEKRET123&x=1 "
        "postgresql://user:" + "hunter2pw@host:5432/db"
    )
    assert "abcdefghijkl1234" not in out
    assert "SEKRET123" not in out
    assert "hunter2pw" not in out
    assert "x=1" in out and "user:" in out and "@host" in out  # structure kept


def test_long_messages_are_truncated_not_dumped():
    out = logging_config.redact("p" * 10_000, max_chars=500)
    assert len(out) < 600 and "TRUNCATED" in out


def _format(record_msg, **extra):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, record_msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return json.loads(logging_config.JsonFormatter().format(rec))


def test_json_log_carries_ids_and_redacts_message():
    token = logging_config.request_id_var.set("req-123456789")
    try:
        entry = _format("auth header Bearer abcdefghijklmnop", run_id="r1", client_id="c1")
    finally:
        logging_config.request_id_var.reset(token)
    assert entry["request_id"] == "req-123456789" and entry["run_id"] == "r1" and entry["client_id"] == "c1"
    assert "abcdefghijklmnop" not in entry["msg"]


def test_client_names_are_dropped_unless_flag_set(monkeypatch):
    monkeypatch.delenv("LOG_INCLUDE_CLIENT_NAMES", raising=False)
    assert "client_name" not in _format("x", client_name="Jane Doe")
    monkeypatch.setenv("LOG_INCLUDE_CLIENT_NAMES", "true")
    assert _format("x", client_name="Jane Doe")["client_name"] == "Jane Doe"


def test_exception_traces_are_redacted():
    try:
        raise RuntimeError("failed calling https://api.example.com/?api_key=TOPSECRETKEY123")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "err", (), sys.exc_info())
    out = json.loads(logging_config.JsonFormatter().format(rec))
    assert "TOPSECRETKEY123" not in out["exc"]


def test_node_logger_stamps_request_id():
    adapter = logging_config.get_node_logger({"run_id": "r", "client_id": "c", "request_id": "q"}, "writer")
    assert adapter.extra["request_id"] == "q"


# ------------------------------- timeouts ---------------------------------


def test_run_with_timeout_actually_abandons_a_hung_call():
    """Regression for the pre-Phase-10 bug: `with ThreadPoolExecutor` blocked
    until the hung call finished, so the 'timeout' never fired on time."""
    release = threading.Event()
    start = time.time()
    with pytest.raises(TimeoutError):
        llm_client._run_with_timeout(lambda: release.wait(5), 0.2)
    assert time.time() - start < 1.5
    release.set()


def test_run_with_timeout_returns_value_and_propagates_errors():
    assert llm_client._run_with_timeout(lambda: 42, 1) == 42
    with pytest.raises(ValueError):
        llm_client._run_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 1)


def test_global_provider_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr(llm_client, "_call_slots", threading.BoundedSemaphore(2))
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def work():
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1

    threads = [threading.Thread(target=lambda: llm_client._run_with_timeout(work, 5)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert active["max"] <= 2


def test_every_outbound_http_call_site_has_an_explicit_timeout():
    """Static audit: each requests.get/post in the backend passes timeout=."""
    offenders = []
    for path in [*BACKEND.glob("*.py")]:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"requests\.(get|post|put|patch|delete)\(", src):
            depth, i = 1, m.end()
            while depth and i < len(src):
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            if "timeout" not in src[m.end() : i]:
                offenders.append(f"{path.name}:{src[: m.start()].count(chr(10)) + 1}")
    assert offenders == []


# --------------------------- statement timeout ----------------------------


class _TxDB:
    def __init__(self):
        self.calls = []

    def tx(self, **kw):
        outer = self

        class Ctx:
            def __enter__(self_inner):
                outer.calls.append(("BEGIN", kw.get("timeout")))
                return outer

            def __exit__(self_inner, *a):
                outer.calls.append(("COMMIT",))
                return False

        return Ctx()

    def execute_raw(self, sql, *a):
        self.calls.append(("exec", sql))

    def query_raw(self, sql, *a):
        self.calls.append(("query", sql))
        return [{"id": 1}]


def test_vector_queries_run_under_set_local_statement_timeout():
    db = _TxDB()
    rows = db_utils.query_with_timeout(db, "SELECT 1", timeout_ms=4000)
    assert rows == [{"id": 1}]
    assert [c[0] for c in db.calls] == ["BEGIN", "exec", "query", "COMMIT"]
    assert db.calls[1][1] == "SET LOCAL statement_timeout = 4000"
    assert db.calls[0][1] > datetime.timedelta(milliseconds=4000)  # prisma tx timeout outlasts it


def test_statement_timeout_falls_back_for_standins_without_tx():
    class Plain:
        def query_raw(self, sql, *a):
            return ["ok"]

    assert db_utils.query_with_timeout(Plain(), "SELECT 1", timeout_ms=1000) == ["ok"]


def test_agent_vector_queries_go_through_the_timeout_helper():
    src = (BACKEND / "agent.py").read_text(encoding="utf-8")
    assert src.count("db_utils.query_with_timeout(") == 2
    assert not re.search(r"db\.query_raw\(\s*f?\"\"\"\s*SELECT[^\"]*<->", src)


# ---------------------- dead-letter / failed-run handling -----------------


def test_failed_run_is_recorded_audited_and_counted(monkeypatch):
    """A run whose graph raises leaves a failed RunLog (the dead-letter
    record the UI lists), an AuditLog `run_failed` entry, and a metric."""
    import agent
    import metrics

    from conftest import FakeDB

    fake = FakeDB()
    fake.clientprofile._store["c1"] = SimpleNamespace(id="c1")
    run_log = runs.create_run_log(fake, "c1", "user-a", trigger="manual")
    monkeypatch.setattr(agent, "get_db", lambda: fake)
    monkeypatch.setattr(agent, "close_db", lambda: None)

    def boom(_state):
        raise RuntimeError("graph exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(agent.app, "stream", boom)

    def count():
        return metrics.RUNS_TOTAL.labels("manual", "failed")._value.get()

    before = count()
    t = runs.start_background_run(run_log.id, "c1", "user-a", ["X"], "manual", release_lock=lambda: None)
    t.join(timeout=10)

    row = fake.runlog.find_unique(where={"id": run_log.id})
    assert str(row.status) == "failed" and row.errorCode == "RuntimeError"
    assert fake.auditlog.created and fake.auditlog.created[-1]["action"] == "run_failed"
    assert fake.auditlog.created[-1]["metadata"]["error_code"] == "RuntimeError"
    assert count() == before + 1


def test_request_id_reaches_run_state_and_langsmith_trace_metadata(monkeypatch):
    """The API's request id must follow the run into (a) the graph's input
    state (so node log lines carry it) and (b) the LangSmith tracing context
    metadata -- while carrying NO client name or user id."""
    import agent
    from langsmith.run_helpers import get_tracing_context

    from conftest import FakeDB

    fake = FakeDB()
    fake.clientprofile._store["c1"] = SimpleNamespace(id="c1")
    run_log = runs.create_run_log(fake, "c1", "user-a", trigger="manual")
    monkeypatch.setattr(agent, "get_db", lambda: fake)
    monkeypatch.setattr(agent, "close_db", lambda: None)
    seen = {}

    def fake_stream(state):
        seen["request_id"] = state.get("request_id")
        seen["ctx"] = dict(get_tracing_context().get("metadata") or {})
        seen["tags"] = list(get_tracing_context().get("tags") or [])
        return iter(())

    monkeypatch.setattr(agent.app, "stream", fake_stream)
    t = runs.start_background_run(
        run_log.id, "c1", "user-a", ["X"], "manual", release_lock=lambda: None, request_id="req-abc-123456"
    )
    t.join(timeout=10)

    assert seen["request_id"] == "req-abc-123456"
    assert seen["ctx"]["request_id"] == "req-abc-123456"
    assert seen["ctx"]["run_id"] == run_log.id and seen["ctx"]["client_id"] == "c1"
    assert not {"client_name", "user_id", "name"} & set(seen["ctx"])
    assert any(tag.startswith("trigger:") for tag in seen["tags"])

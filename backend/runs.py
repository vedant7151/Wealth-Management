"""Background graph runs, live SSE progress, and run history.

A run (manual `/run` call or a cron-triggered client run) gets a `RunLog`
row immediately and executes `agent.app.stream()` on a background thread,
so the HTTP request that creates it returns right away with `{run_id}`
instead of blocking for the 20-90s a full graph run usually takes.

Live progress is served from an in-process pub/sub (`_channels`, below),
not by polling `RunLog.nodeTimeline` -- that column is only written once,
at completion. This means live progress is only visible to a client
connected to the SAME process that is running the graph; in this project's
single-instance deployment (see CLAUDE.md/docs/PROGRESS.md's existing note
about `llm_client`'s in-process circuit breakers for the same reason) that
is always true. A multi-instance deployment would need this moved to
Redis/Postgres pub-sub -- noted as a Handoff item, not fixed here.

Rate limiting and the per-client run lock are both resolved by the CALLER
(`api.py`) at run-creation time, before `start_background_run` is ever
invoked -- this module has no opinion on quotas, only on executing and
reporting a run that has already been admitted. The lock is released here,
in the background thread's `finally`, once the run actually finishes (or
crashes/times out) -- not when the HTTP request returns -- via the
`release_lock` callable the caller passes in.
"""

import contextlib
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from prisma import Json

import metrics

logger = logging.getLogger("runs")

HEARTBEAT_INTERVAL_SECONDS = 15
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "600"))

# Only these fields are ever put on an event -- ids, counts, node names,
# reasons, and status. Never the briefing text, retrieved chunk content,
# criticism quotes, or any prompt.
_ALLOWED_STATS_KEYS = {
    "ingest_stats",
    "theme_coverage",
    "citations_so_far",
    "write_failed",
    "is_compliant",
    "judge_unavailable",
    "violation_count",
    "next",
}


class _RunChannel:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: List[dict] = []
        self.subscribers: List["queue.Queue"] = []
        self.done = False


_channels: dict = {}
_channels_lock = threading.Lock()

# Phase 8: tracked so the API's shutdown handler can drain in-flight runs
# on SIGTERM (up to GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS) instead of the
# process dying mid-run and leaving a RunLog stuck at "running" for
# recover_stuck_runs() to clean up later. A thread removes itself in its
# own `finally`, whether it finishes, crashes, or times out.
_active_threads: List[threading.Thread] = []
_active_threads_lock = threading.Lock()


def active_run_count() -> int:
    with _active_threads_lock:
        return len(_active_threads)


def _trace_context(run_id: str, client_id: str, trigger: str, request_id: Optional[str]):
    """LangSmith trace metadata (request/run/client ids + environment tag),
    applied via langsmith.tracing_context so every LangGraph/LLM span in
    this run carries it. Deliberately NO client name and NO user id (see
    REDACT_CLIENT_NAMES_IN_TRACES / docs/SECURITY.md). Falls back to a
    no-op if langsmith can't provide the context manager."""
    try:
        from langsmith import tracing_context

        return tracing_context(
            metadata={"run_id": run_id, "client_id": client_id, "request_id": request_id, "trigger": trigger},
            tags=[os.environ.get("APP_ENV", "dev"), f"trigger:{trigger}"],
        )
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext()


def wait_for_active_runs(timeout_seconds: float) -> int:
    """Joins every still-running background thread with a shared time
    budget. Returns how many are STILL running when the budget runs out
    (0 means every in-flight run finished within the timeout)."""
    with _active_threads_lock:
        threads = list(_active_threads)
    deadline = time.time() + timeout_seconds
    for t in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        t.join(timeout=remaining)
    with _active_threads_lock:
        still_running = [t for t in _active_threads if t.is_alive()]
        return len(still_running)


def _get_channel(run_id: str, create: bool = False) -> Optional[_RunChannel]:
    with _channels_lock:
        ch = _channels.get(run_id)
        if ch is None and create:
            ch = _RunChannel()
            _channels[run_id] = ch
        return ch


def publish(run_id: str, event_type: str, **fields) -> dict:
    ch = _get_channel(run_id, create=True)
    with ch.lock:
        seq = len(ch.events) + 1
        event = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **fields,
        }
        ch.events.append(event)
        if event_type == "done":
            ch.done = True
        subs = list(ch.subscribers)
    for q in subs:
        q.put(event)
    return event


def subscribe(run_id: str):
    ch = _get_channel(run_id, create=True)
    q: "queue.Queue" = queue.Queue()
    with ch.lock:
        ch.subscribers.append(q)
    return ch, q


def unsubscribe(ch: _RunChannel, q) -> None:
    with ch.lock:
        if q in ch.subscribers:
            ch.subscribers.remove(q)


def events_since(run_id: str, last_seq: int) -> List[dict]:
    ch = _get_channel(run_id)
    if not ch:
        return []
    with ch.lock:
        return [e for e in ch.events if e["seq"] > last_seq]


def _format_sse(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"


def sse_stream(run_id: str, last_event_id: int = 0):
    """Generator of SSE-formatted strings for one run. Replays any events
    after `last_event_id` (client reconnect via the `Last-Event-ID` header
    or a `?last_event_id=` query param), then streams live events, sending
    a heartbeat comment every HEARTBEAT_INTERVAL_SECONDS while idle so
    intermediary proxies/load balancers don't time out the connection.
    Stops after the run's terminal "done" event."""
    for event in events_since(run_id, last_event_id):
        yield _format_sse(event)
        if event["type"] == "done":
            return

    ch, q = subscribe(run_id)
    try:
        while True:
            try:
                event = q.get(timeout=HEARTBEAT_INTERVAL_SECONDS)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            yield _format_sse(event)
            if event["type"] == "done":
                return
    finally:
        unsubscribe(ch, q)


def _safe_stats(node_name: str, value: dict) -> dict:
    """Extracts only ids/counts/status -- never briefing text, retrieved
    chunk content, or criticism quotes -- from a node's state delta."""
    value = value or {}
    if node_name == "researcher":
        stats = {"ingest_stats": value.get("ingest_stats")}
    elif node_name == "retriever":
        stats = {
            "theme_coverage": value.get("theme_coverage"),
            "citations_so_far": len(value.get("final_citations") or []),
        }
    elif node_name == "writer":
        stats = {"write_failed": value.get("write_failed", False)}
    elif node_name == "critic":
        stats = {
            "is_compliant": value.get("is_compliant"),
            "judge_unavailable": value.get("judge_unavailable"),
            "violation_count": len(value.get("compliance_violations") or []),
        }
    elif node_name == "supervisor":
        stats = {"next": value.get("next")}
    else:
        stats = {}
    return {k: v for k, v in stats.items() if k in _ALLOWED_STATS_KEYS}


def create_run_log(db, client_id: str, user_id: Optional[str], trigger: str, idempotency_key: Optional[str] = None):
    return db.runlog.create(
        data={
            "clientProfileId": client_id,
            "userId": user_id,
            "trigger": trigger,
            "status": "running",
            "idempotencyKey": idempotency_key,
        }
    )


def _persist_run_log(
    db,
    run_log_id: str,
    status: str,
    duration_ms: int,
    timeline: List[dict],
    error_code: Optional[str],
    briefing_id: Optional[str],
) -> None:
    try:
        db.runlog.update(
            where={"id": run_log_id},
            data={
                "status": status,
                "finishedAt": datetime.now(timezone.utc),
                "durationMs": duration_ms,
                "nodeTimeline": Json(timeline),
                "errorCode": error_code,
                "briefingId": briefing_id,
            },
        )
    except Exception:
        logger.exception("Failed to persist RunLog %s", run_log_id)


def start_background_run(
    run_log_id: str,
    client_id: str,
    user_id: Optional[str],
    portfolio_themes: List[str],
    trigger: str,
    release_lock: Callable[[], None],
    request_id: Optional[str] = None,
) -> threading.Thread:
    """Spawns the daemon thread that actually executes the graph for one
    run and reports progress. `release_lock` is called exactly once, in a
    `finally`, whether the run succeeds, fails, raises, or times out --
    this is what guarantees the per-client run lock is never held forever
    by a crashed run."""

    def _runner() -> None:
        import agent  # local import: avoids a circular import at module load time

        run_id = run_log_id
        start = time.time()
        timeline: List[dict] = []
        status = "failed"
        error_code: Optional[str] = None
        briefing_id: Optional[str] = None

        publish(run_id, "node", node="run", reason=f"run started (trigger={trigger})", stats={})

        try:
            input_state = agent.new_input_state(client_id, portfolio_themes, run_id=run_id, user_id=user_id)
            input_state["request_id"] = request_id
            with _trace_context(run_id, client_id, trigger, request_id):
                for output in agent.app.stream(input_state):
                    if time.time() - start > RUN_TIMEOUT_SECONDS:
                        raise TimeoutError(f"run exceeded RUN_TIMEOUT_SECONDS ({RUN_TIMEOUT_SECONDS}s)")
                    for node_name, value in output.items():
                        value = value or {}
                        event = publish(
                            run_id,
                            "node",
                            node=node_name,
                            reason=value.get("reason"),
                            stats=_safe_stats(node_name, value),
                        )
                        timeline.append(event)
                        if node_name == "writer" and value.get("write_failed"):
                            error_code = "writer_failed"

            db = agent.get_db()
            briefing = db.briefing.find_first(where={"runId": run_id})
            if briefing is not None:
                briefing_id = briefing.id
                status = "failed" if str(briefing.status) == "failed" else "success"
                if status == "failed":
                    error_code = error_code or "writer_failed"
            else:
                status = "failed"
                error_code = error_code or "no_briefing_saved"
        except Exception as e:
            status = "failed"
            error_code = error_code or type(e).__name__
            logger.exception("Run %s failed", run_id)
        finally:
            duration_ms = int((time.time() - start) * 1000)
            metrics.RUNS_TOTAL.labels(trigger, status).inc()
            metrics.RUN_DURATION.labels(trigger, status).observe(duration_ms / 1000)
            try:
                _persist_run_log(agent.get_db(), run_log_id, status, duration_ms, timeline, error_code, briefing_id)
            except Exception:
                logger.exception("Failed to persist RunLog %s (db unreachable)", run_log_id)
            if status == "failed":
                # Dead-letter record: the failed RunLog row (status=failed +
                # errorCode) is the queue the UI reads; this audit entry is
                # the append-only "who/what/why" trail alongside it.
                try:
                    import audit

                    audit.log_event(
                        agent.get_db(),
                        action="run_failed",
                        user_id=user_id,
                        client_id=client_id,
                        metadata={"run_id": run_id, "error_code": error_code, "trigger": trigger},
                    )
                except Exception:
                    logger.exception("Failed to audit failed run %s", run_id)
            publish(
                run_id,
                "done",
                status=status,
                duration_ms=duration_ms,
                briefing_id=briefing_id,
                error_code=error_code,
            )
            try:
                agent.close_db()
            except Exception:
                logger.debug("close_db failed (ignored)", exc_info=True)
            release_lock()
            with _active_threads_lock:
                if thread in _active_threads:
                    _active_threads.remove(thread)
            metrics.set_in_flight(active_run_count())

    thread = threading.Thread(target=_runner, name=f"run-{run_log_id}", daemon=True)
    with _active_threads_lock:
        _active_threads.append(thread)
    metrics.set_in_flight(active_run_count())
    thread.start()
    return thread


def start_demo_run(db, run_log_id: str, client_id: str, release_lock: Callable[[], None]):
    """Phase 8 demo-mode fallback: instead of running the real graph,
    synchronously saves a Briefing from a pre-recorded REAL-run fixture
    (`demo_mode.pick_fixture`, never invented text -- see that module's
    docstring) and marks the RunLog `success` right away. Still publishes
    the same `node`/`done` SSE event shape a real run would, so
    `RunProgress`'s timeline renders something coherent instead of
    nothing, and still calls `release_lock` exactly once in a `finally`
    for the same reason `start_background_run` does -- callers (api.py)
    don't need to know which path actually ran."""
    import demo_mode

    run_id = run_log_id
    start = time.time()
    try:
        publish(run_id, "node", node="run", reason="demo mode: serving a pre-recorded real run", stats={})
        fixture = demo_mode.pick_fixture(seed=client_id)
        briefing = db.briefing.create(
            data={
                "content": fixture["content"],
                "clientProfileId": client_id,
                "status": fixture.get("status", "compliant"),
                "complianceViolations": Json(fixture.get("complianceViolations", [])),
                "runId": run_id,
                "isDemo": True,
            }
        )
        for cit in fixture.get("citations", []):
            db.citation.create(
                data={
                    "url": cit.get("url"),
                    "title": cit.get("title"),
                    "publishedDate": cit.get("publishedDate"),
                    "briefingId": briefing.id,
                }
            )
        publish(run_id, "node", node="save", reason="demo briefing saved", stats={})
        duration_ms = int((time.time() - start) * 1000)
        _persist_run_log(db, run_log_id, "success", duration_ms, [], None, briefing.id)
        publish(run_id, "done", status="success", duration_ms=duration_ms, briefing_id=briefing.id, error_code=None)
        return briefing
    except Exception:
        logger.exception("Demo run %s failed", run_log_id)
        duration_ms = int((time.time() - start) * 1000)
        _persist_run_log(db, run_log_id, "failed", duration_ms, [], "demo_run_failed", None)
        publish(
            run_id, "done", status="failed", duration_ms=duration_ms, briefing_id=None, error_code="demo_run_failed"
        )
        raise
    finally:
        release_lock()


def recover_stuck_runs(db, timeout_seconds: int = RUN_TIMEOUT_SECONDS) -> int:
    """Startup task: any RunLog still `status=running` whose `startedAt` is
    older than `timeout_seconds` means the process that was executing it
    died (crash, redeploy, OOM-kill) without ever reaching the background
    thread's `finally`. Mark it failed so a restart can't leave that
    client's run lock (and its row in the history view) stuck forever.
    Returns the number of rows recovered."""
    import datetime

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=timeout_seconds)
    try:
        rows = db.query_raw(
            """
            UPDATE "RunLog"
            SET status = 'failed', "errorCode" = 'timeout_recovered', "finishedAt" = NOW()
            WHERE status = 'running' AND "startedAt" < $1::timestamp
            RETURNING id
            """,
            cutoff,
        )
        count = len(rows or [])
        if count:
            logger.warning("Recovered %d stuck run(s) on startup", count)
        return count
    except Exception:
        logger.exception("recover_stuck_runs failed")
        return 0

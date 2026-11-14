"""Background run execution + SSE event ordering (Phase 6). Drives
runs.start_background_run against a monkeypatched agent.app.stream() (a
mocked graph run -- no live LLM/Tavily/HF/Groq calls), under MOCK_DB=1 so
RunLog/Briefing persistence goes through agent.MockDB."""

import os
import time

os.environ.setdefault("MOCK_DB", "1")

import agent  # noqa: E402
import runs  # noqa: E402


def _wait_for_done(run_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = runs.events_since(run_id, 0)
        if events and events[-1]["type"] == "done":
            return events
        time.sleep(0.02)
    raise AssertionError("run did not complete in time")


def test_event_ordering_for_a_mocked_successful_graph_run(monkeypatch):
    scripted_outputs = [
        {"supervisor": {"next": "researcher", "reason": "no research yet"}},
        {"researcher": {"research_done": True, "ingest_stats": {"queries": 2, "chunks_added": 3}}},
        {"supervisor": {"next": "retriever", "reason": "research done, not yet retrieved"}},
        {
            "retriever": {
                "retrieval_attempted": True,
                "theme_coverage": {"AI": True},
                "final_citations": [{"url": "https://x"}],
            }
        },
        {"supervisor": {"next": "writer", "reason": "retrieved context accepted, drafting"}},
        {"writer": {"final_briefing": "draft text", "needs_critique": True, "write_failed": False}},
        {"supervisor": {"next": "compliance_critic", "reason": "draft ready for compliance critique"}},
        {"critic": {"is_compliant": True, "judge_unavailable": False, "compliance_violations": []}},
        {"supervisor": {"next": "save", "briefing_status": "compliant", "reason": "rules + judge both passed"}},
        {"save": {}},
    ]

    monkeypatch.setattr(agent.app, "stream", lambda input_state: iter(scripted_outputs))

    # Patched on the CLASS (not a specific instance) because
    # start_background_run executes on its own thread, which gets its own
    # agent.MockDB() instance via the thread-local in agent.get_db() --
    # patching an instance method on this test thread's instance would
    # never be seen by that other instance.
    def fake_find_first(self, where=None):
        return type("Rec", (), {"id": "mock-briefing-final", "status": "compliant"})()

    monkeypatch.setattr(agent.MockDB._Briefing, "find_first", fake_find_first)

    released = {"n": 0}
    run_log_id = "test-run-1"
    thread = runs.start_background_run(
        run_log_id,
        "client_1",
        None,
        ["AI infrastructure"],
        trigger="manual",
        release_lock=lambda: released.__setitem__("n", released["n"] + 1),
    )
    thread.join(timeout=5)

    events = _wait_for_done(run_log_id)

    node_sequence = [e["node"] for e in events if e["type"] == "node"]
    assert node_sequence == [
        "run",
        "supervisor",
        "researcher",
        "supervisor",
        "retriever",
        "supervisor",
        "writer",
        "supervisor",
        "critic",
        "supervisor",
        "save",
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "success"
    assert events[-1]["briefing_id"] == "mock-briefing-final"
    assert released["n"] == 1

    # Every event's seq is strictly increasing (ordering is well-defined).
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    # No event carries briefing text, retrieved content, or criticism quotes.
    for e in events:
        blob = str(e)
        assert "draft text" not in blob


def test_release_lock_is_called_even_when_the_graph_run_raises(monkeypatch):
    def boom(input_state):
        raise RuntimeError("simulated Gemini/Tavily outage")

    monkeypatch.setattr(agent.app, "stream", boom)

    released = {"n": 0}
    run_log_id = "test-run-crash"
    thread = runs.start_background_run(
        run_log_id,
        "client_1",
        None,
        ["AI infrastructure"],
        trigger="manual",
        release_lock=lambda: released.__setitem__("n", released["n"] + 1),
    )
    thread.join(timeout=5)

    events = _wait_for_done(run_log_id)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_code"] == "RuntimeError"
    assert released["n"] == 1


def test_release_lock_is_called_on_timeout(monkeypatch):
    monkeypatch.setattr(runs, "RUN_TIMEOUT_SECONDS", 0)

    def slow_stream(input_state):
        yield {"supervisor": {"next": "researcher", "reason": "no research yet"}}
        time.sleep(0.05)
        yield {"researcher": {"research_done": True}}

    monkeypatch.setattr(agent.app, "stream", slow_stream)

    released = {"n": 0}
    run_log_id = "test-run-timeout"
    thread = runs.start_background_run(
        run_log_id,
        "client_1",
        None,
        ["AI infrastructure"],
        trigger="manual",
        release_lock=lambda: released.__setitem__("n", released["n"] + 1),
    )
    thread.join(timeout=5)

    events = _wait_for_done(run_log_id)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error_code"] == "TimeoutError"
    assert released["n"] == 1


def test_sse_stream_replays_events_after_last_event_id():
    run_id = "test-run-replay"
    runs.publish(run_id, "node", node="supervisor", reason="no research yet", stats={})
    runs.publish(run_id, "node", node="researcher", reason=None, stats={})
    runs.publish(run_id, "done", status="success", duration_ms=10, briefing_id="b1", error_code=None)

    chunks = list(runs.sse_stream(run_id, last_event_id=1))
    assert len(chunks) == 2
    assert "researcher" in chunks[0]
    assert '"type": "done"' in chunks[1]


def test_sse_stream_heartbeats_while_waiting(monkeypatch):
    monkeypatch.setattr(runs, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    run_id = "test-run-heartbeat"

    gen = runs.sse_stream(run_id)
    first_chunk = next(gen)
    assert first_chunk == ": heartbeat\n\n"
    gen.close()

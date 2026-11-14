"""Mocked-agent API server for load/abuse testing (Phase 10).

Runs the REAL FastAPI app (real middleware, auth dependency in demo mode,
real rate limiter on its in-memory store, real admission/lock/budget logic)
with exactly two things faked, so no live provider is EVER contacted:
  - the database: an in-memory Prisma stand-in (tests/conftest.py's FakeDB),
    pre-seeded with demo clients and briefings;
  - the agent graph: `agent.app.stream` is replaced by a fast fake that
    sleeps briefly per node and saves a briefing, so /run exercises the real
    background-run machinery (threads, locks, SSE channel, RunLog) cheaply.

    python scripts/loadtest/mock_server.py            # serves on :8011

Env knobs (set BEFORE launching; read at import by the real modules):
    GLOBAL_API_LIMIT, RUN_LIMIT, MAX_CONCURRENT_RUNS, MOCK_NODE_SECONDS,
    LOADTEST_PORT.
NOT representative of production latency: there is no network database, no
TLS, one process, localhost. It measures framework + middleware + limiter
overhead and the correctness of the abuse controls, nothing more."""

import os
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))

# Isolate from a developer's real backend/.env BEFORE anything imports it:
# blank Clerk vars -> demo auth; mock DB -> memory rate-limit store; never demo
# fixtures; no metrics token.
for k, v in {
    "MOCK_DB": "1",
    "RATELIMIT_STORE": "memory",
    "DEMO_MODE": "false",
    "APP_ENV": "dev",
    "CLERK_JWKS_URL": "",
    "CLERK_ISSUER": "",
    "METRICS_TOKEN": "",
    "LOG_LEVEL": "WARNING",
    "INNGEST_DEV": "1",
}.items():
    os.environ[k] = v

import conftest as fakes  # noqa: E402  (FakeDB + friends; sets safe test env as a side effect)

import agent  # noqa: E402
import api  # noqa: E402
import llm_client  # noqa: E402
import runs  # noqa: E402

NODE_SECONDS = float(os.environ.get("MOCK_NODE_SECONDS", "0.15"))


def build_fake_db():
    db = fakes.FakeDB()
    for i in range(5):
        cid = f"demo-{i}"
        db.clientprofile._store[cid] = fakes.FakeClientProfile(
            cid, name=f"Demo Client {i}", portfolio_themes=["AI infrastructure", "Energy transition", "Rates"]
        )
        for j in range(5):
            db.briefing._rows.append(
                fakes.FakeBriefing(
                    f"b-{i}-{j}",
                    cid,
                    content=("Market summary paragraph. " * 120),
                    status="compliant",
                    complianceViolations=[],
                )
            )
    return db


class FakeApp:
    """Stands in for the compiled LangGraph: same `.stream(state)` contract
    (yields {node: delta} dicts), but no provider call anywhere."""

    def stream(self, state):
        for node in ("supervisor", "researcher", "supervisor", "retriever", "supervisor", "writer", "critic"):
            time.sleep(NODE_SECONDS)
            yield {node: {"reason": f"mock {node}", "next": "x"}}
        FAKE_DB.briefing.create(
            data={
                "clientProfileId": state["client_id"],
                "content": "Mock briefing.",
                "status": "compliant",
                "runId": state["run_id"],
            }
        )
        yield {"save": {}}


FAKE_DB = build_fake_db()
api.db = FAKE_DB
agent.app = FakeApp()
agent.get_db = lambda: FAKE_DB
agent.close_db = lambda: None
llm_client.check_daily_budget = lambda provider="gemini": {"allowed": True, "used": 0, "limit": 18}
runs.recover_stuck_runs = lambda db, timeout_seconds=0: 0

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api.app, host="127.0.0.1", port=int(os.environ.get("LOADTEST_PORT", "8011")), log_level="warning")

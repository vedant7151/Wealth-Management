import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["RATELIMIT_STORE"] = "memory"
os.environ.setdefault("MOCK_DB", "1")
# Forced, not setdefault: whether this dev environment's real backend/.env
# happens to have GEMINI_API_KEY set must never change what the test
# suite exercises (DEMO_MODE=auto would silently flip api.py's /run route
# onto the demo-fixture path the moment a real key is present/absent,
# which is exactly the nondeterminism Phase 5's flaky-test fix elsewhere
# in this file was written to avoid). Phase 8's demo-mode-specific tests
# override this explicitly per test via monkeypatch.
os.environ["DEMO_MODE"] = "false"

import pytest  # noqa: E402

import ratelimit  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ratelimit_state():
    """Every test gets a clean in-memory store and a forced in-memory
    store selection, so no test call ever touches a real DB/Redis."""
    ratelimit.configure(db_getter=None)
    ratelimit._memory_store.reset()
    yield
    ratelimit._memory_store.reset()


class FakeClientProfile:
    def __init__(self, id, name="Test Client", portfolio_themes=None, clerkUserId=None):
        self.id = id
        self.name = name
        self.portfolio_themes = portfolio_themes or ["AI infrastructure"]
        self.clerkUserId = clerkUserId


class FakeClientProfileTable:
    def __init__(self, store: dict):
        self._store = store

    def find_unique(self, where):
        return self._store.get(where["id"])

    def find_many(self, where=None, take=None, skip=None, order=None):
        where = where or {}
        rows = list(self._store.values())
        if "clerkUserId" in where:
            rows = [c for c in rows if c.clerkUserId == where["clerkUserId"]]
        rows = rows[skip or 0 :]
        return rows[:take] if take is not None else rows

    def update(self, where, data):
        row = self._store[where["id"]]
        for k, v in data.items():
            setattr(row, k, v)
        return row

    def delete(self, where):
        return self._store.pop(where["id"], None)

    def create(self, data):
        new_id = f"client-{len(self._store) + 1}"
        client = FakeClientProfile(
            new_id, name=data["name"], portfolio_themes=data["portfolio_themes"], clerkUserId=data.get("clerkUserId")
        )
        self._store[new_id] = client
        return client


class FakeBriefing:
    """Minimal Briefing stand-in for api.py's Phase 7 approval/edit routes
    -- a plain mutable object so `db.briefing.update(...)` can set
    attributes directly, mirroring how the real Prisma record behaves."""

    def __init__(self, id, clientProfileId, content="Draft content.", status="needs_human_review", **kwargs):
        import datetime

        self.id = id
        self.clientProfileId = clientProfileId
        self.content = content
        self.status = status
        self.createdAt = kwargs.get("createdAt", datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc))
        self.complianceViolations = kwargs.get("complianceViolations", [])
        self.revisionCount = kwargs.get("revisionCount", 0)
        self.runId = kwargs.get("runId")
        self.citations = kwargs.get("citations", [])
        self.revisions = kwargs.get("revisions", [])
        self.reviewedAt = kwargs.get("reviewedAt")
        self.reviewedBy = kwargs.get("reviewedBy")
        self.overrideReason = kwargs.get("overrideReason")
        self.isDemo = kwargs.get("isDemo", False)


class FakeBriefingTable:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def find_many(self, where=None, include=None, order=None, take=None, skip=None):
        where = where or {}
        rows = self._rows
        if "clientProfileId" in where:
            rows = [r for r in rows if r.clientProfileId == where["clientProfileId"]]
        if "status" in where:
            rows = [r for r in rows if str(r.status) == where["status"]]
        rows = rows[skip or 0 :]
        return rows[:take] if take is not None else rows

    def find_first(self, where=None, include=None, order=None):
        rows = self.find_many(where=where)
        return rows[0] if rows else None

    def find_unique(self, where, include=None):
        for r in self._rows:
            if r.id == where["id"]:
                return r
        return None

    def update(self, where, data):
        row = self.find_unique(where)
        if row is None:
            return None
        for k, v in data.items():
            setattr(row, k, _unwrap_json(v))
        return row

    def create(self, data):
        new_id = f"briefing-{len(self._rows) + 1}"
        row = FakeBriefing(
            id=new_id,
            clientProfileId=data["clientProfileId"],
            content=data.get("content", ""),
            status=_unwrap_json(data.get("status", "draft")),
            complianceViolations=_unwrap_json(data.get("complianceViolations")) or [],
            revisionCount=data.get("revisionCount", 0),
            runId=data.get("runId"),
            isDemo=data.get("isDemo", False),
        )
        self._rows.append(row)
        return row


class FakeCitationTable:
    def __init__(self):
        self.created = []

    def create(self, data):
        self.created.append(data)
        return None


def _unwrap_json(value):
    """Mirrors what the real Prisma client does on write+read-back for a
    Json column: a caller-supplied `prisma.Json(...)` wrapper's `.data` is
    what actually gets persisted/returned, never the wrapper object
    itself. FakeBriefingTable has no real DB round-trip to do that
    automatically, so it's done here instead."""
    from prisma import Json

    return value.data if isinstance(value, Json) else value


class FakeBriefingRevisionTable:
    def __init__(self):
        self.created = []

    def create(self, data):
        self.created.append(dict(data, violations=_unwrap_json(data.get("violations"))))
        return None


class FakeHolding:
    def __init__(self, id, clientProfileId, ticker, sector, weight):
        self.id = id
        self.clientProfileId = clientProfileId
        self.ticker = ticker
        self.sector = sector
        self.weight = weight


class FakeHoldingTable:
    def __init__(self, store=None):
        self._store = store if store is not None else {}
        self._counter = 0

    def find_many(self, where=None):
        where = where or {}
        rows = list(self._store.values())
        if "clientProfileId" in where:
            rows = [r for r in rows if r.clientProfileId == where["clientProfileId"]]
        return rows

    def delete_many(self, where=None):
        where = where or {}
        cpid = where.get("clientProfileId")
        for hid in [h.id for h in self._store.values() if cpid is None or h.clientProfileId == cpid]:
            self._store.pop(hid, None)

    def create(self, data):
        self._counter += 1
        new_id = f"holding-{self._counter}"
        row = FakeHolding(
            id=new_id,
            clientProfileId=data["clientProfileId"],
            ticker=data["ticker"],
            sector=data.get("sector"),
            weight=data["weight"],
        )
        self._store[new_id] = row
        return row


class FakeDeliveryLogTable:
    def __init__(self):
        self._rows = []

    def find_first(self, where=None):
        where = where or {}
        for r in self._rows:
            if all(getattr(r, k, None) == v for k, v in where.items()):
                return r
        return None

    def create(self, data):
        row = type("FakeDeliveryLogRecord", (), dict(data, id=f"delivery-{len(self._rows) + 1}"))()
        self._rows.append(row)
        return row


class FakeRunLogRecord:
    def __init__(self, id, clientProfileId, userId, trigger, status, idempotencyKey=None):
        self.id = id
        self.clientProfileId = clientProfileId
        self.userId = userId
        self.trigger = trigger
        self.status = status
        self.idempotencyKey = idempotencyKey
        self.startedAt = "2026-11-12T00:00:00+00:00"
        self.finishedAt = None
        self.durationMs = None
        self.nodeTimeline = None
        self.errorCode = None
        self.briefingId = None


class FakeRunLogTable:
    """Minimal Prisma stand-in for the `RunLog` model (Phase 6) -- same
    spirit as FakeClientProfileTable/FakeBriefingTable above: just enough
    for api.py's run-creation/history/ownership routes, no network/DB."""

    def __init__(self, store: dict):
        self._store = store
        self._counter = 0

    def find_unique(self, where):
        return self._store.get(where["id"])

    def find_many(self, where=None, order=None, take=None):
        where = where or {}
        rows = [r for r in self._store.values() if all(getattr(r, k, None) == v for k, v in where.items())]
        rows.sort(key=lambda r: r.startedAt, reverse=True)
        return rows[:take] if take else rows

    def create(self, data):
        self._counter += 1
        new_id = f"run-{self._counter}"
        row = FakeRunLogRecord(
            id=new_id,
            clientProfileId=data["clientProfileId"],
            userId=data.get("userId"),
            trigger=data.get("trigger", "manual"),
            status=data.get("status", "running"),
            idempotencyKey=data.get("idempotencyKey"),
        )
        self._store[new_id] = row
        return row

    def update(self, where, data):
        row = self._store[where["id"]]
        for k, v in data.items():
            setattr(row, k, v)
        return row


class FakeAuditLogTable:
    def __init__(self):
        self.created = []

    def create(self, data):
        self.created.append(dict(data, metadata=_unwrap_json(data.get("metadata"))))
        return None


class _FakeBatch:
    """Minimal stand-in for Prisma's `db.batch_()` context manager -- just
    enough for holdings.replace_holdings's delete_many + create calls,
    applied directly against the same FakeHoldingTable (no real
    transaction semantics needed for a test double)."""

    def __init__(self, db):
        self.holding = db.holding

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDB:
    """Minimal Prisma stand-in for api.py's module-level `db`: supports
    exactly the calls the auth/IDOR/rate-limit/Phase-7 tests need, with no
    network or real database involved. Not a general Prisma mock -- a
    different, richer one already exists for agent.py's own tests
    (agent.MockDB)."""

    def __init__(self, clients=None, briefings=None, run_logs=None, holdings=None):
        self.clientprofile = FakeClientProfileTable(clients or {})
        self.briefing = FakeBriefingTable(briefings or [])
        self.briefingrevision = FakeBriefingRevisionTable()
        self.citation = FakeCitationTable()
        self.runlog = FakeRunLogTable(run_logs or {})
        self.holding = FakeHoldingTable(holdings)
        self.deliverylog = FakeDeliveryLogTable()
        self.auditlog = FakeAuditLogTable()

    def connect(self):
        pass

    def disconnect(self):
        pass

    def query_raw(self, sql, *args):
        return [{"cnt": 0}]

    def execute_raw(self, *args, **kwargs):
        return None

    def batch_(self):
        return _FakeBatch(self)

    def tx(self, **kwargs):
        """Records dependent-table deletes; the client row itself is really removed."""
        from unittest.mock import MagicMock

        outer = self

        class _Tx:
            def __init__(self):
                self.citation = MagicMock()
                self.briefingrevision = MagicMock()
                self.deliverylog = MagicMock()
                self.auditlog = MagicMock()
                self.briefing = MagicMock()
                self.runlog = MagicMock()
                self.holding = MagicMock()
                self.clientprofile = outer.clientprofile

            def __enter__(self):
                outer.last_tx = self
                return self

            def __exit__(self, *exc):
                return False

        return _Tx()


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def api_client(monkeypatch, fake_db):
    """Factory: api_client(user_id=None) returns a TestClient for api.py
    wired to `fake_db` (no real Prisma/network). Pass user_id to override
    get_current_user (bypassing real Clerk JWT verification); omit it to
    exercise the real dependency (e.g. to test the no-token 401 path)."""
    import api
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api, "db", fake_db)

    def _make(user_id=None):
        if user_id is not None:
            api.app.dependency_overrides[api.get_current_user] = lambda: user_id
            # Same override, for the SSE route's separate dependency (it
            # also accepts a `?token=` query param EventSource can set
            # instead of a header -- see api._get_current_user_for_stream).
            api.app.dependency_overrides[api._get_current_user_for_stream] = lambda: user_id
        else:
            api.app.dependency_overrides.pop(api.get_current_user, None)
            api.app.dependency_overrides.pop(api._get_current_user_for_stream, None)
        return TestClient(api.app)

    yield _make
    api.app.dependency_overrides.clear()

"""Pluggable sliding-window rate limiting.

Store selection (first match wins):
  - MOCK_DB=1 or RATELIMIT_STORE=memory -> in-memory (tests / local offline runs)
  - REDIS_URL set                       -> Redis-backed (multi-instance safe)
  - otherwise                           -> Postgres-backed, using the
                                            additive RateLimitHit table, via
                                            whatever Prisma connection the
                                            caller supplies through configure()

Policies (RUN_LIMIT, GLOBAL_API_LIMIT, WRITE_LIMIT, DAILY_LLM_BUDGET and
their windows) are read from the environment once at import time, with the
documented defaults, so a process restart is all that's needed to pick up a
changed env var -- no other wiring required.

Atomicity (Phase 10 -- this replaces the earlier "accepted race" note):
the Postgres store's "count, then conditionally insert" is NOT safe by
itself under READ COMMITTED -- two transactions on the same key can both
count N-1 and both insert. For the policies that guard scarce/paid
resources (`fail_mode="closed"`: `run`, `llm_budget`) and for the per-client
run lock, the check now runs inside a short transaction that first takes
`pg_advisory_xact_lock(hashtext(key))`; the lock serializes same-key
callers and the count statement that follows takes a fresh snapshot AFTER
acquiring it, so a 4th concurrent request cannot slip past a limit of 3.
(Taking the lock inside the same statement as the count would NOT work: a
statement's snapshot is fixed before the lock wait.) The cheap
single-round-trip CTE is kept for the fail-open read/write policies, where
a rare off-by-one under contention is acceptable. The in-memory store is
guarded by a threading.Lock; the Redis store is Lua-scripted.
See backend/tests/test_ratelimit_concurrency.py and
backend/scripts/verify_ratelimit_pg.py (the latter exercises the real SQL).
"""

import datetime
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import metrics  # noqa: E402

logger = logging.getLogger("ratelimit")


# --------------------------------------------------------------------------
# Env-configurable policy defaults
# --------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


RUN_LIMIT = _int_env("RUN_LIMIT", 3)
RUN_WINDOW_SECONDS = _int_env("RUN_WINDOW_SECONDS", 3600)
GLOBAL_API_LIMIT = _int_env("GLOBAL_API_LIMIT", 120)
GLOBAL_API_WINDOW_SECONDS = 60
WRITE_LIMIT = _int_env("WRITE_LIMIT", 20)
WRITE_WINDOW_SECONDS = 60
DAILY_LLM_BUDGET = _int_env("DAILY_LLM_BUDGET", 18)
DAILY_LLM_BUDGET_WINDOW_SECONDS = 86400
RUN_LOCK_TTL_SECONDS = _int_env("RUN_LOCK_TTL_SECONDS", 300)
# Phase 7: PDF generation and CSV holdings upload get their own limits,
# separate from the general "write" policy, since they're heavier
# (PDF rendering, parsing/validating a file) than an ordinary POST/PATCH.
PDF_LIMIT = _int_env("PDF_LIMIT", 10)
PDF_WINDOW_SECONDS = _int_env("PDF_WINDOW_SECONDS", 60)
UPLOAD_LIMIT = _int_env("UPLOAD_LIMIT", 5)
UPLOAD_WINDOW_SECONDS = _int_env("UPLOAD_WINDOW_SECONDS", 60)


@dataclass(frozen=True)
class Policy:
    limit: int
    window_seconds: int
    fail_mode: str  # "open" or "closed": behavior when the store itself errors


POLICIES = {
    # /run's own per-user quota and the shared daily LLM budget both protect
    # the scarce/paid resource, so if the store is unreachable we fail
    # CLOSED (reject the run) rather than risk burning quota blind.
    "run": Policy(RUN_LIMIT, RUN_WINDOW_SECONDS, fail_mode="closed"),
    "llm_budget": Policy(DAILY_LLM_BUDGET, DAILY_LLM_BUDGET_WINDOW_SECONDS, fail_mode="closed"),
    # General API traffic and writes fail OPEN: a down limiter store
    # shouldn't take the whole read/write API down with it.
    "global_api": Policy(GLOBAL_API_LIMIT, GLOBAL_API_WINDOW_SECONDS, fail_mode="open"),
    "write": Policy(WRITE_LIMIT, WRITE_WINDOW_SECONDS, fail_mode="open"),
    # PDF rendering and CSV upload are heavier than an ordinary write but
    # don't touch the scarce/paid LLM budget, so they fail OPEN too -- a
    # down limiter store shouldn't block a PDF download or holdings upload.
    "pdf": Policy(PDF_LIMIT, PDF_WINDOW_SECONDS, fail_mode="open"),
    "upload": Policy(UPLOAD_LIMIT, UPLOAD_WINDOW_SECONDS, fail_mode="open"),
}


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_at: float  # epoch seconds
    retry_after: int  # seconds


class RateLimitStoreError(Exception):
    """Raised by a store implementation when it cannot answer a check at
    all (e.g. DB/Redis connection failure) -- distinct from a normal
    "not allowed" result."""


# --------------------------------------------------------------------------
# Store implementations
# --------------------------------------------------------------------------


class _InMemoryStore:
    """Single-process sliding window. Used for MOCK_DB/tests and as the
    last-resort fallback. Not shared across instances -- fine for tests and
    local dev, not for a real multi-instance deployment."""

    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._locks: dict[str, float] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, window_seconds: int, limit: int, now: Optional[float] = None) -> RateLimitResult:
        now = now if now is not None else time.time()
        with self._lock:
            bucket = self._hits.setdefault(key, [])
            cutoff = now - window_seconds
            bucket[:] = [t for t in bucket if t > cutoff]
            allowed = len(bucket) < limit
            if allowed:
                bucket.append(now)
            remaining = max(0, limit - len(bucket))
            reset_at = (min(bucket) + window_seconds) if bucket else now + window_seconds
            return RateLimitResult(allowed, limit, remaining, reset_at, max(0, int(reset_at - now)))

    def peek(self, key: str, window_seconds: int, limit: int, now: Optional[float] = None) -> RateLimitResult:
        now = now if now is not None else time.time()
        with self._lock:
            bucket = self._hits.get(key, [])
            cutoff = now - window_seconds
            active = [t for t in bucket if t > cutoff]
            remaining = max(0, limit - len(active))
            reset_at = (min(active) + window_seconds) if active else now + window_seconds
            return RateLimitResult(len(active) < limit, limit, remaining, reset_at, max(0, int(reset_at - now)))

    def try_lock(self, key: str, ttl_seconds: int, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            expiry = self._locks.get(key)
            if expiry is not None and expiry > now:
                return False
            self._locks[key] = now + ttl_seconds
            return True

    def release_lock(self, key: str) -> None:
        with self._lock:
            self._locks.pop(key, None)

    def reset(self) -> None:
        """Test helper: clear all state."""
        with self._lock:
            self._hits.clear()
            self._locks.clear()


def _epoch(value) -> Optional[float]:
    """Prisma's query_raw sometimes returns a raw timestamp column as a
    native datetime and sometimes as an ISO-formatted string, depending on
    the query shape (plain column vs. an aggregate/CTE alias) -- normalize
    either into epoch seconds."""
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return value.timestamp()
    import datetime

    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


class _PostgresStore:
    """Sliding window backed by the additive RateLimitHit(key, ts) table,
    shared by every backend instance talking to the same Neon/Postgres DB.
    See module docstring for the isolation caveat."""

    def __init__(self, db_getter: Callable):
        self._db_getter = db_getter

    def _db(self):
        return self._db_getter()

    def _serialized(self, key: str, fn):
        """Runs `fn(tx)` inside a transaction holding a per-key advisory
        lock (released automatically at commit/rollback)."""
        db = self._db()
        # Prisma's default tx start window (max_wait=2s) is too short when
        # callers queue behind the advisory lock on a small pool; give the
        # wait room, but bounded (a timeout surfaces as RateLimitStoreError,
        # i.e. fail-closed for run/budget).
        with db.tx(max_wait=datetime.timedelta(seconds=10), timeout=datetime.timedelta(seconds=15)) as tx:
            tx.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", key)
            return fn(tx)

    def hit(
        self, key: str, window_seconds: int, limit: int, now: Optional[float] = None, strict: bool = False
    ) -> RateLimitResult:
        import datetime

        now_dt = (
            datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
            if now
            else datetime.datetime.now(datetime.timezone.utc)
        )
        cutoff_dt = now_dt - datetime.timedelta(seconds=window_seconds)
        try:
            db = self._db()
            run_query = (
                (lambda q, *a: self._serialized(key, lambda tx: tx.query_raw(q, *a))) if strict else db.query_raw
            )
            rows = run_query(
                """
                WITH active AS (
                    SELECT count(*) AS cnt, min(ts) AS oldest
                    FROM "RateLimitHit"
                    WHERE key = $1 AND ts > $2::timestamp
                ),
                ins AS (
                    INSERT INTO "RateLimitHit" (id, key, ts)
                    SELECT $3, $1, $4::timestamp
                    WHERE (SELECT cnt FROM active) < $5
                    RETURNING ts
                )
                SELECT (SELECT cnt FROM active) AS cnt_before,
                       (SELECT oldest FROM active) AS oldest_ts,
                       (SELECT ts FROM ins) AS inserted_ts
                """,
                key,
                cutoff_dt,
                str(uuid.uuid4()),
                now_dt,
                limit,
            )
        except Exception as e:  # noqa: BLE001 - any DB failure becomes a store error
            raise RateLimitStoreError(str(e)) from e

        row = rows[0] if rows else {}
        cnt_before = row.get("cnt_before") or 0
        inserted = row.get("inserted_ts") is not None
        oldest_ts = row.get("oldest_ts")
        count_after = cnt_before + (1 if inserted else 0)
        remaining = max(0, limit - count_after)
        now_f = now if now is not None else time.time()
        oldest_epoch = _epoch(oldest_ts)
        reset_at = (oldest_epoch + window_seconds) if oldest_epoch is not None else now_f + window_seconds
        return RateLimitResult(inserted, limit, remaining, reset_at, max(0, int(reset_at - now_f)))

    def peek(self, key: str, window_seconds: int, limit: int, now: Optional[float] = None) -> RateLimitResult:
        import datetime

        now_dt = (
            datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
            if now
            else datetime.datetime.now(datetime.timezone.utc)
        )
        cutoff_dt = now_dt - datetime.timedelta(seconds=window_seconds)
        try:
            db = self._db()
            rows = db.query_raw(
                """
                SELECT count(*) AS cnt, min(ts) AS oldest
                FROM "RateLimitHit"
                WHERE key = $1 AND ts > $2::timestamp
                """,
                key,
                cutoff_dt,
            )
        except Exception as e:  # noqa: BLE001
            raise RateLimitStoreError(str(e)) from e

        row = rows[0] if rows else {}
        cnt = row.get("cnt") or 0
        oldest_ts = row.get("oldest")
        remaining = max(0, limit - cnt)
        now_f = now if now is not None else time.time()
        oldest_epoch = _epoch(oldest_ts)
        reset_at = (oldest_epoch + window_seconds) if oldest_epoch is not None else now_f + window_seconds
        return RateLimitResult(cnt < limit, limit, remaining, reset_at, max(0, int(reset_at - now_f)))

    def try_lock(self, key: str, ttl_seconds: int, now: Optional[float] = None) -> bool:
        import datetime

        now_dt = (
            datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
            if now
            else datetime.datetime.now(datetime.timezone.utc)
        )
        stale_cutoff = now_dt - datetime.timedelta(seconds=ttl_seconds)
        lock_key = f"lock:{key}"

        def _acquire(tx):
            tx.execute_raw(
                'DELETE FROM "RateLimitHit" WHERE key = $1 AND ts < $2::timestamp',
                lock_key,
                stale_cutoff,
            )
            return tx.query_raw(
                """
                INSERT INTO "RateLimitHit" (id, key, ts)
                SELECT $2, $1, $3::timestamp
                WHERE NOT EXISTS (SELECT 1 FROM "RateLimitHit" WHERE key = $1)
                RETURNING ts
                """,
                lock_key,
                str(uuid.uuid4()),
                now_dt,
            )

        try:
            # Advisory-locked transaction: without it two requests can both
            # see "no lock row" and both insert one (READ COMMITTED).
            rows = self._serialized(lock_key, _acquire)
        except Exception as e:  # noqa: BLE001
            raise RateLimitStoreError(str(e)) from e
        return bool(rows)

    def release_lock(self, key: str) -> None:
        try:
            db = self._db()
            db.execute_raw('DELETE FROM "RateLimitHit" WHERE key = $1', f"lock:{key}")
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to release lock %s: %s", key, e)

    def cleanup_expired(self, older_than_seconds: int = 86400 * 2) -> int:
        import datetime

        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=older_than_seconds)
        db = self._db()
        return db.execute_raw('DELETE FROM "RateLimitHit" WHERE ts < $1::timestamp', cutoff)


class _RedisStore:
    """Sliding window via a Redis sorted set, atomic via a Lua script.
    Not verified against a live Redis instance in this environment (no
    REDIS_URL configured here) -- exercised only by unit tests with
    fakeredis-style behavior if/when added. See docs/PROGRESS.md."""

    _SCRIPT = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    local allowed = 0
    if count < limit then
        redis.call('ZADD', key, now, member)
        redis.call('EXPIRE', key, window)
        allowed = 1
        count = count + 1
    end
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local oldest_ts = now
    if #oldest > 0 then
        oldest_ts = tonumber(oldest[2])
    end
    return {allowed, count, oldest_ts}
    """

    def __init__(self, redis_url: str):
        import redis as redis_lib

        self._client = redis_lib.from_url(redis_url)
        self._script = self._client.register_script(self._SCRIPT)

    def hit(self, key: str, window_seconds: int, limit: int, now: Optional[float] = None) -> RateLimitResult:
        now = now if now is not None else time.time()
        try:
            allowed, count, oldest_ts = self._script(
                keys=[key], args=[now, window_seconds, limit, f"{now}:{uuid.uuid4()}"]
            )
        except Exception as e:  # noqa: BLE001
            raise RateLimitStoreError(str(e)) from e
        remaining = max(0, limit - int(count))
        reset_at = float(oldest_ts) + window_seconds
        return RateLimitResult(bool(int(allowed)), limit, remaining, reset_at, max(0, int(reset_at - now)))

    def peek(self, key: str, window_seconds: int, limit: int, now: Optional[float] = None) -> RateLimitResult:
        now = now if now is not None else time.time()
        try:
            self._client.zremrangebyscore(key, 0, now - window_seconds)
            count = self._client.zcard(key)
        except Exception as e:  # noqa: BLE001
            raise RateLimitStoreError(str(e)) from e
        remaining = max(0, limit - int(count))
        return RateLimitResult(int(count) < limit, limit, remaining, now + window_seconds, window_seconds)

    def try_lock(self, key: str, ttl_seconds: int, now: Optional[float] = None) -> bool:
        try:
            return bool(self._client.set(f"lock:{key}", "1", nx=True, ex=ttl_seconds))
        except Exception as e:  # noqa: BLE001
            raise RateLimitStoreError(str(e)) from e

    def release_lock(self, key: str) -> None:
        try:
            self._client.delete(f"lock:{key}")
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to release lock %s: %s", key, e)


# --------------------------------------------------------------------------
# Store selection + public API
# --------------------------------------------------------------------------

_memory_store = _InMemoryStore()
_configured_store: Optional["_InMemoryStore | _RedisStore | _PostgresStore"] = None


def configure(db_getter: Optional[Callable] = None) -> None:
    """Call once at app startup with a callable returning a connected
    Prisma client (api.py passes `lambda: db` for its already-connected
    module-level client). Safe to call multiple times (e.g. in tests)."""
    global _configured_store
    if os.environ.get("MOCK_DB") == "1" or os.environ.get("RATELIMIT_STORE") == "memory":
        _configured_store = _memory_store
        return
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        _configured_store = _RedisStore(redis_url)
        return
    if db_getter is not None:
        _configured_store = _PostgresStore(db_getter)
        return
    _configured_store = _memory_store


def _store():
    if _configured_store is None:
        configure(None)
    return _configured_store


def _fallback_result(policy: Policy, now: Optional[float] = None) -> RateLimitResult:
    now = now if now is not None else time.time()
    if policy.fail_mode == "closed":
        return RateLimitResult(False, policy.limit, 0, now + policy.window_seconds, policy.window_seconds)
    return RateLimitResult(True, policy.limit, policy.limit, now + policy.window_seconds, 0)


def check(policy_name: str, identity: str, now: Optional[float] = None) -> RateLimitResult:
    """Consume one slot for `identity` under `policy_name`. Returns the
    result either way -- callers decide what to do with `allowed=False`."""
    policy = POLICIES[policy_name]
    key = f"{policy_name}:{identity}"
    store = _store()
    try:
        if policy.fail_mode == "closed" and isinstance(store, _PostgresStore):
            result = store.hit(key, policy.window_seconds, policy.limit, now=now, strict=True)
        else:
            result = store.hit(key, policy.window_seconds, policy.limit, now=now)
        if not result.allowed:
            metrics.RATE_LIMIT_REJECTIONS.labels(policy_name).inc()
        return result
    except RateLimitStoreError as e:
        logger.error(
            "Rate limit store error on policy=%s identity=%s: %s -- failing %s",
            policy_name,
            identity,
            e,
            policy.fail_mode,
        )
        return _fallback_result(policy, now=now)


def peek(policy_name: str, identity: str, now: Optional[float] = None) -> RateLimitResult:
    """Read-only check: does not consume a slot. Used by GET /api/me/limits."""
    policy = POLICIES[policy_name]
    key = f"{policy_name}:{identity}"
    try:
        return _store().peek(key, policy.window_seconds, policy.limit, now=now)
    except RateLimitStoreError as e:
        logger.error(
            "Rate limit store error (peek) on policy=%s identity=%s: %s -- failing %s",
            policy_name,
            identity,
            e,
            policy.fail_mode,
        )
        return _fallback_result(policy, now=now)


def try_lock(key: str, ttl_seconds: int = RUN_LOCK_TTL_SECONDS, now: Optional[float] = None) -> bool:
    try:
        acquired = _store().try_lock(key, ttl_seconds, now=now)
        if not acquired:
            metrics.RATE_LIMIT_REJECTIONS.labels("client_run_lock").inc()
        return acquired
    except RateLimitStoreError as e:
        # Concurrency lock failures fail CLOSED (same reasoning as /run
        # itself: we'd rather reject a run than risk two in flight at once).
        logger.error("Rate limit store error (try_lock) on key=%s: %s -- failing closed", key, e)
        return False


def release_lock(key: str) -> None:
    try:
        _store().release_lock(key)
    except RateLimitStoreError as e:
        logger.error("Rate limit store error (release_lock) on key=%s: %s", key, e)


def store_health() -> dict:
    """Cheap, side-effect-free-ish health probe for GET /readyz (Phase 8):
    a `peek` on a throwaway key never inserts/consumes anything, so this
    is safe to call on every readiness check. Returns the store's class
    name (memory/Postgres/Redis) so an operator can see which backend is
    actually configured, not just whether it's reachable."""
    store = _store()
    try:
        store.peek("readyz:healthcheck", window_seconds=1, limit=1)
        return {"status": "ok", "store": type(store).__name__}
    except RateLimitStoreError as e:
        return {"status": "error", "store": type(store).__name__, "error": str(e)}

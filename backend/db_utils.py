"""Small DB helpers (Phase 10)."""

import datetime
import logging

logger = logging.getLogger("db")


def query_with_timeout(db, sql: str, *args, timeout_ms: int):
    """Runs a raw query under a server-side `statement_timeout`.

    pgvector similarity scans (`ORDER BY embedding <-> $1`) are the only
    queries in this app whose cost grows with table size (HNSW indexes are
    prepared but not applied -- see docs/OPERATIONS.md), so they get a hard
    cap instead of being allowed to pin a pooled connection. The timeout is
    applied with `SET LOCAL` inside a short transaction, which (unlike a
    session-level SET) is safe behind a PgBouncer/Neon pooled endpoint in
    transaction mode: it cannot leak onto the next client's statements.

    A timed-out statement raises (Postgres error 57014); callers already
    treat a retrieval/cache query failure as "no result" and continue.

    Falls back to a plain `query_raw` for stand-ins without `.tx()` (the
    offline MockDB)."""
    tx_factory = getattr(db, "tx", None)
    if tx_factory is None:
        return db.query_raw(sql, *args)
    # Prisma's own interactive-transaction timeout must outlast the
    # statement timeout, or it would fire first and mask the real cause.
    with tx_factory(
        max_wait=datetime.timedelta(seconds=10), timeout=datetime.timedelta(milliseconds=timeout_ms + 3000)
    ) as tx:
        tx.execute_raw(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        return tx.query_raw(sql, *args)

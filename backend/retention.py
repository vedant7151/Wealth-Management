"""Data retention (Phase 10): purges old, REGENERABLE operational rows.

Purged (age thresholds from settings.py / env):
  - DocumentChunk   (RETENTION_CHUNK_DAYS, default 14)       -- retrieval is
        windowed to 48h anyway; chunks are re-ingested by the next run.
  - SearchCache     (RETENTION_SEARCH_CACHE_DAYS, default 7)  -- 4h cache.
  - ApiCallLog      (RETENTION_API_CALL_LOG_DAYS, default 90) -- cost ledger;
        the daily budget only reads "today".
  - RateLimitHit    (RETENTION_RATE_LIMIT_HIT_DAYS, default 2) -- longest
        window is 24h; also clears long-dead run-lock rows.

NEVER touched, by construction: ClientProfile, Briefing, Citation,
BriefingRevision, AuditLog, RunLog, Holding, DeliveryLog. Table names come
from the fixed allowlist below (never from input), and a unit test asserts
no other table name can reach the SQL.

Safety: `purge()` is a DRY RUN unless called with `dry_run=False` (the CLI
needs an explicit `--execute`; the Inngest cron additionally needs
RETENTION_ENABLED=true). Deletes are batched (bounded lock time) with an
iteration cap."""

import datetime
import logging
from typing import Dict, Optional

import metrics

logger = logging.getLogger("retention")

# (table, timestamp column, settings attribute holding the max age in days)
PURGE_TARGETS = (
    ("DocumentChunk", "createdAt", "retention_chunk_days"),
    ("SearchCache", "createdAt", "retention_search_cache_days"),
    ("ApiCallLog", "createdAt", "retention_api_call_log_days"),
    ("RateLimitHit", "ts", "retention_rate_limit_hit_days"),
)
ALLOWED_TABLES = frozenset(t for t, _, _ in PURGE_TARGETS)
PROTECTED_TABLES = frozenset(
    {"ClientProfile", "Briefing", "Citation", "BriefingRevision", "AuditLog", "RunLog", "Holding", "DeliveryLog"}
)
BATCH_SIZE = 5000
MAX_BATCHES_PER_TABLE = 200


def purge(db, settings, dry_run: bool = True, now: Optional[datetime.datetime] = None) -> Dict[str, int]:
    """Returns {table: rows_deleted (or rows that WOULD be deleted, in a dry
    run)}. A failure on one table is logged and does not stop the others."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    report: Dict[str, int] = {}
    for table, column, attr in PURGE_TARGETS:
        if table not in ALLOWED_TABLES or table in PROTECTED_TABLES:  # explicit check, not assert (-O strips asserts)
            raise RuntimeError(f"retention refused: {table} is not an allowed purge target")
        days = getattr(settings, attr)
        cutoff = now - datetime.timedelta(days=days)
        try:
            # nosec B608: table/column come from the fixed PURGE_TARGETS allowlist, never input
            if dry_run:
                rows = db.query_raw(f'SELECT count(*) AS cnt FROM "{table}" WHERE "{column}" < $1::timestamp', cutoff)
                report[table] = int(rows[0]["cnt"]) if rows else 0
                continue
            total = 0
            for _ in range(MAX_BATCHES_PER_TABLE):
                deleted = db.execute_raw(
                    f'DELETE FROM "{table}" WHERE id IN '
                    f'(SELECT id FROM "{table}" WHERE "{column}" < $1::timestamp LIMIT {BATCH_SIZE})',
                    cutoff,
                )
                total += int(deleted or 0)
                if not deleted or int(deleted) < BATCH_SIZE:
                    break
            report[table] = total
            if total:
                metrics.RETENTION_DELETED.labels(table).inc(total)
        except Exception:  # noqa: BLE001
            logger.exception("Retention purge failed for %s", table)
            report[table] = -1
    logger.info("Retention %s: %s", "dry-run" if dry_run else "executed", report)
    return report

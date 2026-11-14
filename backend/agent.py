import logging
import os
import json
import math
import threading
import uuid
from typing import TypedDict, List, Optional
from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END
from prisma import Json
from prisma.enums import BriefingStatus as PrismaBriefingStatus

from logging_config import configure_logging, get_node_logger

# compliance_rules/compliance_judge are no longer called directly from
# this module (compliance_critic_node delegates to compliance_verdict,
# which imports them itself) -- kept as module-level names anyway because
# existing tests (test_critic_and_supervisor.py) monkeypatch
# `agent.compliance_rules`/`agent.compliance_judge` as a (harmless, no-op)
# re-bind of the same module object; removing them would break those
# monkeypatch.setattr calls with an AttributeError.
from compliance import rules as compliance_rules  # noqa: F401
from compliance import judge as compliance_judge  # noqa: F401
from compliance import verdict as compliance_verdict
from compliance import injection_guard
import audit
import db_utils
import llm_client
import settings as settings_module

# Load environment variables
load_dotenv()
configure_logging()

# Setup LangSmith. The per-environment project name (`<base>-<APP_ENV>`) is
# pushed into LANGCHAIN_PROJECT at API startup (settings.apply_runtime_env);
# a standalone agent/scheduler process applies it here too.
os.environ["LANGCHAIN_TRACING_V2"] = "true"
try:
    os.environ["LANGCHAIN_PROJECT"] = settings_module.get_settings().langsmith_project
except settings_module.SettingsError:
    pass  # the entry point (api.py) reports config errors; don't crash an import here

# Server-side cap on pgvector similarity queries (see db_utils.py).
VECTOR_QUERY_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000"))

# pgvector "<->" is L2 distance: lower = more similar. Tune based on observed
# distances for near-duplicate queries with bge-small-en-v1.5 embeddings.
CACHE_SIMILARITY_THRESHOLD = 0.35
CACHE_MAX_AGE_SQL_INTERVAL = "4 hours"

# Retrieval settings for the actual RAG step (retriever_node). Chunks older
# than this window are treated as stale and excluded from context.
RETRIEVAL_TOP_K = 4
# Recency + breadth (user feedback 2026-11-14: briefings cited months-old
# sources and only ~3 of them). Sources must have been PUBLISHED within
# SOURCE_MAX_AGE_DAYS (default 5 = today plus the previous 4 days); the
# briefing aims for up to MAX_CITATIONS distinct sources. Each theme fetches
# RETRIEVAL_CANDIDATES_PER_THEME chunks so that, after dropping stale ones,
# enough remain to fill the citation target.
SOURCE_MAX_AGE_DAYS = int(os.environ.get("SOURCE_MAX_AGE_DAYS", "5"))
MAX_CITATIONS = int(os.environ.get("MAX_CITATIONS", "8"))
RETRIEVAL_CANDIDATES_PER_THEME = int(os.environ.get("RETRIEVAL_CANDIDATES_PER_THEME", "16"))
TAVILY_MAX_RESULTS = int(os.environ.get("TAVILY_MAX_RESULTS", "10"))
MAX_CHUNKS_PER_URL = 2
RETRIEVAL_MAX_AGE_SQL_INTERVAL = "48 hours"
# Chunks whose distance from the theme embedding exceeds this are dropped
# rather than handed to the writer as if they were relevant -- unlike the
# ingestion cache's CACHE_SIMILARITY_THRESHOLD, retrieval previously had no
# cutoff at all (always returned up to RETRIEVAL_TOP_K regardless of
# relevance). Default is a rough starting point, not yet empirically tuned
# against a labeled eval set (that's Phase 4's job) -- env-overridable so it
# can be adjusted without a code change in the meantime.
RETRIEVAL_MAX_DISTANCE = float(os.environ.get("RETRIEVAL_MAX_DISTANCE", "1.0"))

# Chunking settings for article text ingested into DocumentChunk.
CHUNK_SIZE = 700
CHUNK_OVERLAP = 120
MAX_CHUNKS_PER_ARTICLE = 2

# Hard budget on how much retrieved context the writer prompt carries.
# Applied by selecting whole chunks (chunk boundaries, not a raw string
# slice) until the budget is reached, so a citation never claims a source
# whose content was actually cut off mid-sentence.
CONTEXT_CHAR_BUDGET = int(os.environ.get("CONTEXT_CHAR_BUDGET", "8000"))


def parse_published_date(value):
    """Best-effort parse of a source's published date (RFC 2822 as Tavily
    returns it, or ISO 8601). Returns an aware UTC datetime, or None if the
    value is missing/unparseable."""
    import datetime as _dt
    from email.utils import parsedate_to_datetime

    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def is_recent_enough(value, max_age_days=None, now=None) -> bool:
    """True if the source was published within the recency window. An
    unparseable/missing date is treated as NOT recent: an undatable source
    can't be shown to satisfy the recency requirement."""
    import datetime as _dt

    dt = parse_published_date(value)
    if dt is None:
        return False
    now = now or _dt.datetime.now(_dt.timezone.utc)
    max_age_days = SOURCE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    return (now - dt) <= _dt.timedelta(days=max_age_days) and dt <= now + _dt.timedelta(days=1)


# Client names never enter GraphState (only client_id, a UUID, does), so
# there is nothing today that would put a client's real name into a
# LangSmith trace. This flag exists so that stays true on purpose, not by
# accident, if a future change adds a name field to the state -- any code
# that logs/traces a human-readable client label should check it first.
REDACT_CLIENT_NAMES_IN_TRACES = os.environ.get("REDACT_CLIENT_NAMES_IN_TRACES", "true").lower() != "false"

# Revision loop cap (writer <-> critic). Hit with unresolved violations ->
# status needs_human_review, never "compliant" by timeout -- see
# supervisor_node's Stage 5 and compliance_critic_node.
MAX_REVISIONS = 2


class MockDB:
    """In-memory stand-in for Prisma. Used only by the local __main__ test
    block (MOCK_DB=1) so local runs never touch the real Neon database.

    Actually stores SearchCache/DocumentChunk rows in class-level lists (per
    Python process) and does a naive L2 nearest-neighbor scan, so the local
    test run exercises the same cache-skip and retrieval logic as production
    instead of always seeing an empty table."""

    _search_cache: List[dict] = []
    _document_chunks: List[dict] = []

    class _Briefing:
        """Stores created rows (keyed by id) so runs.py's background runner
        can look a saved briefing back up by runId under MOCK_DB=1, the same
        way it would via a real `find_first(where={"runId": ...})` call."""

        _rows: dict = {}

        def create(self, data):
            row = dict(data)
            row.setdefault("id", f"mock-briefing-{len(MockDB._Briefing._rows) + 1}")
            MockDB._Briefing._rows[row["id"]] = row
            return type("MockBriefingRecord", (), row)()

        def update(self, where, data):
            row = MockDB._Briefing._rows.get(where["id"])
            if row is not None:
                row.update(data)
            return None

        def find_first(self, where=None, include=None, order=None):
            where = where or {}
            rows = [r for r in MockDB._Briefing._rows.values() if all(r.get(k) == v for k, v in where.items())]
            return type("MockBriefingRecord", (), rows[0])() if rows else None

    class _Citation:
        def create(self, data):
            return None

    class _BriefingRevision:
        def create(self, data):
            return None

    class _AuditLog:
        def create(self, data):
            return None

    class _ApiCallLog:
        def create(self, data):
            return None

    class _Holding:
        """Offline __main__/MOCK_DB stand-in -- always empty (no holdings
        pre-seeded), so researcher_node's ticker/sector-aware query path
        falls back to themes-only in local testing, same as a real new
        client with no CSV upload yet."""

        def find_many(self, where=None):
            return []

    class _RunLog:
        """Backs runs.py's background-run bookkeeping in offline/MOCK_DB
        tests -- a class-level dict (per Python process) instead of a real
        Postgres table, mirroring SearchCache/DocumentChunk's approach."""

        _rows: dict = {}

        def create(self, data):
            import datetime

            row = dict(data)
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("startedAt", datetime.datetime.now(datetime.timezone.utc))
            row.setdefault("finishedAt", None)
            row.setdefault("durationMs", None)
            row.setdefault("nodeTimeline", None)
            row.setdefault("errorCode", None)
            row.setdefault("briefingId", None)
            MockDB._RunLog._rows[row["id"]] = row
            return type("MockRunLogRecord", (), row)()

        def update(self, where, data):
            row = MockDB._RunLog._rows.get(where["id"])
            if row is None:
                return None
            row.update(data)
            return type("MockRunLogRecord", (), row)()

        def find_unique(self, where):
            row = MockDB._RunLog._rows.get(where["id"])
            return type("MockRunLogRecord", (), row)() if row else None

        def find_many(self, where=None, order=None, take=None):
            where = where or {}
            rows = [r for r in MockDB._RunLog._rows.values() if all(r.get(k) == v for k, v in where.items())]
            rows.sort(key=lambda r: r.get("startedAt") or 0, reverse=True)
            if take:
                rows = rows[:take]
            return [type("MockRunLogRecord", (), r)() for r in rows]

    def __init__(self):
        self.briefing = MockDB._Briefing()
        self.citation = MockDB._Citation()
        self.briefingrevision = MockDB._BriefingRevision()
        self.auditlog = MockDB._AuditLog()
        self.apicalllog = MockDB._ApiCallLog()
        self.runlog = MockDB._RunLog()
        self.holding = MockDB._Holding()

    def connect(self):
        pass

    def disconnect(self):
        pass

    @staticmethod
    def _as_vector(value):
        return json.loads(value) if isinstance(value, str) else value

    @staticmethod
    def _l2(a, b):
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    def query_raw(self, sql, *args):
        if '"SearchCache"' in sql:
            if not MockDB._search_cache or not args:
                return []
            query_emb = self._as_vector(args[0])
            best = min(MockDB._search_cache, key=lambda r: self._l2(r["embedding"], query_emb))
            return [{"id": best["id"], "distance": self._l2(best["embedding"], query_emb)}]

        if '"DocumentChunk"' in sql:
            if not MockDB._document_chunks or not args:
                return []
            query_emb = self._as_vector(args[0])
            scored = sorted(MockDB._document_chunks, key=lambda r: self._l2(r["embedding"], query_emb))
            return [
                {
                    "id": r["id"],
                    "url": r["url"],
                    "title": r["title"],
                    "publishedDate": r["publishedDate"],
                    "content": r["content"],
                    "distance": self._l2(r["embedding"], query_emb),
                }
                for r in scored[:RETRIEVAL_TOP_K]
            ]

        if '"ApiCallLog"' in sql:
            return [{"cnt": 0}]

        if '"RunLog"' in sql and "UPDATE" in sql.upper():
            cutoff = args[0] if args else None
            recovered = []
            for row in MockDB._RunLog._rows.values():
                started_at = row.get("startedAt")
                if (
                    row.get("status") == "running"
                    and cutoff is not None
                    and started_at is not None
                    and started_at < cutoff
                ):
                    row["status"] = "failed"
                    row["errorCode"] = "timeout_recovered"
                    recovered.append({"id": row["id"]})
            return recovered

        return []

    def execute_raw(self, sql, *args):
        if 'INSERT INTO "SearchCache"' in sql:
            query, emb, results_json = args
            MockDB._search_cache.append(
                {
                    "id": str(len(MockDB._search_cache)),
                    "embedding": self._as_vector(emb),
                }
            )
        elif 'INSERT INTO "DocumentChunk"' in sql:
            url, title, published_date, chunk_index, content, emb = args
            MockDB._document_chunks.append(
                {
                    "id": f"mock-chunk-{len(MockDB._document_chunks)}",
                    "url": url,
                    "title": title,
                    "publishedDate": published_date,
                    "content": content,
                    "embedding": self._as_vector(emb),
                }
            )


# One Prisma connection is opened per graph run (per thread) and reused
# across every node, instead of the previous connect()/disconnect() churn
# inside researcher_node/retriever_node/save_node on every single call.
# Whoever starts a run (api.py, scheduler.py, run_seeded.py, __main__) is
# responsible for calling close_db() once after the stream loop finishes.
_thread_local = threading.local()


def get_db():
    if os.environ.get("MOCK_DB") == "1":
        if getattr(_thread_local, "mockdb", None) is None:
            _thread_local.mockdb = MockDB()
        return _thread_local.mockdb
    if getattr(_thread_local, "db", None) is None:
        _thread_local.db = settings_module.make_prisma()
        _thread_local.db.connect()
    return _thread_local.db


def close_db():
    db = getattr(_thread_local, "db", None)
    if db is not None:
        try:
            db.disconnect()
        except Exception as e:
            logging.getLogger("agent").debug("close_db: disconnect failed (ignored): %s", e)
        _thread_local.db = None
    _thread_local.mockdb = None


# llm_client logs every provider call (and the real-usage daily budget
# check) through this same thread-local, one-per-run connection -- see
# llm_client.configure()'s docstring.
# Don't override a getter the host process already registered: the API
# registers its own shared, connected client at startup (api.on_startup),
# and without that the API's daily-budget check failed closed on every
# request and silently served demo data. Standalone entry points
# (scheduler, scripts, __main__) get the thread-local one.
if llm_client._db_getter is None:
    llm_client.configure(get_db)


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP, max_chunks: int = MAX_CHUNKS_PER_ARTICLE
) -> List[str]:
    """Splits article text into overlapping chunks for embedding/retrieval."""
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = start + chunk_size
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


class GraphState(TypedDict):
    run_id: str
    request_id: Optional[str]
    client_id: str
    user_id: Optional[str]
    portfolio_themes: List[str]
    research_done: bool
    retrieval_attempted: bool
    gathered_news: str
    iteration_count: int
    revision_count: int
    final_briefing: str
    final_citations: List[dict]
    is_compliant: bool
    criticism: str
    needs_critique: bool
    write_failed: bool
    ingest_stats: dict
    theme_coverage: dict
    # Compliance engine (Phase 2) state:
    compliance_violations: List[dict]  # merged rules + judge violations for the CURRENT draft
    judge_unavailable: bool  # True if the judge failed/malformed after its retry this critique
    revision_history: List[
        dict
    ]  # one entry per writer-draft + critic-verdict cycle, flushed to BriefingRevision at save time
    briefing_status: str  # draft|compliant|needs_human_review|failed -- mirrors Briefing.status
    next: str


def new_input_state(
    client_id: str, portfolio_themes: List[str], run_id: Optional[str] = None, user_id: Optional[str] = None
) -> GraphState:
    """Builds a fresh GraphState for a new run. Shared by every entry point
    (api.py, scheduler.py, run_seeded.py, this file's __main__ block) so
    they can't drift out of sync on which fields exist."""
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "request_id": None,
        "client_id": client_id,
        "user_id": user_id,
        "portfolio_themes": portfolio_themes,
        "research_done": False,
        "retrieval_attempted": False,
        "gathered_news": "",
        "iteration_count": 0,
        "revision_count": 0,
        "final_briefing": "",
        "final_citations": [],
        "is_compliant": False,
        "criticism": "",
        "needs_critique": False,
        "write_failed": False,
        "ingest_stats": {},
        "theme_coverage": {},
        "compliance_violations": [],
        "judge_unavailable": False,
        "revision_history": [],
        "briefing_status": "draft",
    }


def researcher_node(state: GraphState) -> dict:
    """Ingestion step: searches Tavily for fresh queries (skipping ones
    already ingested recently, per SearchCache), chunks each result's
    article text, embeds every chunk, and stores it in DocumentChunk. This
    node builds the knowledge base; it does NOT decide what the writer sees
    -- that's retriever_node's job."""
    log = get_node_logger(state, "researcher")
    log.info("Node: researcher_node")
    iteration_count = state.get("iteration_count", 0)
    themes = [t.strip() for t in state.get("portfolio_themes", []) if t and t.strip()]

    if not themes:
        log.warning("No portfolio themes configured for this client; skipping ingestion.")
        return {
            "research_done": True,
            "iteration_count": iteration_count + 1,
            "ingest_stats": {
                "queries": 0,
                "tavily_ok": 0,
                "tavily_failed": 0,
                "chunks_added": 0,
                "cache_skips": 0,
                "error": "no portfolio themes configured",
            },
        }

    db = get_db()
    try:
        holdings = db.holding.find_many(where={"clientProfileId": state.get("client_id")})
    except Exception as e:  # noqa: BLE001 - never let a holdings lookup fail ingestion
        log.warning(f"Holdings lookup failed ({e}); falling back to theme-only queries.")
        holdings = []

    if holdings:
        # Ticker/sector-aware queries when the client has an uploaded
        # holdings CSV (Phase 7), instead of only the broader portfolio
        # themes -- falls back to themes-only (below) when there are none.
        holdings_desc = ", ".join(
            f"{h.ticker}" + (f" ({h.sector})" if getattr(h, "sector", None) else "") for h in holdings[:20]
        )
        prompt = (
            f"Based on this portfolio's actual holdings: {holdings_desc}, and its broader themes: "
            f"{', '.join(themes)}, generate 2 to 3 highly targeted search queries covering the specific "
            "tickers/sectors held, not just the themes. Return ONLY a valid JSON array of strings, "
            'e.g. ["AAPL earnings outlook", "semiconductor sector news"].'
        )
    else:
        prompt = f'Based on portfolio themes: {", ".join(themes)}, generate 2 to 3 highly targeted search queries. Return ONLY a valid JSON array of strings, e.g. ["theme 1 news", "theme 2 news"].'

    def _validate_queries(parsed):
        if not isinstance(parsed, list) or not all(isinstance(q, str) and q.strip() for q in parsed):
            return None
        return parsed

    run_id = state.get("run_id")
    result = llm_client.generate_json(prompt, purpose="query_gen", run_id=run_id, validate=_validate_queries)
    if result.error:
        log.warning(f"Query generation failed or malformed ({result.error}); falling back to templated queries.")
        queries = [f"{theme} financial news" for theme in themes][:3]
    else:
        queries = result.parsed

    stats = {"queries": len(queries), "tavily_ok": 0, "tavily_failed": 0, "chunks_added": 0, "cache_skips": 0}

    for q in queries:
        emb = llm_client.embed_text(q, purpose="query_embedding", run_id=run_id)
        already_ingested = False
        try:
            # nosec B608: only module constants (a fixed interval string) are interpolated
            cached_rows = db_utils.query_with_timeout(
                db,
                f"""
                SELECT id, "queryEmbedding" <-> $1::vector AS distance
                FROM "SearchCache"
                WHERE "createdAt" >= NOW() - INTERVAL '{CACHE_MAX_AGE_SQL_INTERVAL}'
                ORDER BY distance
                LIMIT 1
                """,
                emb,
                timeout_ms=VECTOR_QUERY_TIMEOUT_MS,
            )
            if cached_rows and len(cached_rows) > 0:
                distance = cached_rows[0].get("distance")
                if distance is not None and distance <= CACHE_SIMILARITY_THRESHOLD:
                    log.info(f"Skipping ingestion for query: {q} (already ingested, distance={distance:.4f})")
                    already_ingested = True
        except Exception as e:
            log.warning(f"Cache check error: {e}")

        if already_ingested:
            stats["cache_skips"] += 1
            continue

        try:
            res = llm_client.tavily_search(
                q,
                purpose="research_search",
                run_id=run_id,
                search_depth="basic",
                topic="news",
                days=SOURCE_MAX_AGE_DAYS,
                max_results=TAVILY_MAX_RESULTS,
            )
            stats["tavily_ok"] += 1

            # Collect every chunk of every fresh result first, then embed them in
            # batches (llm_client.embed_batch: EMBEDDING_BATCH_SIZE per call,
            # bounded concurrency) -- one call per chunk was ~60 sequential
            # round trips per run and looked like a hang.
            pending = []  # (url, title, published_date, chunk_index, chunk_text)
            for item in res.get("results", []):
                url = item.get("url")
                content = item.get("content", "")
                if not url or not content:
                    continue
                if not is_recent_enough(item.get("published_date")):
                    stats["stale_skipped"] = stats.get("stale_skipped", 0) + 1
                    continue
                for idx, chunk in enumerate(chunk_text(content)):
                    pending.append((url, item.get("title"), item.get("published_date"), idx, chunk))

            if pending:
                try:
                    vectors = llm_client.embed_batch([p[4] for p in pending], purpose="chunk_embedding", run_id=run_id)
                except Exception as batch_err:
                    log.warning(f"Failed to embed chunks for query '{q}': {batch_err}")
                    vectors = []
                for (url, title, published, idx, chunk), chunk_emb in zip(pending, vectors):
                    try:
                        db.execute_raw(
                            """
                            INSERT INTO "DocumentChunk"
                                ("id", "url", "title", "publishedDate", "chunkIndex", "content", "embedding", "createdAt")
                            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6::vector, NOW())
                            """,
                            url,
                            title,
                            published,
                            idx,
                            chunk,
                            json.dumps(chunk_emb),
                        )
                        stats["chunks_added"] += 1
                    except Exception as chunk_err:
                        log.warning(f"Failed to store chunk for {url}: {chunk_err}")

            # Mark this query as ingested so a near-duplicate query within
            # the cache window skips re-fetching and re-embedding.
            db.execute_raw(
                """
                INSERT INTO "SearchCache" ("id", "query", "queryEmbedding", "results", "createdAt")
                VALUES (gen_random_uuid(), $1, $2::vector, $3::json, NOW())
                """,
                q,
                json.dumps(emb),
                json.dumps({"queried": True}),
            )
        except Exception as e:
            stats["tavily_failed"] += 1
            log.warning(f"Tavily search error for {q}: {str(e)}")

    log.info(f"Ingestion stats: {stats}")

    return {
        "research_done": True,
        "iteration_count": iteration_count + 1,
        "ingest_stats": stats,
    }


def retriever_node(state: GraphState) -> dict:
    """The actual RAG retrieval step: for each portfolio theme, embeds the
    theme and does a top-k pgvector similarity search over DocumentChunk,
    drops chunks beyond RETRIEVAL_MAX_DISTANCE, and selects whole chunks
    (not a raw character slice) up to CONTEXT_CHAR_BUDGET."""
    log = get_node_logger(state, "retriever")
    log.info("Node: retriever_node")
    themes = state.get("portfolio_themes", [])
    current_citations = state.get("final_citations", [])

    db = get_db()

    seen_urls = {c["url"] for c in current_citations}
    candidates = []  # list of (theme, row) in priority order
    theme_coverage = {}

    for theme in themes:
        rows = []
        try:
            theme_emb = llm_client.embed_text(theme, purpose="theme_embedding", run_id=state.get("run_id"))
            # nosec B608: interpolates only module constants (fixed interval, int LIMIT); theme text is a bound param
            rows = db_utils.query_with_timeout(
                db,
                f"""
                SELECT id, url, title, "publishedDate", content, embedding <-> $1::vector AS distance
                FROM "DocumentChunk"
                WHERE "createdAt" >= NOW() - INTERVAL '{RETRIEVAL_MAX_AGE_SQL_INTERVAL}'
                ORDER BY distance
                LIMIT {RETRIEVAL_CANDIDATES_PER_THEME}
                """,
                theme_emb,
                timeout_ms=VECTOR_QUERY_TIMEOUT_MS,
            )
        except Exception as e:
            log.warning(f"Retrieval error for theme '{theme}': {e}")

        theme_has_source = False
        kept = 0
        for row in rows or []:
            distance = row.get("distance")
            if distance is not None and distance > RETRIEVAL_MAX_DISTANCE:
                continue
            # Recency by PUBLISH date (createdAt only says when we ingested it).
            # Rows with no publishedDate are kept only if the caller's data has
            # none at all (offline fixtures); live Tavily news always has one,
            # and ingestion already drops stale/undated items.
            if row.get("publishedDate") is not None and not is_recent_enough(row.get("publishedDate")):
                continue
            if kept >= RETRIEVAL_TOP_K * 2:
                break
            kept += 1
            theme_has_source = True
            candidates.append((theme, row))
        theme_coverage[theme] = theme_has_source

    # Interleave themes (round-robin) so no single theme's sources crowd out the
    # others when the citation/context budget runs out.
    by_theme: dict = {}
    for theme, row in candidates:
        by_theme.setdefault(theme, []).append((theme, row))
    interleaved = []
    while any(by_theme.values()):
        for theme in list(by_theme):
            if by_theme[theme]:
                interleaved.append(by_theme[theme].pop(0))
    candidates = interleaved

    # Prompt-injection screen: BEFORE any candidate chunk reaches the
    # writer, score it with the (actually-suited-for-this) prompt-guard
    # classifier and drop anything flagged. Quarantined chunks are logged
    # by chunk id only, never their text.
    candidate_rows = [row for _, row in candidates]
    _clean_rows, quarantined = injection_guard.screen_chunks(candidate_rows, run_id=state.get("run_id"))
    if quarantined:
        quarantined_ids = {q["chunk_id"] for q in quarantined}
        log.warning(f"Quarantined {len(quarantined)} chunk(s) as likely prompt injection: {sorted(quarantined_ids)}")
        for q in quarantined:
            audit.log_event(
                db,
                action="chunk_quarantined",
                user_id=state.get("user_id"),
                client_id=state.get("client_id"),
                metadata={"chunk_id": q["chunk_id"], "score": q["score"], "run_id": state.get("run_id")},
            )
        candidates = [(theme, row) for theme, row in candidates if row.get("id") not in quarantined_ids]

    # Apply a whole-chunk budget instead of a raw string slice, so a
    # citation never claims a source whose text was actually truncated
    # mid-sentence. Candidates are already roughly similarity-ordered
    # within each theme; process in the order gathered.
    context_parts = []
    new_citations = []
    total_chars = 0

    chunks_per_url: dict = {}
    for theme, row in candidates:
        source_label = row.get("title") or row.get("url")
        url = row.get("url")
        if url and chunks_per_url.get(url, 0) >= MAX_CHUNKS_PER_URL:
            continue
        if url and url not in seen_urls and len(new_citations) + len(current_citations) >= MAX_CITATIONS:
            continue
        piece = f"[Source: {source_label}]\n{row.get('content', '')}"
        if context_parts and total_chars + len(piece) > CONTEXT_CHAR_BUDGET:
            continue
        context_parts.append(piece)
        total_chars += len(piece) + 2
        if url:
            chunks_per_url[url] = chunks_per_url.get(url, 0) + 1
        if url and url not in seen_urls:
            seen_urls.add(url)
            new_citations.append(
                {
                    "url": url,
                    "title": row.get("title"),
                    "publishedDate": row.get("publishedDate"),
                }
            )

    for theme, has_source in theme_coverage.items():
        if not has_source:
            context_parts.append(f"[No recent sources found for theme: {theme}]")

    gathered_news = "\n\n".join(context_parts)
    log.info(
        f"Retrieved {len(new_citations)} new citation(s) across {len(themes)} theme(s); "
        f"coverage={theme_coverage}; context_chars={total_chars}"
    )

    return {
        "gathered_news": gathered_news,
        "final_citations": current_citations + new_citations,
        "retrieval_attempted": True,
        "theme_coverage": theme_coverage,
    }


def writer_node(state: GraphState) -> dict:
    log = get_node_logger(state, "writer")
    log.info("Node: writer_node")
    news = state.get("gathered_news", "")
    criticism = state.get("criticism", "")
    themes = state.get("portfolio_themes", [])
    revision_count = state.get("revision_count", 0)

    prompt = f"""
    You are a financial analyst drafting an SEC-compliant morning briefing.
    Themes: {", ".join(themes)}
    Retrieved Source Excerpts (each tagged with its originating source): {news}

    Previous Compliance Critic Feedback to address (if any): {criticism}

    Format into Markdown with a 'Macro View' and 'Portfolio Impact' section.
    CRITICAL RESTRICTION 1: Strict source grounding. Use ONLY facts from the Retrieved Source Excerpts above.
    CRITICAL: Never make definitive buy, sell, or hold recommendations. Phrase all impacts strictly as potential risks or potential tailwinds based on historical correlations.
    """

    result = llm_client.generate_text(prompt, purpose="writer", run_id=state.get("run_id"))
    if result.error:
        # Never save the raw exception text as briefing content -- that's
        # what previously shipped a literal "Error generating draft: 404
        # NOT_FOUND..." string to end users (see MEMORY.md, 2026-09-13).
        # A clearly-labeled placeholder goes to save_node instead, and
        # needs_critique is False so a failed draft never reaches the critic.
        log.error(f"Writer call failed on every configured provider (error={result.error})")
        return {
            "final_briefing": "Briefing generation failed today due to a temporary error with our research "
            "provider. No draft is available for this run; the next scheduled run will retry.",
            "revision_count": revision_count + 1,
            "needs_critique": False,
            "write_failed": True,
            "briefing_status": "failed",
        }

    return {
        "final_briefing": result.text,
        "revision_count": revision_count + 1,
        "needs_critique": True,
        "write_failed": False,
    }


def build_criticism(rule_violations: list, judge_violations: list) -> str:
    """Thin re-export of compliance.verdict.build_criticism, kept here so
    nothing importing agent.build_criticism (pre-Phase-7 call sites/tests)
    breaks. See compliance/verdict.py for the real implementation."""
    return compliance_verdict.build_criticism(rule_violations, judge_violations)


def compliance_critic_node(state: GraphState) -> dict:
    """Deterministic rules (compliance/rules.py) ALWAYS run first, on every
    draft, with no API call. The LLM judge (compliance/judge.py) runs
    separately; if the judge says "compliant" but the rules found a
    violation, the rules VETO it -- an "approved" LLM verdict can never
    override a rule hit. If the judge itself is unavailable or returns a
    malformed response after its one retry, this fails safe: the run is
    routed to needs_human_review by the supervisor, never silently marked
    compliant.

    Delegates the actual rules+judge evaluation to compliance.verdict
    (extracted in Phase 7 so the edit-and-recheck endpoint,
    PATCH /api/briefings/{id}, runs the exact same combined system over
    edited text instead of re-deriving this veto logic separately)."""
    log = get_node_logger(state, "critic")
    log.info("Node: compliance_critic_node")
    draft = state.get("final_briefing", "")
    revision_count = state.get("revision_count", 0)

    result = compliance_verdict.evaluate(draft, run_id=state.get("run_id"))

    revision_entry = {
        "revision_index": revision_count,
        "content": draft,
        "violations": result.violations,
        "critic_model": result.critic_model,
        "is_compliant": result.compliant,
    }

    if result.judge_unavailable:
        log.warning("Judge unavailable/malformed this critique; failing safe -- never 'compliant' on a judge failure.")

    return {
        "is_compliant": result.compliant,
        "criticism": result.criticism,
        "compliance_violations": result.violations,
        "judge_unavailable": result.judge_unavailable,
        "needs_critique": False,
        "revision_history": state.get("revision_history", []) + [revision_entry],
    }


def supervisor_node(state: GraphState) -> dict:
    """Hub node. Evaluates current state and decides which worker runs next:
    researcher, retriever, writer, compliance_critic, or FINISH (-> save -> END)."""
    log = get_node_logger(state, "supervisor")
    log.info("Node: supervisor_node")

    research_done = state.get("research_done", False)
    retrieval_attempted = state.get("retrieval_attempted", False)
    gathered_news = state.get("gathered_news", "")
    iteration_count = state.get("iteration_count", 0)
    final_briefing = state.get("final_briefing", "")
    is_compliant = state.get("is_compliant", False)
    criticism = state.get("criticism", "")
    revision_count = state.get("revision_count", 0)
    needs_critique = state.get("needs_critique", False)
    judge_unavailable = state.get("judge_unavailable", False)
    write_failed = state.get("write_failed", False)
    themes = state.get("portfolio_themes", [])

    # Stage 1: no ingestion done yet.
    if not research_done:
        reason = "no research yet"
        log.info(f"Supervisor routing -> researcher ({reason})")
        return {"next": "researcher", "reason": reason}

    # Stage 2: ingestion done, retrieval not attempted yet. Guarded by
    # retrieval_attempted (not just "gathered_news is empty") so a genuinely
    # empty retrieval result -- nothing ingested for these themes in the
    # freshness window -- doesn't loop back to the retriever forever.
    if not retrieval_attempted:
        reason = "research done, not yet retrieved"
        log.info(f"Supervisor routing -> retriever ({reason})")
        return {"next": "retriever", "reason": reason}

    # Stage 3: retrieval has run (context may still be empty), no draft yet.
    # Evaluate whether the retrieved context has enough depth for these
    # themes (evaluate_and_route from the v1 spec, folded into the
    # supervisor). Hard-capped at 2 research iterations regardless of the
    # LLM's judgement, so this always terminates.
    if not final_briefing:
        if iteration_count < 2:
            try:
                if not gathered_news:
                    raise ValueError("empty retrieval, skip LLM call and re-research directly")
                eval_prompt = f"""You are evaluating whether retrieved financial context has enough depth to draft a compliant morning briefing.
Portfolio themes: {", ".join(themes)}
Retrieved context (may be truncated): {gathered_news[:3000]}

Does this context have sufficient financial depth and relevance to these themes? Respond with ONLY a JSON object: {{"sufficient": boolean}}"""

                def _validate_sufficiency(parsed):
                    return parsed if isinstance(parsed, dict) and "sufficient" in parsed else None

                gen_result = llm_client.generate_json(
                    eval_prompt, purpose="depth_check", run_id=state.get("run_id"), validate=_validate_sufficiency
                )
                if gen_result.error:
                    raise ValueError(f"depth-check call failed: {gen_result.error}")
                if not gen_result.parsed.get("sufficient", True):
                    raise ValueError("insufficient depth")
            except Exception as e:
                reason = f"re-research triggered: {e}"
                log.info(f"Supervisor routing -> researcher ({reason})")
                return {
                    "next": "researcher",
                    "research_done": False,
                    "retrieval_attempted": False,
                    "gathered_news": "",
                    "reason": reason,
                }
        reason = "retrieved context accepted, drafting"
        log.info(f"Supervisor routing -> writer ({reason})")
        return {"next": "writer", "reason": reason}

    # Stage 4: writer itself failed -- never sent to the critic at all.
    if write_failed:
        reason = "writer failed, never critiqued"
        log.info(f"Supervisor routing -> FINISH (save) with status=failed ({reason})")
        return {"next": "save", "reason": reason}

    # Stage 5: have a draft that hasn't been through the critic yet.
    if needs_critique:
        reason = "draft ready for compliance critique"
        log.info(f"Supervisor routing -> compliance_critic ({reason})")
        return {"next": "compliance_critic", "reason": reason}

    # Stage 6: draft has been critiqued. The judge being unavailable/malformed
    # fails safe immediately -- needs_human_review, never another revision
    # cycle against a non-functioning judge, and never silently "compliant".
    if judge_unavailable:
        reason = "judge_unavailable after retry, fail safe to needs_human_review"
        log.info(f"Supervisor routing -> FINISH (save) with status=needs_human_review ({reason})")
        return {"next": "save", "briefing_status": "needs_human_review", "reason": reason}

    if is_compliant:
        reason = "rules + judge both passed"
        log.info(f"Supervisor routing -> FINISH (save) with status=compliant ({reason})")
        return {"next": "save", "briefing_status": "compliant", "reason": reason}

    if criticism:
        if revision_count >= MAX_REVISIONS:
            reason = f"max revisions ({MAX_REVISIONS}) hit with unresolved violations, never 'compliant' by timeout"
            log.info(f"Supervisor routing -> FINISH (save) with status=needs_human_review ({reason})")
            return {"next": "save", "briefing_status": "needs_human_review", "reason": reason}
        reason = "revision requested by critic"
        log.info(f"Supervisor routing -> writer ({reason})")
        return {"next": "writer", "reason": reason}

    reason = "no further action determined"
    log.info(f"Supervisor routing -> FINISH (save) with status=compliant ({reason})")
    return {"next": "save", "briefing_status": "compliant", "reason": reason}


def save_node(state: GraphState) -> dict:
    log = get_node_logger(state, "save")
    log.info("Node: save_node")
    client_id = state.get("client_id")
    briefing = state.get("final_briefing")
    citations = state.get("final_citations", [])
    status = state.get("briefing_status", "draft")
    try:
        status_enum = PrismaBriefingStatus(status)
    except ValueError:
        log.warning(f"Unknown briefing_status {status!r}; defaulting to draft")
        status_enum = PrismaBriefingStatus.draft
    compliance_violations = state.get("compliance_violations", [])
    revision_count = state.get("revision_count", 0)
    run_id = state.get("run_id")
    revision_history = state.get("revision_history", [])

    if client_id and briefing:
        db = get_db()
        try:
            br_rec = db.briefing.create(
                data={
                    "content": briefing,
                    "clientProfileId": client_id,
                    "status": status_enum,
                    "complianceViolations": Json(compliance_violations),
                    "revisionCount": revision_count,
                    "runId": run_id,
                }
            )
            for cit in citations:
                db.citation.create(
                    data={
                        "url": cit.get("url"),
                        "title": cit.get("title"),
                        "publishedDate": cit.get("publishedDate"),
                        "briefingId": br_rec.id,
                    }
                )
            # BriefingRevision rows can only be written now, once a real
            # briefingId exists -- the Briefing itself is only ever created
            # once, at the very end of the run (see schema.prisma's
            # BriefingRevision docstring). revision_history accumulated one
            # entry per writer-draft + critic-verdict cycle during the run.
            for entry in revision_history:
                db.briefingrevision.create(
                    data={
                        "briefingId": br_rec.id,
                        "revisionIndex": entry["revision_index"],
                        "content": entry["content"],
                        "violations": Json(entry["violations"]),
                        "criticModel": entry["critic_model"],
                        "isCompliant": entry["is_compliant"],
                    }
                )
            audit.log_event(
                db,
                action="briefing_finalized",
                user_id=state.get("user_id"),
                client_id=client_id,
                briefing_id=br_rec.id,
                metadata={
                    "status": status,
                    "revision_count": revision_count,
                    "run_id": run_id,
                    "violation_count": len(compliance_violations),
                },
            )
            log.info(f"Successfully saved briefing (status={status}) and citations to database.")
        except Exception as e:
            log.error(f"Error saving to database: {e}")

    return {}


# Build StateGraph: Supervisor hub dispatches to researcher / retriever /
# writer / compliance_critic; each worker returns to the supervisor;
# supervisor decides FINISH by routing to "save" -> END.
workflow = StateGraph(GraphState)

workflow.add_node("supervisor", supervisor_node)
workflow.add_node("researcher", researcher_node)
workflow.add_node("retriever", retriever_node)
workflow.add_node("writer", writer_node)
workflow.add_node("critic", compliance_critic_node)
workflow.add_node("save", save_node)

workflow.add_edge(START, "supervisor")
workflow.add_edge("researcher", "supervisor")
workflow.add_edge("retriever", "supervisor")
workflow.add_edge("writer", "supervisor")
workflow.add_edge("critic", "supervisor")

workflow.add_conditional_edges(
    "supervisor",
    lambda state: state.get("next", "save"),
    {
        "researcher": "researcher",
        "retriever": "retriever",
        "writer": "writer",
        "compliance_critic": "critic",
        "save": "save",
    },
)

workflow.add_edge("save", END)
app = workflow.compile()

if __name__ == "__main__":
    print("--- Starting Local Execution Block (Multi-Agent, mocked DB) ---")
    os.environ["MOCK_DB"] = "1"
    mock_input = new_input_state("client_1", ["AI infrastructure", "Semiconductors"])

    final_state = dict(mock_input)
    try:
        for output in app.stream(mock_input):
            for key, value in output.items():
                print(f"Finished node: {key}")
                if value:
                    final_state.update(value)
    finally:
        close_db()

    if final_state and "final_briefing" in final_state:
        print("\n\n--- Final Morning Briefing ---")
        print(final_state["final_briefing"])
        print("\n--- Citations ---")
        for url in final_state.get("final_citations", []):
            print(f"- {url}")
        print("\n--- Ingest stats ---")
        print(final_state.get("ingest_stats"))
        print("\n--- Compliance ---")
        print("status:", final_state.get("briefing_status"))
        print("revision_count:", final_state.get("revision_count"))
        print("violations:", final_state.get("compliance_violations"))
        print("judge_unavailable:", final_state.get("judge_unavailable"))
        print("revision_history entries:", len(final_state.get("revision_history", [])))

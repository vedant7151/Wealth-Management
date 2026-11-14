import datetime
import logging
import os
import threading
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel, field_validator  # noqa: E402
from prisma import Json  # noqa: E402
from prisma.enums import BriefingStatus  # noqa: E402

import audit  # noqa: E402
import delivery  # noqa: E402
import demo_mode  # noqa: E402
import holdings as holdings_module  # noqa: E402
import http_layer  # noqa: E402
import llm_client  # noqa: E402
import metrics  # noqa: E402
import ratelimit  # noqa: E402
import runs  # noqa: E402
import settings as settings_module  # noqa: E402
import validation  # noqa: E402
from auth import DEMO_USER_ID, get_current_user, require_admin  # noqa: E402
from compliance import verdict as compliance_verdict  # noqa: E402
from logging_config import configure_logging, request_id_var  # noqa: E402

# Fail fast: a bad/missing env var (or a prod profile with CORS "*", demo
# auth, or DEBUG on) aborts startup here with a list of variable NAMES.
settings = settings_module.load_or_exit()
settings_module.apply_runtime_env(settings)
os.environ.setdefault("LOG_LEVEL", settings.log_level)
configure_logging()
logger = logging.getLogger("api")

app = FastAPI(
    title="Wealth Agent API",
    debug=settings.debug,
    # No interactive docs / schema in prod: less surface to probe.
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_prod else "/openapi.json",
)

# Middleware order (Starlette: the LAST one added is the OUTERMOST):
#   CORS (outermost) -> hardening (request id, headers, body cap, 500s) -> app.
# CORS must wrap the hardening layer: responses that layer generates itself
# (413 body too large, 500 internal_error) would otherwise carry no CORS
# headers and the browser would report an opaque "Failed to fetch" instead of
# the real status. Cost: CORS preflight replies skip the security headers.
http_layer.install(
    app,
    max_body_bytes=settings.max_request_body_bytes,
    is_prod=settings.is_prod,
    hsts_max_age=settings.hsts_max_age_seconds,
    csp=settings.csp_api,
)
# CORS origins are env-configured (comma-separated); prod refuses "*" (see
# settings.py). The rate-limit/request-id headers are exposed so the
# browser frontend can read them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Last-Event-ID"],
    expose_headers=["X-Request-ID", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)


def _make_db():
    return settings_module.make_prisma()


db = _make_db()


def _init_sentry() -> None:
    """Optional: only when SENTRY_DSN is set AND sentry-sdk is installed.
    PII, request bodies and headers are NOT sent; the same redaction used
    for logs scrubs event messages."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; Sentry disabled")
        return
    from logging_config import redact

    def _before_send(event, hint):
        if "message" in event:
            event["message"] = redact(str(event["message"]))
        event.pop("request", None)
        return event

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        before_send=_before_send,
    )


@app.on_event("startup")
def on_startup():
    _init_sentry()
    db.connect()
    ratelimit.configure(db_getter=lambda: db)
    # Real daily-budget check + ApiCallLog writes need a DB; without this the
    # budget check failed closed and EVERY /run was served as demo data.
    llm_client.configure(lambda: db)
    # A run stuck in "running" means the process that was executing it died
    # (crash/redeploy/OOM) before its background thread's `finally` ever
    # ran -- without this, that client's run lock (RUN_LOCK_TTL_SECONDS)
    # would eventually expire on its own, but the RunLog row itself would
    # sit as "running" forever. See runs.recover_stuck_runs's docstring.
    runs.recover_stuck_runs(db)
    logger.info("API started (env=%s, auth=%s)", settings.app_env, "clerk" if settings.auth_configured else "demo")


GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = settings.graceful_shutdown_timeout_seconds


@app.on_event("shutdown")
def on_shutdown():
    """Runs on uvicorn's ASGI lifespan shutdown (triggered by SIGTERM,
    after it stops accepting new connections) -- drains any in-flight
    background graph runs up to GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS before
    disconnecting the DB, instead of letting a run get killed mid-write.
    A run that's still going after the timeout is left to
    recover_stuck_runs() on the next startup, same as an outright crash.
    Pair this with `uvicorn --timeout-graceful-shutdown` set to at least
    this value (see backend/Dockerfile's CMD) so uvicorn itself doesn't
    force-kill the process before this handler finishes waiting."""
    still_running = runs.wait_for_active_runs(GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS)
    if still_running:
        logger.warning(
            "Shutdown: %d run(s) still in flight after %ss grace period -- recover_stuck_runs will clean up on next startup",
            still_running,
            GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
        )
    db.disconnect()


# --------------------------------------------------------------------------
# Rate limiting plumbing
# --------------------------------------------------------------------------


class RateLimitExceeded(Exception):
    def __init__(self, result: ratelimit.RateLimitResult, error: str = "rate_limited"):
        self.result = result
        self.error = error


@app.exception_handler(RateLimitExceeded)
def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded):
    r = exc.result
    message = "Rate limit exceeded; retry after the indicated delay"
    return JSONResponse(
        status_code=429,
        content={
            "error": exc.error,
            "message": message,
            "detail": message,
            "request_id": getattr(request.state, "request_id", None) or request_id_var.get(),
            "limit": r.limit,
            "remaining": r.remaining,
            "reset_at": r.reset_at,
            "retry_after": r.retry_after,
        },
        headers={
            "Retry-After": str(r.retry_after),
            "X-RateLimit-Limit": str(r.limit),
            "X-RateLimit-Remaining": str(r.remaining),
            "X-RateLimit-Reset": str(int(r.reset_at)),
        },
    )


def _client_ip(request: Request) -> str:
    """Only trusts X-Forwarded-For when TRUSTED_PROXIES lists the direct
    peer, so a client can't just spoof the header to dodge IP-based limits
    on an untrusted deployment."""
    direct = request.client.host if request.client else "unknown"
    trusted = {t.strip() for t in os.environ.get("TRUSTED_PROXIES", "").split(",") if t.strip()}
    if trusted and direct in trusted:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return direct


def resolve_identity(request: Request, user_id: str) -> str:
    """Rate-limit identity: the real Clerk user id when auth is active,
    otherwise the caller's IP (so one noisy demo-mode visitor doesn't use
    up the whole shared DEMO_USER_ID bucket for everyone else)."""
    if user_id != DEMO_USER_ID:
        return f"user:{user_id}"
    return f"ip:{_client_ip(request)}"


def _budget_result(budget: dict) -> ratelimit.RateLimitResult:
    """Adapts llm_client.check_daily_budget()'s {allowed, used, limit} dict
    (real ApiCallLog usage, not an estimate -- Phase 3) into the same
    RateLimitResult shape Phase 1's ratelimit.py uses, so the existing
    RateLimitExceeded/header-building code works unchanged for either."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    reset_at = tomorrow.timestamp()
    return ratelimit.RateLimitResult(
        allowed=budget["allowed"],
        limit=budget["limit"],
        remaining=max(0, budget["limit"] - budget["used"]),
        reset_at=reset_at,
        retry_after=max(0, int(reset_at - now.timestamp())),
    )


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------


def get_owned_client_or_404(client_id: str, user_id: str):
    """Single shared ownership check for every client-scoped route. Returns
    404 (never 403) for another tenant's client, so a cross-tenant probe
    can't even confirm the id exists.

    No more `OR clerkUserId IS NULL` escape hatch: a real (non-demo) Clerk
    user only ever sees rows they own. Demo mode (auth unconfigured, every
    caller resolves to DEMO_USER_ID) only sees the explicitly-unowned demo
    rows -- it does NOT see rows already claimed by a real tenant, even
    though demo mode can't tell callers apart from each other."""
    client = db.clientprofile.find_unique(where={"id": client_id})
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    owned = (user_id == DEMO_USER_ID and client.clerkUserId is None) or (
        user_id != DEMO_USER_ID and client.clerkUserId == user_id
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _serialize_client(p) -> dict:
    return {"id": p.id, "name": p.name, "portfolio_themes": p.portfolio_themes}


def _serialize_briefing(b) -> dict:
    revisions = sorted(b.revisions or [], key=lambda r: r.revisionIndex)
    return {
        "id": b.id,
        "content": b.content,
        "createdAt": b.createdAt.isoformat(),
        "status": str(b.status),
        "complianceViolations": b.complianceViolations or [],
        "revisionCount": b.revisionCount,
        "citations": [{"url": c.url, "title": c.title, "publishedDate": c.publishedDate} for c in (b.citations or [])],
        "revisions": [
            {
                "revisionIndex": r.revisionIndex,
                "content": r.content,
                "violations": r.violations or [],
                "criticModel": r.criticModel,
                "isCompliant": r.isCompliant,
                "createdAt": r.createdAt.isoformat(),
            }
            for r in revisions
        ],
        "reviewedAt": b.reviewedAt.isoformat() if getattr(b, "reviewedAt", None) else None,
        "reviewedBy": getattr(b, "reviewedBy", None),
        "overrideReason": getattr(b, "overrideReason", None),
        "clientProfileId": b.clientProfileId,
        "isDemo": getattr(b, "isDemo", False),
    }


def _serialize_holding(h) -> dict:
    return {"id": h.id, "ticker": h.ticker, "sector": h.sector, "weight": h.weight}


def _get_owned_briefing_or_404(briefing_id: str, user_id: str):
    """Looks up a Briefing by id and enforces the SAME ownership rule as
    every client-scoped route, via the client it belongs to -- 404 (never
    403) if the caller doesn't own that client."""
    briefing = db.briefing.find_unique(where={"id": briefing_id}, include={"citations": True, "revisions": True})
    if not briefing:
        raise HTTPException(status_code=404, detail="Briefing not found")
    get_owned_client_or_404(briefing.clientProfileId, user_id)
    return briefing


def _serialize_run_log(r) -> dict:
    return {
        "id": r.id,
        "clientId": r.clientProfileId,
        "trigger": str(r.trigger),
        "status": str(r.status),
        "startedAt": r.startedAt.isoformat() if hasattr(r.startedAt, "isoformat") else r.startedAt,
        "finishedAt": r.finishedAt.isoformat() if r.finishedAt and hasattr(r.finishedAt, "isoformat") else r.finishedAt,
        "durationMs": r.durationMs,
        "errorCode": r.errorCode,
        "briefingId": r.briefingId,
    }


@app.get("/healthz")
def healthz():
    """Dependency-free liveness probe -- no DB/network/limiter-store call,
    just "is the process up and able to answer a request." `demoMode`
    reports the CONFIGURED `DEMO_MODE` setting (an env-var read, not a
    live decision) so a reviewer/operator can see at a glance whether
    this deployment is running with real providers or demo fixtures."""
    return {"status": "ok", "demoMode": demo_mode.configured_mode()}


@app.get("/readyz")
def readyz():
    """Readiness: DB connectivity, the rate-limit/budget store, and every
    provider circuit breaker's current state (Phase 3). A provider
    breaker being open doesn't fail readiness by itself (the fallback
    chain/graceful-degradation paths handle that) or the limiter store
    being down (every policy has an explicit fail-open/fail-closed
    behavior already) -- both are surfaced here so an operator can see
    them without grepping logs, but only DB connectivity actually flips
    the status code, since nothing in this app can serve correctly
    without it."""
    try:
        db.query_raw("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    body = {
        "db": "ok" if db_ok else "error",
        "rate_limit_store": ratelimit.store_health(),
        "circuit_breakers": llm_client.breaker_states(),
        "active_runs": runs.active_run_count(),
        "max_concurrent_runs": settings.max_concurrent_runs,
        "env": settings.app_env,
        "auth": "clerk" if settings.auth_configured else "demo",
    }
    return JSONResponse(status_code=200 if db_ok else 503, content=body)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(request: Request):
    """Prometheus text exposition. 404 when METRICS_ENABLED is off; when
    METRICS_TOKEN is set it must be presented as `Authorization: Bearer
    <token>` (required in prod -- see settings.py). Constant-time compare."""
    import hmac

    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    if settings.metrics_token:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {settings.metrics_token}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing metrics token")
    for name, st in llm_client.breaker_states().items():
        metrics.CIRCUIT_OPEN.labels(name).set(1 if (st or {}).get("open") else 0)
    metrics.set_in_flight(runs.active_run_count())
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/api/admin/usage")
def admin_usage(request: Request, user_id: str = Depends(get_current_user)):
    """Owner/admin-only (ADMIN_USER_IDS env, comma-separated Clerk user
    ids). Calls/day by provider, failure counts, cache-hit rate, avg
    latency -- from the real ApiCallLog ledger. No UI for this yet
    (Phase 7 scope); this is the data endpoint only."""
    require_admin(user_id, request)

    rows = db.query_raw(
        """
        SELECT provider,
               count(*) AS calls,
               count(*) FILTER (WHERE status != 'ok') AS failures,
               avg(CASE WHEN "cacheHit" THEN 1.0 ELSE 0.0 END) AS cache_hit_rate,
               avg("latencyMs") AS avg_latency_ms,
               sum(coalesce("estCostUsd", 0)) AS est_cost_usd
        FROM "ApiCallLog"
        WHERE "createdAt" >= date_trunc('day', now())
        GROUP BY provider
        ORDER BY provider
        """
    )
    return {"window": "today", "providers": rows, "circuit_breakers": llm_client.breaker_states()}


class Page:
    """Pagination params with a HARD cap (a request above the cap is a 422,
    not silently clamped, so a client can't be surprised by a short page)."""

    def __init__(
        self,
        limit: int = Query(default=50, ge=1, le=settings.max_page_size),
        offset: int = Query(default=0, ge=0, le=100_000),
    ):
        self.limit = limit
        self.offset = offset


@app.get("/api/clients")
def list_clients(request: Request, page: Page = Depends(), user_id: str = Depends(get_current_user)):
    identity = resolve_identity(request, user_id)
    result = ratelimit.check("global_api", identity)
    if not result.allowed:
        raise RateLimitExceeded(result)

    owner = None if user_id == DEMO_USER_ID else user_id
    profiles = db.clientprofile.find_many(
        where={"clerkUserId": owner}, take=page.limit, skip=page.offset, order=[{"name": "asc"}, {"id": "asc"}]
    )
    return [_serialize_client(p) for p in profiles]


class CreateClientRequest(BaseModel):
    name: str
    portfolio_themes: List[str]

    @field_validator("name")
    @classmethod
    def _norm_name(cls, v: str) -> str:
        v = validation.normalize_text(v)
        if len(v) > settings.max_name_chars:
            raise ValueError(f"must be at most {settings.max_name_chars} characters")
        return v

    @field_validator("portfolio_themes")
    @classmethod
    def _norm_themes(cls, v: List[str]) -> List[str]:
        return validation.normalize_themes(v, max_count=settings.max_themes, max_chars=settings.max_theme_chars)


@app.post("/api/clients", status_code=201)
def create_client(
    payload: CreateClientRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Client name is required")

    themes = [t.strip() for t in payload.portfolio_themes if t.strip()]
    if not themes:
        raise HTTPException(status_code=400, detail="At least one portfolio theme is required")

    owner = None if user_id == DEMO_USER_ID else user_id
    existing = db.clientprofile.find_many(where={"clerkUserId": owner}, take=settings.max_clients_per_user + 1)
    if len(existing) >= settings.max_clients_per_user:
        raise HTTPException(
            status_code=400, detail=f"A maximum of {settings.max_clients_per_user} clients is allowed per account"
        )

    client = db.clientprofile.create(
        data={
            "name": name,
            "portfolio_themes": themes,
            "clerkUserId": None if user_id == DEMO_USER_ID else user_id,
        }
    )
    return _serialize_client(client)


@app.patch("/api/clients/{client_id}")
def update_client(
    client_id: str,
    payload: CreateClientRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Edit a client's name and portfolio themes (same validation as create)."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    get_owned_client_or_404(client_id, user_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Client name is required")
    themes = [t for t in payload.portfolio_themes if t.strip()]
    if not themes:
        raise HTTPException(status_code=400, detail="At least one portfolio theme is required")

    updated = db.clientprofile.update(where={"id": client_id}, data={"name": name, "portfolio_themes": themes})
    audit.log_event(db, action="client_updated", user_id=user_id, client_id=client_id, metadata={"themes": len(themes)})
    return _serialize_client(updated)


@app.delete("/api/clients/{client_id}", status_code=204)
def delete_client(client_id: str, request: Request, user_id: str = Depends(get_current_user)):
    """Permanently deletes a client and everything hanging off it (briefings,
    revisions, citations, delivery logs, run history, holdings) in ONE
    transaction. The AuditLog is append-only and is NOT deleted: its rows keep
    their `clientId` string and are only detached (briefingId -> NULL) from the
    briefings being removed, and a `client_deleted` entry is written. Refused
    with 409 while a run is in flight for this client."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    client = get_owned_client_or_404(client_id, user_id)

    lock_key = f"run:{client_id}"
    if not ratelimit.try_lock(lock_key):
        raise HTTPException(
            status_code=409, detail="A briefing run is in progress for this client; try again after it finishes"
        )
    try:
        mine = {"is": {"clientProfileId": client_id}}
        with db.tx() as tx:
            tx.citation.delete_many(where={"briefing": mine})
            tx.briefingrevision.delete_many(where={"briefing": mine})
            tx.deliverylog.delete_many(where={"briefing": mine})
            tx.auditlog.update_many(where={"briefing": mine}, data={"briefingId": None})
            tx.briefing.delete_many(where={"clientProfileId": client_id})
            tx.runlog.delete_many(where={"clientProfileId": client_id})
            tx.holding.delete_many(where={"clientProfileId": client_id})
            tx.clientprofile.delete(where={"id": client_id})
    finally:
        ratelimit.release_lock(lock_key)
    audit.log_event(
        db, action="client_deleted", user_id=user_id, client_id=client_id, metadata={"name_length": len(client.name)}
    )
    return Response(status_code=204)


@app.get("/api/clients/{client_id}/briefings")
def get_client_briefings(
    client_id: str,
    request: Request,
    page: Page = Depends(),
    user_id: str = Depends(get_current_user),
):
    identity = resolve_identity(request, user_id)
    result = ratelimit.check("global_api", identity)
    if not result.allowed:
        raise RateLimitExceeded(result)

    client = get_owned_client_or_404(client_id, user_id)

    briefings = db.briefing.find_many(
        where={"clientProfileId": client_id},
        include={"citations": True, "revisions": True},
        order={"createdAt": "desc"},
        take=page.limit,
        skip=page.offset,
    )

    return {
        "client": _serialize_client(client),
        "briefings": [_serialize_briefing(b) for b in briefings],
    }


@app.get("/api/me/limits")
def get_my_limits(request: Request, user_id: str = Depends(get_current_user)):
    identity = resolve_identity(request, user_id)
    run_res = ratelimit.peek("run", identity)
    # Real usage from ApiCallLog (Phase 3), not Phase 1's RateLimitHit
    # estimate -- check_daily_budget() is itself a read-only count, so
    # this is already "peek" semantics, nothing is consumed.
    budget_res = _budget_result(llm_client.check_daily_budget(provider="gemini"))
    return {
        "run": {
            "limit": run_res.limit,
            "remaining": run_res.remaining,
            "reset_at": run_res.reset_at,
        },
        "daily_llm_budget": {
            "limit": budget_res.limit,
            "remaining": budget_res.remaining,
            "reset_at": budget_res.reset_at,
        },
    }


# Serializes the check-then-start window of /run admission within this
# process (capacity cap, daily-budget read, RunLog insert, thread start), so
# two concurrent requests cannot both read "1 slot left" and both start. The
# per-user hourly limit and per-client lock have their own atomic stores
# (see ratelimit.py); this lock closes the remaining budget/capacity gap.
_admission_lock = threading.Lock()


@app.post("/api/clients/{client_id}/run", status_code=202)
def run_briefing(client_id: str, request: Request, user_id: str = Depends(get_current_user)):
    """Starts the LangGraph agent for this client on a background thread
    and returns `{run_id}` immediately (202) -- the caller watches
    `GET /api/runs/{run_id}/events` (SSE) or polls `GET /api/runs/{run_id}`
    for progress instead of blocking on the HTTP request for the 20-90s a
    full run usually takes.

    Rate limiting and the per-client concurrency lock are both resolved
    HERE, at run-creation time, synchronously, before the background
    thread ever starts -- never at stream-connect time. The lock is
    released by the background thread itself once the run actually
    finishes, crashes, or times out (see runs.start_background_run), via a
    `finally`, so a crash can't hold it forever (recover_stuck_runs is the
    other half of that guarantee, for a crash that takes the whole
    process down before even that `finally` runs)."""
    identity = resolve_identity(request, user_id)

    global_res = ratelimit.check("global_api", identity)
    if not global_res.allowed:
        raise RateLimitExceeded(global_res)

    client = get_owned_client_or_404(client_id, user_id)

    lock_key = f"run:{client_id}"
    if not ratelimit.try_lock(lock_key):
        raise HTTPException(status_code=409, detail="A briefing run is already in progress for this client")

    released = {"done": False}

    def _release_once():
        if not released["done"]:
            released["done"] = True
            ratelimit.release_lock(lock_key)

    try:
        _admission_lock.acquire()
        # Capacity first, BEFORE consuming the user's hourly run slot -- a
        # server-side capacity rejection must not burn their quota.
        if runs.active_run_count() >= settings.max_concurrent_runs:
            metrics.RUN_ADMISSION_REJECTIONS.labels("capacity").inc()
            raise HTTPException(
                status_code=503,
                detail="The server is at its concurrent-run capacity; try again shortly",
                headers={"Retry-After": "30"},
            )

        run_res = ratelimit.check("run", identity)
        if not run_res.allowed:
            raise RateLimitExceeded(run_res, error="run_limit_exceeded")

        # Phase 8: DEMO_MODE=true (or DEMO_MODE=auto with no GEMINI_API_KEY
        # configured -- exactly docker-compose's `local` profile with no
        # .env) serves a pre-recorded real-run fixture unconditionally,
        # skipping the real daily-budget check entirely (no live call is
        # about to happen, so there's no budget to check).
        if demo_mode.should_use_demo_data():
            run_log = runs.create_run_log(db, client.id, user_id, trigger="demo")
            runs.start_demo_run(db, run_log.id, client.id, release_lock=_release_once)
            return JSONResponse(
                status_code=202,
                content={"run_id": run_log.id, "isDemo": True},
                headers={
                    "X-RateLimit-Limit": str(run_res.limit),
                    "X-RateLimit-Remaining": str(run_res.remaining),
                    "X-RateLimit-Reset": str(int(run_res.reset_at)),
                },
            )

        # Real usage from ApiCallLog (Phase 3) -- was Phase 1's
        # RateLimitHit-based estimate; checked before the run starts so a
        # Gemini-quota-exhausted day rejects cleanly instead of burning
        # quota mid-run and failing.
        budget = llm_client.check_daily_budget(provider="gemini")
        # ApiCallLog only sees calls that have already happened. Every run
        # already in flight is about to make more, so each one reserves at
        # least one unit -- a conservative bound that stops a burst of
        # concurrent admissions all reading the same stale "used" count.
        budget["used"] += runs.active_run_count()
        budget["allowed"] = budget["allowed"] and budget["used"] < budget["limit"]
        budget_res = _budget_result(budget)
        if not budget_res.allowed:
            metrics.RUN_ADMISSION_REJECTIONS.labels("daily_budget").inc()
            # Phase 8: a live demo should never show a raw 429 here -- fall
            # back to a pre-recorded fixture for this one run instead of
            # rejecting it, same fixture pool DEMO_MODE=true would use.
            run_log = runs.create_run_log(db, client.id, user_id, trigger="demo")
            runs.start_demo_run(db, run_log.id, client.id, release_lock=_release_once)
            return JSONResponse(status_code=202, content={"run_id": run_log.id, "isDemo": True})

        run_log = runs.create_run_log(db, client.id, user_id, trigger="manual")
        runs.start_background_run(
            run_log.id,
            client.id,
            user_id,
            client.portfolio_themes,
            trigger="manual",
            release_lock=_release_once,
            request_id=getattr(request.state, "request_id", None),
        )

        return JSONResponse(
            status_code=202,
            content={"run_id": run_log.id, "isDemo": False},
            headers={
                "X-RateLimit-Limit": str(run_res.limit),
                "X-RateLimit-Remaining": str(run_res.remaining),
                "X-RateLimit-Reset": str(int(run_res.reset_at)),
            },
        )
    except Exception:
        _release_once()
        raise
    finally:
        # acquire() is the first statement of the try block, so every exit
        # path (return, raise) holds the lock here.
        _admission_lock.release()


def _get_owned_run_log_or_404(run_id: str, user_id: str):
    run_log = db.runlog.find_unique(where={"id": run_id})
    if not run_log:
        raise HTTPException(status_code=404, detail="Run not found")
    get_owned_client_or_404(run_log.clientProfileId, user_id)
    return run_log


def _get_current_user_for_stream(request: Request, authorization: Optional[str] = Header(default=None)) -> str:
    """Same auth as get_current_user, but also accepts the token via a
    `?token=` query param: browser EventSource (used by the SSE stream)
    cannot set a custom Authorization header, so this is the one route
    that needs the fallback. Never logged/echoed back."""
    from auth import resolve_user_from_token

    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :].strip()
    elif request.query_params.get("token"):
        token = request.query_params.get("token")
    return resolve_user_from_token(token, request)


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, request: Request, user_id: str = Depends(_get_current_user_for_stream)):
    """SSE stream of this run's node-transition events. Auth + ownership
    are checked here (the run's owning client must belong to `user_id`,
    same 404-not-403 semantics as every other client-scoped route) --
    deliberately NOT rate-limited, since rate limiting applies at run
    CREATION, not at stream connect (a client reconnecting to watch a run
    it already started shouldn't burn another slot). Supports resuming
    from a `Last-Event-ID` header or `?last_event_id=` query param."""
    run_log = _get_owned_run_log_or_404(run_id, user_id)

    last_event_id = 0
    header_val = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if header_val:
        try:
            last_event_id = int(header_val)
        except ValueError:
            last_event_id = 0

    return StreamingResponse(
        runs.sse_stream(run_log.id, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user_id: str = Depends(get_current_user)):
    """Polling fallback for clients whose SSE connection fails (proxy,
    firewall, browser extension, etc.) -- same data an SSE `done` event
    carries, read back from the persisted RunLog row."""
    run_log = _get_owned_run_log_or_404(run_id, user_id)
    return _serialize_run_log(run_log)


@app.get("/api/runs")
def list_runs(
    request: Request,
    client_id: Optional[str] = None,
    page: Page = Depends(),
    user_id: str = Depends(get_current_user),
):
    """Run history: status, duration, trigger, and a link (briefingId) to
    the resulting briefing, newest first. Scoped to clients owned by the
    caller (or, in demo mode, the unowned demo clients) -- never another
    tenant's runs."""
    identity = resolve_identity(request, user_id)
    result = ratelimit.check("global_api", identity)
    if not result.allowed:
        raise RateLimitExceeded(result)

    if client_id:
        client = get_owned_client_or_404(client_id, user_id)
        owned_ids = [client.id]
    else:
        if user_id == DEMO_USER_ID:
            owned_clients = db.clientprofile.find_many(where={"clerkUserId": None})
        else:
            owned_clients = db.clientprofile.find_many(where={"clerkUserId": user_id})
        owned_ids = [c.id for c in owned_clients]

    all_runs = []
    for cid in owned_ids:
        rows = db.runlog.find_many(
            where={"clientProfileId": cid}, order={"startedAt": "desc"}, take=page.offset + page.limit
        )
        all_runs.extend(rows)
    all_runs.sort(key=lambda r: r.startedAt, reverse=True)
    return [_serialize_run_log(r) for r in all_runs[page.offset : page.offset + page.limit]]


def _owned_client_ids(user_id: str) -> List[str]:
    if user_id == DEMO_USER_ID:
        owned_clients = db.clientprofile.find_many(where={"clerkUserId": None})
    else:
        owned_clients = db.clientprofile.find_many(where={"clerkUserId": user_id})
    return [c.id for c in owned_clients]


# --------------------------------------------------------------------------
# Phase 7: human-in-the-loop approval workflow + review queue
# --------------------------------------------------------------------------


def _norm_reason(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = validation.normalize_text(v, multiline=True)
    if len(v) > 2000:
        raise ValueError("must be at most 2000 characters")
    return v


class ApproveRequest(BaseModel):
    override_reason: Optional[str] = None

    _check = field_validator("override_reason")(_norm_reason)


class RejectRequest(BaseModel):
    reason: Optional[str] = None

    _check = field_validator("reason")(_norm_reason)


class EditBriefingRequest(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def _norm_content(cls, v: str) -> str:
        v = validation.normalize_text(v, multiline=True)
        if len(v) > settings.max_briefing_chars:
            raise ValueError(f"must be at most {settings.max_briefing_chars} characters")
        return v


@app.patch("/api/briefings/{briefing_id}/approve")
def approve_briefing(
    briefing_id: str,
    payload: ApproveRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Owner-only. Only a `needs_human_review` or `compliant` briefing can
    be approved; only `approved` briefings are deliverable (PDF/email).
    Approving a briefing that still has unresolved compliance violations
    requires an explicit, mandatory `override_reason` -- stored on the
    Briefing row AND in the AuditLog entry, never silently allowed."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    briefing = _get_owned_briefing_or_404(briefing_id, user_id)
    from_status = str(briefing.status)
    if from_status not in ("needs_human_review", "compliant"):
        raise HTTPException(status_code=400, detail=f"A briefing with status '{from_status}' cannot be approved")

    violations = briefing.complianceViolations or []
    override_reason = (payload.override_reason or "").strip() or None
    if violations and not override_reason:
        raise HTTPException(
            status_code=400,
            detail="This briefing has unresolved compliance violations. Provide override_reason to approve anyway.",
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    updated = db.briefing.update(
        where={"id": briefing_id},
        data={
            "status": BriefingStatus.approved,
            "reviewedAt": now,
            "reviewedBy": user_id,
            "overrideReason": override_reason,
        },
    )
    audit.log_event(
        db,
        action="briefing_approved",
        user_id=user_id,
        client_id=briefing.clientProfileId,
        briefing_id=briefing_id,
        metadata={"from_status": from_status, "to_status": "approved", "override_reason": override_reason},
    )
    return _serialize_briefing(
        db.briefing.find_unique(where={"id": briefing_id}, include={"citations": True, "revisions": True}) or updated
    )


@app.patch("/api/briefings/{briefing_id}/reject")
def reject_briefing(
    briefing_id: str,
    payload: RejectRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Owner-only, audited. A briefing already approved or rejected cannot
    be re-rejected (finalized review states are terminal)."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    briefing = _get_owned_briefing_or_404(briefing_id, user_id)
    from_status = str(briefing.status)
    if from_status in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"A briefing with status '{from_status}' cannot be rejected")

    now = datetime.datetime.now(datetime.timezone.utc)
    db.briefing.update(
        where={"id": briefing_id},
        data={"status": BriefingStatus.rejected, "reviewedAt": now, "reviewedBy": user_id},
    )
    audit.log_event(
        db,
        action="briefing_rejected",
        user_id=user_id,
        client_id=briefing.clientProfileId,
        briefing_id=briefing_id,
        metadata={"from_status": from_status, "to_status": "rejected", "reason": payload.reason},
    )
    return _serialize_briefing(
        db.briefing.find_unique(where={"id": briefing_id}, include={"citations": True, "revisions": True})
    )


@app.patch("/api/briefings/{briefing_id}")
def edit_briefing(
    briefing_id: str,
    payload: EditBriefingRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """The review queue's "edit then re-check" action: saves the advisor's
    edited text as a new BriefingRevision and re-runs the FULL compliance
    engine (rules + judge, same veto/fail-safe semantics as the graph's own
    critic node -- see compliance/verdict.py) on the edited text, rather
    than trusting the edit blindly. A finalized briefing (approved/rejected)
    cannot be edited."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    briefing = _get_owned_briefing_or_404(briefing_id, user_id)
    from_status = str(briefing.status)
    if from_status in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"A briefing with status '{from_status}' cannot be edited")

    verdict = compliance_verdict.evaluate(content, run_id=briefing.runId)
    new_status = (
        BriefingStatus.needs_human_review
        if (verdict.judge_unavailable or not verdict.compliant)
        else BriefingStatus.compliant
    )
    next_revision_index = len(briefing.revisions or [])

    db.briefingrevision.create(
        data={
            "briefingId": briefing_id,
            "revisionIndex": next_revision_index,
            "content": content,
            "violations": Json(verdict.violations),
            "criticModel": verdict.critic_model,
            "isCompliant": verdict.compliant and not verdict.judge_unavailable,
        }
    )
    db.briefing.update(
        where={"id": briefing_id},
        data={
            "content": content,
            "complianceViolations": Json(verdict.violations),
            "revisionCount": briefing.revisionCount + 1,
            "status": new_status,
        },
    )
    audit.log_event(
        db,
        action="briefing_edited",
        user_id=user_id,
        client_id=briefing.clientProfileId,
        briefing_id=briefing_id,
        metadata={
            "from_status": from_status,
            "to_status": str(new_status),
            "violation_count": len(verdict.violations),
        },
    )
    return _serialize_briefing(
        db.briefing.find_unique(where={"id": briefing_id}, include={"citations": True, "revisions": True})
    )


@app.get("/api/review-queue")
def review_queue(request: Request, page: Page = Depends(), user_id: str = Depends(get_current_user)):
    """Briefings with status=needs_human_review across every client the
    caller owns, newest first -- the review queue the master prompt asks
    for, with violations already attached (via _serialize_briefing) so the
    frontend can render them inline without a second request per item."""
    identity = resolve_identity(request, user_id)
    result = ratelimit.check("global_api", identity)
    if not result.allowed:
        raise RateLimitExceeded(result)

    owned_ids = _owned_client_ids(user_id)
    items = []
    for cid in owned_ids:
        rows = db.briefing.find_many(
            where={"clientProfileId": cid, "status": BriefingStatus.needs_human_review},
            include={"citations": True, "revisions": True},
            order={"createdAt": "desc"},
            take=page.offset + page.limit,
        )
        items.extend(rows)
    items.sort(key=lambda b: b.createdAt, reverse=True)
    return [_serialize_briefing(b) for b in items[page.offset : page.offset + page.limit]]


# --------------------------------------------------------------------------
# Phase 7: PDF download
# --------------------------------------------------------------------------


@app.get("/api/briefings/{briefing_id}/pdf")
def download_briefing_pdf(briefing_id: str, request: Request, user_id: str = Depends(get_current_user)):
    """Owner-only PDF of one briefing: content, sources, compliance status,
    approval status/timestamp, and the required disclaimer (see
    backend/delivery.py). Available regardless of review status (an
    advisor may want a PDF while still reviewing); email DELIVERY (below)
    is the step gated to approved-only."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "pdf"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    briefing = _get_owned_briefing_or_404(briefing_id, user_id)
    client = db.clientprofile.find_unique(where={"id": briefing.clientProfileId})
    pdf_bytes = delivery.render_briefing_pdf(_serialize_briefing(briefing), client.name if client else "Client")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="briefing-{briefing_id}.pdf"'},
    )


# --------------------------------------------------------------------------
# Phase 7: email delivery (disabled by default)
# --------------------------------------------------------------------------


class DeliverRequest(BaseModel):
    recipient: str

    @field_validator("recipient")
    @classmethod
    def _norm_recipient(cls, v: str) -> str:
        v = validation.normalize_text(v)
        if len(v) > 254:
            raise ValueError("must be at most 254 characters")
        return v


@app.post("/api/briefings/{briefing_id}/deliver")
def deliver_briefing(
    briefing_id: str,
    payload: DeliverRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """Email delivery. Disabled by default (EMAIL_DELIVERY_ENABLED=false
    -> 501). Even when enabled: only an `approved` briefing may be
    delivered, only to a recipient whose domain is allowlisted
    (EMAIL_ALLOWED_RECIPIENT_DOMAINS), and idempotently per
    (briefingId, recipient) -- a repeat request for the same pair returns
    the prior result instead of sending a second email."""
    if not delivery.email_delivery_enabled():
        raise HTTPException(status_code=501, detail="Email delivery is disabled on this deployment")

    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    recipient = payload.recipient.strip().lower()
    if not recipient or "@" not in recipient:
        raise HTTPException(status_code=400, detail="A valid recipient email address is required")
    if not delivery.recipient_domain_allowed(recipient):
        raise HTTPException(status_code=400, detail="Recipient domain is not on the allowlist")

    briefing = _get_owned_briefing_or_404(briefing_id, user_id)
    if str(briefing.status) != "approved":
        raise HTTPException(status_code=400, detail="Only an approved briefing can be delivered")

    existing = db.deliverylog.find_first(where={"briefingId": briefing_id, "recipient": recipient})
    if existing:
        return {"already_sent": True, "status": existing.status}

    client = db.clientprofile.find_unique(where={"id": briefing.clientProfileId})
    pdf_bytes = delivery.render_briefing_pdf(_serialize_briefing(briefing), client.name if client else "Client")
    send_result = delivery.send_email(recipient, f"Morning Briefing — {client.name if client else ''}", pdf_bytes)

    db.deliverylog.create(
        data={
            "briefingId": briefing_id,
            "recipient": recipient,
            "status": "sent" if send_result.sent else "failed",
            "errorMessage": send_result.error,
        }
    )
    audit.log_event(
        db,
        action="briefing_delivered",
        user_id=user_id,
        client_id=briefing.clientProfileId,
        briefing_id=briefing_id,
        metadata={"recipient": recipient, "sent": send_result.sent, "error": send_result.error},
    )
    if not send_result.sent:
        raise HTTPException(status_code=502, detail=f"Email delivery failed: {send_result.error}")
    return {"sent": True}


# --------------------------------------------------------------------------
# Phase 7: CSV holdings upload
# --------------------------------------------------------------------------


@app.get("/api/clients/{client_id}/holdings")
def list_holdings(client_id: str, request: Request, user_id: str = Depends(get_current_user)):
    identity = resolve_identity(request, user_id)
    result = ratelimit.check("global_api", identity)
    if not result.allowed:
        raise RateLimitExceeded(result)

    get_owned_client_or_404(client_id, user_id)
    rows = db.holding.find_many(where={"clientProfileId": client_id})
    return [_serialize_holding(h) for h in rows]


@app.post("/api/clients/{client_id}/holdings")
async def upload_holdings(
    client_id: str,
    request: Request,
    file: UploadFile,
    user_id: str = Depends(get_current_user),
):
    """CSV upload, replace-on-upload within a single transaction (see
    backend/holdings.py). Strict validation (ticker format, weight range
    + sum sanity, no formula/CSV-injection payloads), a byte-size cap
    checked BEFORE parsing, and a row cap -- never trusts the uploaded
    filename for any filesystem path (the file is parsed entirely from its
    in-memory bytes)."""
    identity = resolve_identity(request, user_id)
    for policy in ("global_api", "write", "upload"):
        result = ratelimit.check(policy, identity)
        if not result.allowed:
            raise RateLimitExceeded(result)

    get_owned_client_or_404(client_id, user_id)

    raw = await file.read()
    try:
        rows = holdings_module.parse_and_validate_csv(raw)
    except holdings_module.HoldingsValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    count = holdings_module.replace_holdings(db, client_id, rows)
    audit.log_event(db, action="holdings_uploaded", user_id=user_id, client_id=client_id, metadata={"count": count})
    return {"count": count}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8010)

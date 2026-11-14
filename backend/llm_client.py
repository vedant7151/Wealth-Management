"""Provider abstraction for every outbound LLM/Tavily/embedding call the
graph makes. agent.py and backend/compliance/* call ONLY the functions in
this module -- generate_text, generate_json, embed_text, embed_batch,
tavily_search, call_groq_classifier -- never a raw provider SDK client
directly (grep for "gemini_client." / "tavily_client." / "groq_client."
outside this file; it should find nothing).

What this module gives every call site, uniformly:
  - a provider fallback chain for chat/JSON generation (Gemini -> Groq by
    default, env-configurable) with per-provider retry (exponential
    backoff + jitter) on retryable errors (429/5xx/timeout);
  - a per-provider circuit breaker: after CIRCUIT_BREAKER_THRESHOLD
    consecutive transport failures, that provider is skipped for
    CIRCUIT_BREAKER_COOLDOWN_SECONDS and callers fall straight through to
    the next provider (state exposed via breaker_states(), surfaced in
    GET /readyz);
  - a per-call timeout;
  - an ApiCallLog row for every single call (never raises on its own
    logging failure -- see _log_call);
  - model names from env, not hardcoded literals scattered across files.

Call ONLY through configure(db_getter) first (agent.py does this once at
import time) so logging/budget-checking has a DB connection to write to;
without it, calls still work but logging silently no-ops (never crashes
the graph over a logging failure)."""

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, cast

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from groq import Groq
from tavily import TavilyClient

import metrics
import model_prices

# Loaded here (not just in agent.py) because this module constructs
# provider clients from env vars at IMPORT time, and compliance/judge.py
# and compliance/injection_guard.py import this module before agent.py's
# own load_dotenv() call runs (their imports sit above it) -- idempotent,
# safe to call more than once per process.
load_dotenv()

logger = logging.getLogger("llm_client")

# --------------------------------------------------------------------------
# Provider clients (the ONLY place these SDK clients are constructed)
# --------------------------------------------------------------------------


class _LazyClient:
    """Builds a provider SDK client on first USE, not at import. The SDKs raise
    at construction when their API key is missing, which made the whole app
    (and every test/CI run without keys) crash at import -- breaking demo mode,
    which is documented to need no keys. A missing key now surfaces only when a
    real call is attempted, where the retry/fallback/circuit-breaker logic
    handles it like any other provider failure."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._client: Any = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        with self._lock:
            if self._client is None:
                self._client = self._factory()
        return getattr(self._client, name)


gemini_client: Any = _LazyClient(lambda: genai.Client())
tavily_client: Any = _LazyClient(lambda: TavilyClient(api_key=os.environ.get("TAVILY_API_KEY")))
groq_client: Any = _LazyClient(lambda: Groq(api_key=os.environ.get("GROQ_API_KEY")))
_hf_api_key = os.environ.get("HUGGINGFACE_API_KEY")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.1-8b-instant")
PROVIDER_MODELS = {"gemini": GEMINI_MODEL, "groq": GROQ_CHAT_MODEL}
DEFAULT_PROVIDER_ORDER = [
    p.strip() for p in os.environ.get("LLM_PROVIDER_ORDER", "gemini,groq").split(",") if p.strip()
]

# Embeddings: "gemini" (default; uses the existing GEMINI_API_KEY) or
# "huggingface" (needs HF Inference Providers credits -- the original provider;
# it started returning 402 Payment Required). Either way vectors are 384-dim
# (the pgvector column width) and L2-normalized, so the distance thresholds
# (RETRIEVAL_MAX_DISTANCE, cache similarity) mean the same thing. Vectors from
# different models are NOT comparable: after switching, previously stored
# chunks are simply never matched (retrieval only looks back 48h anyway).
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "gemini").strip().lower()
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
HF_EMBEDDING_MODEL = os.environ.get("HF_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", GEMINI_EMBEDDING_MODEL if EMBEDDING_PROVIDER == "gemini" else HF_EMBEDDING_MODEL
)
EMBEDDING_PROVIDER_LABEL = "gemini_embeddings" if EMBEDDING_PROVIDER == "gemini" else "hf_embeddings"
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
EMBEDDING_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "8"))
EMBEDDING_MAX_CONCURRENCY = int(os.environ.get("EMBEDDING_MAX_CONCURRENCY", "4"))

CALL_TIMEOUT_SECONDS = float(os.environ.get("LLM_CALL_TIMEOUT_SECONDS", "30"))
MAX_ATTEMPTS_PER_PROVIDER = int(os.environ.get("LLM_MAX_ATTEMPTS_PER_PROVIDER", "2"))
RETRY_BASE_DELAY_SECONDS = float(os.environ.get("LLM_RETRY_BASE_DELAY_SECONDS", "0.5"))
RETRY_MAX_DELAY_SECONDS = float(os.environ.get("LLM_RETRY_MAX_DELAY_SECONDS", "8"))

CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_COOLDOWN_SECONDS = float(os.environ.get("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60"))

DAILY_LLM_BUDGET = int(os.environ.get("DAILY_LLM_BUDGET", "18"))


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class CircuitBreaker:
    """Per-provider. After `threshold` consecutive failures, `is_open()`
    returns True for `cooldown_seconds`, then auto-resets (half-open: the
    next call is allowed through; success closes it, failure reopens and
    restarts the cooldown)."""

    def __init__(
        self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, cooldown_seconds: float = CIRCUIT_BREAKER_COOLDOWN_SECONDS
    ):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def is_open(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            if self._opened_at is None:
                return False
            if now - self._opened_at >= self.cooldown_seconds:
                # Cooldown elapsed: half-open -- let the next call through.
                self._opened_at = None
                self._consecutive_failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.threshold:
                self._opened_at = now

    def state(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        with self._lock:
            opened_at = self._opened_at
            open_ = opened_at is not None and (now - opened_at) < self.cooldown_seconds
            remaining = max(0.0, self.cooldown_seconds - (now - opened_at)) if open_ and opened_at is not None else 0.0
            return {
                "open": open_,
                "consecutive_failures": self._consecutive_failures,
                "cooldown_remaining_seconds": remaining,
            }


_breakers: dict = {}
_breakers_lock = threading.Lock()


def get_breaker(provider: str) -> CircuitBreaker:
    with _breakers_lock:
        if provider not in _breakers:
            _breakers[provider] = CircuitBreaker()
        return _breakers[provider]


def breaker_states() -> dict:
    """Used by GET /readyz."""
    with _breakers_lock:
        return {name: breaker.state() for name, breaker in _breakers.items()}


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LLMError(Exception):
    pass


class ProviderUnavailableError(LLMError):
    """Raised immediately (no attempt made) when a single-provider call's
    circuit breaker is open."""


class EmbeddingDimensionError(LLMError):
    """The embedding API returned a vector of the wrong dimension. Never
    retried -- this is a contract violation (wrong model/API version), not
    a transient failure."""


# --------------------------------------------------------------------------
# DB wiring for logging + the daily budget check
# --------------------------------------------------------------------------

_db_getter: Optional[Callable] = None


def configure(db_getter: Callable) -> None:
    """agent.py calls this once at import time with its own `get_db`
    (the thread-local, one-per-run Prisma connection) so every wrapper in
    this module logs to the SAME connection the rest of the run uses,
    rather than opening a new one."""
    global _db_getter
    _db_getter = db_getter


def _log_call(
    provider: str,
    model: Optional[str],
    purpose: str,
    latency_ms: int,
    status: str,
    run_id: Optional[str] = None,
    est_tokens: Optional[int] = None,
    est_cost_usd: Optional[float] = None,
    cache_hit: bool = False,
) -> None:
    """Never raises -- a logging failure must never crash the graph run
    it's trying to observe."""
    if status != "ok":
        metrics.PROVIDER_FAILURES.labels(provider, status).inc()
    if _db_getter is None:
        logger.debug(f"No db configured; skipping ApiCallLog write for {provider}/{purpose}")
        return
    try:
        db = _db_getter()
        db.apicalllog.create(
            data={
                "provider": provider,
                "model": model,
                "purpose": purpose,
                "runId": run_id,
                "latencyMs": latency_ms,
                "status": status,
                "estTokens": est_tokens,
                "estCostUsd": est_cost_usd,
                "cacheHit": cache_hit,
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to write ApiCallLog for {provider}/{purpose}: {e}")


def check_daily_budget(provider: str = "gemini") -> dict:
    """Reads REAL usage from ApiCallLog (not an estimate) -- every call
    logged against `provider` since midnight UTC today, successful or
    not (a failed attempt still spent real time/quota against the
    provider). Returns {"allowed", "used", "limit"}. Fails CLOSED (same
    as Phase 1's rate-limit budget policy) if the ledger itself can't be
    read -- we'd rather reject a run than risk burning quota blind."""
    import datetime

    if _db_getter is None:
        logger.error("check_daily_budget: no db configured; failing closed")
        return {"allowed": False, "used": 0, "limit": DAILY_LLM_BUDGET}

    start_of_day = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        db = _db_getter()
        rows = db.query_raw(
            'SELECT count(*) AS cnt FROM "ApiCallLog" WHERE provider = $1 AND "createdAt" >= $2::timestamp',
            provider,
            start_of_day,
        )
        used = int(rows[0]["cnt"]) if rows else 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"check_daily_budget: failed to read ApiCallLog, failing closed: {e}")
        return {"allowed": False, "used": 0, "limit": DAILY_LLM_BUDGET}

    return {"allowed": used < DAILY_LLM_BUDGET, "used": used, "limit": DAILY_LLM_BUDGET}


# --------------------------------------------------------------------------
# Retry helpers
# --------------------------------------------------------------------------

_RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "resource_exhausted",
    "unavailable",
    "rate limit",
    "timeout",
    "timed out",
)


def _is_retryable(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if isinstance(status, int) and status in (429, 500, 502, 503, 504):
        return True
    text = str(e).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def _backoff_delay(attempt: int) -> float:
    base = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
    return base + random.uniform(0, base * 0.5)  # nosec B311 -- retry jitter, not security


# Global cap on simultaneous in-flight outbound provider calls (LLM, embedding
# batch, search) across every run/thread in this process. The permit is
# acquired INSIDE the worker thread, so it is held for as long as the
# provider call really is outstanding -- including a call whose caller has
# already given up on it (a Python thread can't be killed) -- and time spent
# waiting for a permit counts against the caller's timeout rather than
# queueing unboundedly.
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "4"))
_call_slots = threading.BoundedSemaphore(LLM_MAX_CONCURRENCY)


def _run_with_timeout(fn: Callable, timeout_seconds: float):
    """Runs `fn` on a worker thread and enforces `timeout_seconds` on the
    CALLER. (Phase 10 fix: the previous `with ThreadPoolExecutor(...)` form
    blocked in `__exit__` until the worker finished, so a hung provider call
    was never actually abandoned and the "timeout" only fired after the call
    returned on its own. `shutdown(wait=False)` lets the caller move on; the
    stuck worker is bounded by `_call_slots` and the SDK's own socket
    timeout.)"""

    def _bounded():
        with _call_slots:
            return fn()

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_bounded)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        raise TimeoutError(f"Call timed out after {timeout_seconds}s") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# Chat/JSON generation (Gemini -> Groq fallback)
# --------------------------------------------------------------------------


def _gemini_call(prompt: str, model: str, json_mode: bool) -> str:
    response = gemini_client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json") if json_mode else None,
    )
    return response.text or ""


def _groq_call(prompt: str, model: str, json_mode: bool) -> str:
    response = groq_client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"} if json_mode else None,
    )
    return response.choices[0].message.content or ""


def _dispatch(provider: str, prompt: str, model: str, json_mode: bool) -> str:
    if provider == "gemini":
        fn = _gemini_call
    elif provider == "groq":
        fn = _groq_call
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
    return _run_with_timeout(lambda: fn(prompt, model, json_mode), CALL_TIMEOUT_SECONDS)


@dataclass
class GenerationResult:
    text: Optional[str] = None
    parsed: Any = None
    provider: Optional[str] = None
    model: Optional[str] = None
    # None on success; "all_providers_unavailable" or "malformed_response" otherwise.
    error: Optional[str] = None


def _generate(
    prompt: str,
    purpose: str,
    run_id: Optional[str],
    provider_order: Optional[List[str]],
    json_mode: bool,
    validate: Optional[Callable[[Any], Any]],
) -> GenerationResult:
    providers = provider_order or DEFAULT_PROVIDER_ORDER
    last_error_kind = "all_providers_unavailable"

    for provider in providers:
        breaker = get_breaker(provider)
        if breaker.is_open():
            logger.warning(f"Circuit open for provider={provider}; skipping straight to fallback")
            continue

        model = PROVIDER_MODELS.get(provider)
        if model is None:
            logger.warning(f"Unknown provider {provider!r} in LLM_PROVIDER_ORDER; skipping")
            continue
        for attempt in range(MAX_ATTEMPTS_PER_PROVIDER):
            t0 = time.time()
            try:
                raw_text = _dispatch(provider, prompt, model, json_mode)
            except Exception as e:  # noqa: BLE001
                latency_ms = int((time.time() - t0) * 1000)
                retryable = _is_retryable(e)
                status = "timeout" if isinstance(e, TimeoutError) else "error"
                _log_call(
                    provider,
                    model,
                    purpose,
                    latency_ms,
                    status,
                    run_id=run_id,
                    est_tokens=model_prices.estimate_tokens(prompt),
                )
                last_error_kind = "all_providers_unavailable"
                if not retryable or attempt == MAX_ATTEMPTS_PER_PROVIDER - 1:
                    breaker.record_failure()
                    logger.warning(f"{provider} exhausted ({status}): {e}")
                    break
                time.sleep(_backoff_delay(attempt))
                continue

            latency_ms = int((time.time() - t0) * 1000)

            if not json_mode:
                breaker.record_success()
                est_tokens = model_prices.estimate_tokens(prompt) + model_prices.estimate_tokens(raw_text)
                est_cost = model_prices.estimate_cost_usd(
                    model, model_prices.estimate_tokens(prompt), model_prices.estimate_tokens(raw_text)
                )
                _log_call(
                    provider,
                    model,
                    purpose,
                    latency_ms,
                    "ok",
                    run_id=run_id,
                    est_tokens=est_tokens,
                    est_cost_usd=est_cost,
                )
                return GenerationResult(text=raw_text, parsed=None, provider=provider, model=model)

            try:
                parsed = json.loads(raw_text)
                normalized = validate(parsed) if validate else parsed
                if validate and normalized is None:
                    raise ValueError("validate() rejected the response shape")
            except Exception as e:  # noqa: BLE001
                _log_call(provider, model, purpose, latency_ms, "malformed", run_id=run_id)
                last_error_kind = "malformed_response"
                logger.warning(
                    f"{provider} returned malformed/invalid JSON (attempt {attempt + 1}/{MAX_ATTEMPTS_PER_PROVIDER}): {e}"
                )
                if attempt == MAX_ATTEMPTS_PER_PROVIDER - 1:
                    break
                time.sleep(_backoff_delay(attempt))
                continue

            breaker.record_success()
            est_tokens = model_prices.estimate_tokens(prompt) + model_prices.estimate_tokens(raw_text)
            est_cost = model_prices.estimate_cost_usd(
                model, model_prices.estimate_tokens(prompt), model_prices.estimate_tokens(raw_text)
            )
            _log_call(
                provider, model, purpose, latency_ms, "ok", run_id=run_id, est_tokens=est_tokens, est_cost_usd=est_cost
            )
            return GenerationResult(text=raw_text, parsed=normalized, provider=provider, model=model)

    return GenerationResult(error=last_error_kind)


def generate_text(
    prompt: str, purpose: str, run_id: Optional[str] = None, provider_order: Optional[List[str]] = None
) -> GenerationResult:
    return _generate(prompt, purpose, run_id, provider_order, json_mode=False, validate=None)


def generate_json(
    prompt: str,
    purpose: str,
    run_id: Optional[str] = None,
    provider_order: Optional[List[str]] = None,
    validate: Optional[Callable[[Any], Any]] = None,
) -> GenerationResult:
    """`validate(parsed) -> normalized_value_or_None`: return None (or
    raise) to signal a successfully-parsed-but-wrong-shaped response,
    which gets the same "malformed, retry once" treatment as invalid JSON
    text. Omit to accept any valid JSON as-is."""
    return _generate(prompt, purpose, run_id, provider_order, json_mode=True, validate=validate)


# --------------------------------------------------------------------------
# Embeddings (HuggingFace -- single provider, no fallback chain, but
# still retried/timed-out/circuit-broken/logged like everything else)
# --------------------------------------------------------------------------


def _hf_embed_call(texts: List[str]) -> List[List[float]]:
    payload = texts[0] if len(texts) == 1 else texts
    response = requests.post(
        f"https://router.huggingface.co/hf-inference/models/{HF_EMBEDDING_MODEL}",
        headers={"Authorization": f"Bearer {_hf_api_key}", "Content-Type": "application/json"},
        json={"inputs": payload},
        timeout=CALL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    res_json = response.json()

    if len(texts) == 1:
        if isinstance(res_json, list) and res_json and isinstance(res_json[0], list):
            return [res_json[0]]
        if isinstance(res_json, list):
            return [res_json]
        raise ValueError(f"Unexpected HF embedding response shape: {res_json!r}")

    if isinstance(res_json, list) and len(res_json) == len(texts):
        return res_json
    raise ValueError(f"Unexpected HF batch embedding response shape for {len(texts)} inputs: {res_json!r}")


def _l2_normalize(vec: List[float]) -> List[float]:
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else vec


def _gemini_embed_call(texts: List[str]) -> List[List[float]]:
    """gemini-embedding-001 with output_dimensionality=EMBEDDING_DIM. Reduced-
    dimension outputs are not unit length, so they are normalized here."""
    response = gemini_client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=cast(Any, texts),
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM, task_type="SEMANTIC_SIMILARITY"),
    )
    vecs = [list(e.values or []) for e in (response.embeddings or [])]
    if len(vecs) != len(texts):
        raise ValueError(f"Gemini returned {len(vecs)} embeddings for {len(texts)} inputs")
    return [_l2_normalize(v) for v in vecs]


def _embed_call(texts: List[str]) -> List[List[float]]:
    """Single seam for the configured embedding provider (tests patch this)."""
    return _gemini_embed_call(texts) if EMBEDDING_PROVIDER == "gemini" else _hf_embed_call(texts)


def _assert_dim(vec: List[float], context: str = "") -> None:
    if len(vec) != EMBEDDING_DIM:
        raise EmbeddingDimensionError(f"Expected {EMBEDDING_DIM}-dim embedding, got {len(vec)} ({context})")


def _embed_chunk(chunk: List[str], purpose: str, run_id: Optional[str]) -> List[List[float]]:
    breaker = get_breaker(EMBEDDING_PROVIDER_LABEL)
    if breaker.is_open():
        raise ProviderUnavailableError(f"{EMBEDDING_PROVIDER_LABEL} circuit open")

    t0 = time.time()
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS_PER_PROVIDER):
        try:
            vecs = _run_with_timeout(lambda: _embed_call(chunk), CALL_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not _is_retryable(e) or attempt == MAX_ATTEMPTS_PER_PROVIDER - 1:
                break
            time.sleep(_backoff_delay(attempt))
            continue

        # Dimension mismatch is a contract violation, not a transient
        # failure -- fail loudly, never retried, never silently coerced.
        for v in vecs:
            _assert_dim(v, context=purpose)

        latency_ms = int((time.time() - t0) * 1000)
        breaker.record_success()
        est_tokens = sum(model_prices.estimate_tokens(t) for t in chunk)
        _log_call(
            EMBEDDING_PROVIDER_LABEL, EMBEDDING_MODEL, purpose, latency_ms, "ok", run_id=run_id, est_tokens=est_tokens
        )
        return vecs

    latency_ms = int((time.time() - t0) * 1000)
    breaker.record_failure()
    _log_call(EMBEDDING_PROVIDER_LABEL, EMBEDDING_MODEL, purpose, latency_ms, "error", run_id=run_id)
    raise (
        last_exc
        if last_exc is not None
        else ProviderUnavailableError(f"{EMBEDDING_PROVIDER_LABEL}: all attempts failed")
    )


def embed_batch(texts: List[str], purpose: str = "embedding", run_id: Optional[str] = None) -> List[List[float]]:
    """Batches into groups of EMBEDDING_BATCH_SIZE and runs groups
    concurrently (bounded by EMBEDDING_MAX_CONCURRENCY). Raises
    EmbeddingDimensionError loudly on any dimension mismatch; raises the
    underlying provider exception if a chunk exhausts its retries."""
    if not texts:
        return []

    chunks = [texts[i : i + EMBEDDING_BATCH_SIZE] for i in range(0, len(texts), EMBEDDING_BATCH_SIZE)]
    results: List[Optional[List[float]]] = [None] * len(texts)

    with ThreadPoolExecutor(max_workers=min(EMBEDDING_MAX_CONCURRENCY, len(chunks))) as pool:
        futures = {pool.submit(_embed_chunk, chunk, purpose, run_id): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            chunk_idx = futures[future]
            vecs = future.result()  # propagates any exception from the chunk
            start = chunk_idx * EMBEDDING_BATCH_SIZE
            for j, v in enumerate(vecs):
                results[start + j] = v

    return results  # type: ignore[return-value]


def embed_text(text: str, purpose: str = "embedding", run_id: Optional[str] = None) -> List[float]:
    return embed_batch([text], purpose=purpose, run_id=run_id)[0]


# --------------------------------------------------------------------------
# Tavily search (single provider, same retry/breaker/logging treatment)
# --------------------------------------------------------------------------


def tavily_search(query: str, purpose: str = "research_search", run_id: Optional[str] = None, **kwargs) -> dict:
    breaker = get_breaker("tavily")
    if breaker.is_open():
        raise ProviderUnavailableError("tavily circuit open")

    t0 = time.time()
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS_PER_PROVIDER):
        try:
            res = _run_with_timeout(lambda: tavily_client.search(query=query, **kwargs), CALL_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not _is_retryable(e) or attempt == MAX_ATTEMPTS_PER_PROVIDER - 1:
                break
            time.sleep(_backoff_delay(attempt))
            continue

        latency_ms = int((time.time() - t0) * 1000)
        breaker.record_success()
        est_cost = model_prices.estimate_cost_usd("tavily-search", 0, 0)
        _log_call("tavily", "tavily-search", purpose, latency_ms, "ok", run_id=run_id, est_cost_usd=est_cost)
        return res

    latency_ms = int((time.time() - t0) * 1000)
    breaker.record_failure()
    _log_call("tavily", "tavily-search", purpose, latency_ms, "error", run_id=run_id)
    raise last_exc if last_exc is not None else ProviderUnavailableError("tavily: all attempts failed")


# --------------------------------------------------------------------------
# Groq classifier (prompt-guard model) -- single provider, no fallback;
# used by compliance/injection_guard.py, which is itself NOT part of the
# generate_text/generate_json fallback chain (there is no "fallback
# classifier" that makes sense here).
# --------------------------------------------------------------------------


def call_groq_classifier(model: str, text: str, purpose: str = "injection_screen", run_id: Optional[str] = None) -> str:
    breaker = get_breaker("groq_classifier")
    if breaker.is_open():
        raise ProviderUnavailableError("groq_classifier circuit open")

    t0 = time.time()
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_ATTEMPTS_PER_PROVIDER):
        try:
            response = _run_with_timeout(
                lambda: groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": text}],
                    temperature=0.0,
                ),
                CALL_TIMEOUT_SECONDS,
            )
            content = response.choices[0].message.content
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not _is_retryable(e) or attempt == MAX_ATTEMPTS_PER_PROVIDER - 1:
                break
            time.sleep(_backoff_delay(attempt))
            continue

        latency_ms = int((time.time() - t0) * 1000)
        breaker.record_success()
        _log_call(
            "groq_classifier",
            model,
            purpose,
            latency_ms,
            "ok",
            run_id=run_id,
            est_tokens=model_prices.estimate_tokens(text),
        )
        return content

    latency_ms = int((time.time() - t0) * 1000)
    breaker.record_failure()
    _log_call("groq_classifier", model, purpose, latency_ms, "error", run_id=run_id)
    raise last_exc if last_exc is not None else ProviderUnavailableError("groq_classifier: all attempts failed")

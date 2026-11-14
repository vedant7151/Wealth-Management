"""Prometheus metrics (Phase 10). Standalone on purpose: it imports nothing
from the rest of the app, so `ratelimit.py`, `llm_client.py`, `runs.py` and
the HTTP middleware can all record into it without import cycles.

Cardinality is kept bounded: HTTP metrics are labelled by the matched ROUTE
TEMPLATE (`/api/clients/{client_id}/run`), never the raw path, and no label
ever carries a user id, client id, run id, or any free text.

State is per-process (same single-instance caveat as the circuit breakers
and SSE pub/sub -- see docs/OPERATIONS.md)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
RUN_DURATION = Histogram(
    "agent_run_duration_seconds",
    "Graph run wall time",
    ["trigger", "status"],
    buckets=(1, 5, 10, 20, 30, 60, 90, 120, 300, 600),
    registry=REGISTRY,
)
RUNS_TOTAL = Counter("agent_runs_total", "Graph runs finished", ["trigger", "status"], registry=REGISTRY)
PROVIDER_FAILURES = Counter(
    "provider_failures_total", "Failed outbound provider calls", ["provider", "status"], registry=REGISTRY
)
RATE_LIMIT_REJECTIONS = Counter(
    "rate_limit_rejections_total", "Requests rejected by a rate-limit policy", ["policy"], registry=REGISTRY
)
RUN_ADMISSION_REJECTIONS = Counter(
    "run_admission_rejections_total", "POST /run rejected before starting", ["reason"], registry=REGISTRY
)
QUEUE_DEPTH = Gauge("agent_runs_in_flight", "Graph runs currently executing (queue depth)", registry=REGISTRY)
CIRCUIT_OPEN = Gauge(
    "provider_circuit_open", "1 if the provider circuit breaker is open", ["provider"], registry=REGISTRY
)
RETENTION_DELETED = Counter(
    "retention_rows_deleted_total", "Rows purged by the retention job", ["table"], registry=REGISTRY
)


def render() -> tuple:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def set_in_flight(n: int) -> None:
    QUEUE_DEPTH.set(n)

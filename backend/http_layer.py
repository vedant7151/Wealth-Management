"""HTTP hardening (Phase 10): request-id, security headers, request body
size limit, metrics, and a uniform error schema.

Implemented as ONE pure-ASGI middleware (not Starlette's BaseHTTPMiddleware,
which buffers/breaks streaming responses -- this API serves an SSE stream)
so it is safe for `text/event-stream`.

Uniform error body, for EVERY non-2xx the API itself produces:

    {"error": "<machine_code>", "message": "<human text>", "detail": "<same
     text, legacy field the frontend already reads>", "request_id": "..."}

Stack traces never leave the process: an unhandled exception is logged
server-side (redacted) and the client only gets
`{"error": "internal_error", "message": "Internal server error"}` plus the
request id to quote to the operator.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import metrics
from logging_config import request_id_var

logger = logging.getLogger("api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    501: "not_implemented",
    502: "bad_gateway",
    503: "service_unavailable",
}


def error_body(status: int, message: str, request_id: Optional[str], code: Optional[str] = None, **extra) -> dict:
    body = {
        "error": code or _STATUS_CODES.get(status, "http_error"),
        "message": message,
        "detail": message,
        "request_id": request_id,
    }
    body.update(extra)
    return body


class BodyTooLarge(StarletteHTTPException):
    """Raised from the receive wrapper once a streamed body passes the cap.
    It subclasses HTTPException on purpose: FastAPI's body-parsing code
    converts any OTHER exception raised while reading the body into a
    generic 400, but lets an HTTPException through, so the client gets a
    proper 413 from the uniform handler."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


def _security_headers(is_prod: bool, https: bool, hsts_max_age: int, csp: str) -> List[tuple]:
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-site"),
        (b"content-security-policy", csp.encode()),
    ]
    # HSTS is meaningless (and ignored by browsers) over plain HTTP, so it is
    # sent only when this request actually arrived over TLS -- directly, or
    # via a proxy's X-Forwarded-Proto -- or when running as prod, where TLS
    # termination in front of the app is a deployment requirement.
    if hsts_max_age > 0 and (https or is_prod):
        headers.append((b"strict-transport-security", f"max-age={hsts_max_age}; includeSubDomains".encode()))
    return headers


class HardeningMiddleware:
    def __init__(
        self,
        app,
        *,
        max_body_bytes: int,
        is_prod: bool,
        hsts_max_age: int,
        csp: str,
        no_store_prefixes: tuple = ("/api/",),
    ):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.is_prod = is_prod
        self.hsts_max_age = hsts_max_age
        self.csp = csp
        self.no_store_prefixes = no_store_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        inbound = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = inbound if _REQUEST_ID_RE.match(inbound) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        https = scope.get("scheme") == "https" or headers.get(b"x-forwarded-proto", b"") == b"https"
        sec_headers = _security_headers(self.is_prod, https, self.hsts_max_age, self.csp)
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        start = time.perf_counter()
        state: Dict[str, Any] = {"status": 500, "started": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["started"] = True
                state["status"] = message["status"]
                hdrs = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                present = {k.lower() for k, _ in hdrs}
                hdrs.append((b"x-request-id", request_id.encode()))
                for k, v in sec_headers:
                    if k not in present:
                        hdrs.append((k, v))
                if b"cache-control" not in present and path.startswith(self.no_store_prefixes):
                    hdrs.append((b"cache-control", b"no-store"))
                message = {**message, "headers": hdrs}
            await send(message)

        # --- body size limit -------------------------------------------------
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_body_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._respond(
                    send_wrapper, 413, "Request body too large", request_id, extra={"max_bytes": self.max_body_bytes}
                )
                self._observe(scope, method, state, start)
                request_id_var.reset(token)
                return

        received = {"n": 0}

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                received["n"] += len(message.get("body", b""))
                if received["n"] > self.max_body_bytes:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLarge:
            if not state["started"]:
                await self._respond(
                    send_wrapper, 413, "Request body too large", request_id, extra={"max_bytes": self.max_body_bytes}
                )
        except Exception:
            # Full detail goes to the (redacted) server log only.
            logger.exception("Unhandled error on %s %s", method, path)
            if not state["started"]:
                await self._respond(send_wrapper, 500, "Internal server error", request_id, code="internal_error")
        finally:
            self._observe(scope, method, state, start)
            request_id_var.reset(token)

    @staticmethod
    async def _respond(send, status: int, message: str, request_id: str, code: Optional[str] = None, extra=None):
        body = json.dumps(error_body(status, message, request_id, code=code, **(extra or {}))).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _observe(scope, method: str, state: dict, start: float) -> None:
        route = scope.get("route")
        # Route TEMPLATE, never the raw path: bounded label cardinality.
        route_label = getattr(route, "path", None) or "unmatched"
        metrics.HTTP_REQUESTS.labels(method, route_label, str(state["status"])).inc()
        if not route_label.endswith("/events"):  # SSE streams are open for minutes; not a latency sample
            metrics.HTTP_LATENCY.labels(method, route_label).observe(time.perf_counter() - start)


def register_error_handlers(app: FastAPI) -> None:
    def _rid(request: Request) -> Optional[str]:
        return getattr(request.state, "request_id", None) or request_id_var.get()

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.status_code, message, _rid(request)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        fields = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body")
            # `msg` only -- never `input`, which would echo user data back.
            fields.append({"field": loc or "body", "message": e.get("msg", "invalid")})
        summary = "; ".join(f"{f['field']}: {f['message']}" for f in fields[:5]) or "Invalid request"
        return JSONResponse(
            status_code=422,
            content=error_body(422, f"Validation failed ({summary})", _rid(request), fields=fields),
        )


def install(app: FastAPI, *, max_body_bytes: int, is_prod: bool, hsts_max_age: int, csp: str) -> None:
    register_error_handlers(app)
    app.add_middleware(
        HardeningMiddleware, max_body_bytes=max_body_bytes, is_prod=is_prod, hsts_max_age=hsts_max_age, csp=csp
    )


__all__ = ["HardeningMiddleware", "BodyTooLarge", "error_body", "install", "register_error_handlers"]

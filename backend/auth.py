"""Clerk session-token verification (Phase 10 rewrite).

What is verified on every token (PyJWT, RS256 ONLY -- `alg: none` and HS256
"key confusion" tokens are rejected before any key lookup):
  - signature, against the key whose `kid` the token names;
  - `exp` and `nbf` (required `exp`), with CLERK_LEEWAY_SECONDS of clock-skew
    tolerance;
  - `iss` == CLERK_ISSUER;
  - `aud` == CLERK_AUDIENCE, only when that is configured (Clerk's default
    session tokens carry no `aud`; setting it for a custom JWT template
    makes it mandatory);
  - `azp` (authorized party) in CLERK_AUTHORIZED_PARTIES, only when that is
    configured -- Clerk's recommended CSRF-style check that the token was
    minted for OUR frontend origin;
  - `sub` present.

JWKS handling: keys are cached for CLERK_JWKS_CACHE_SECONDS. An unknown
`kid` triggers ONE refresh (Clerk key rotation), but refreshes are
rate-limited to one per CLERK_JWKS_REFRESH_MIN_INTERVAL_SECONDS so an
attacker spraying forged `kid`s can't turn this API into a Clerk-hammering
proxy. The JWKS fetch has an explicit timeout.

Status semantics: a missing/malformed/expired/forged token is 401 (with
`WWW-Authenticate: Bearer`); an authenticated caller who lacks a role is 403
(see `require_admin`). The 401 body is a generic message -- the specific
reason is logged server-side only, never returned to the caller.

When CLERK_JWKS_URL/CLERK_ISSUER are unset the API runs in DEMO auth mode
(every caller is `demo-user`). `settings.py` refuses to start in prod in
that state."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

import jwt
import requests
from fastapi import Header, HTTPException, Request

logger = logging.getLogger("auth")

DEMO_USER_ID = "demo-user"
JWKS_FETCH_TIMEOUT_SECONDS = 5


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _cfg() -> Dict[str, Any]:
    """Read at call time (not import time) so tests/ops can change env and
    so a settings reload takes effect without re-importing this module."""
    return {
        "jwks_url": os.environ.get("CLERK_JWKS_URL") or None,
        "issuer": os.environ.get("CLERK_ISSUER") or None,
        "audience": os.environ.get("CLERK_AUDIENCE") or None,
        "azp": {p.strip() for p in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",") if p.strip()},
        "leeway": _env_int("CLERK_LEEWAY_SECONDS", 10),
        "cache_seconds": _env_int("CLERK_JWKS_CACHE_SECONDS", 3600),
        "refresh_min_interval": _env_int("CLERK_JWKS_REFRESH_MIN_INTERVAL_SECONDS", 30),
    }


def auth_configured() -> bool:
    c = _cfg()
    return bool(c["jwks_url"] and c["issuer"])


class JWKSCache:
    """Caches a JWKS document's keys by `kid`. `fetch` is injectable so
    tests never touch the network."""

    def __init__(self, fetch: Optional[Callable[[str], dict]] = None):
        self._fetch = fetch or self._http_fetch
        self._keys: Dict[str, Any] = {}
        self._fetched_at = 0.0
        self._last_refresh_attempt = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _http_fetch(url: str) -> dict:
        resp = requests.get(url, timeout=JWKS_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()

    def _load(self, url: str) -> None:
        doc = self._fetch(url)
        keys = {}
        for jwk in doc.get("keys", []):
            kid = jwk.get("kid")
            if not kid or jwk.get("kty") != "RSA":
                continue
            keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        if not keys:
            raise ValueError("JWKS contained no usable RSA keys")
        self._keys = keys
        self._fetched_at = time.time()

    def get_key(self, kid: str, url: str, cache_seconds: int, refresh_min_interval: int):
        with self._lock:
            now = time.time()
            stale = (now - self._fetched_at) > cache_seconds
            if not self._keys or stale:
                self._last_refresh_attempt = now
                try:
                    self._load(url)
                except Exception:
                    if not self._keys:
                        raise
                    logger.warning("JWKS refresh failed; serving cached keys")
            key = self._keys.get(kid)
            if key is None and (now - self._last_refresh_attempt) >= refresh_min_interval:
                # Unknown kid: maybe Clerk rotated keys. One rate-limited refresh.
                self._last_refresh_attempt = now
                try:
                    self._load(url)
                except Exception:
                    logger.warning("JWKS refresh-on-kid-miss failed")
                key = self._keys.get(kid)
            if key is None:
                raise KeyError("unknown signing key id")
            return key

    def reset(self) -> None:
        with self._lock:
            self._keys = {}
            self._fetched_at = 0.0
            self._last_refresh_attempt = 0.0


_jwks = JWKSCache()


def _unauthorized(reason: str) -> HTTPException:
    # Reason is for the server log only; the caller gets a generic message.
    logger.info("auth rejected: %s", reason)
    return HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})


def verify_token(token: str) -> Dict[str, Any]:
    """Returns the verified claims or raises HTTPException(401)."""
    c = _cfg()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _unauthorized("malformed token") from None
    if header.get("alg") != "RS256":
        raise _unauthorized("unexpected alg")
    kid = header.get("kid")
    if not kid:
        raise _unauthorized("no kid")

    try:
        key = _jwks.get_key(kid, c["jwks_url"], c["cache_seconds"], c["refresh_min_interval"])
    except KeyError:
        raise _unauthorized("unknown kid") from None
    except Exception:
        # JWKS unreachable AND nothing cached: we cannot verify -> fail closed.
        logger.exception("JWKS unavailable")
        raise HTTPException(status_code=503, detail="Authentication service unavailable") from None

    options = {"require": ["exp", "sub"], "verify_aud": bool(c["audience"])}
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=c["issuer"],
            audience=c["audience"],
            leeway=c["leeway"],
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("expired") from None
    except jwt.InvalidTokenError as e:
        raise _unauthorized(f"invalid: {type(e).__name__}") from None

    if c["azp"] and claims.get("azp") not in c["azp"]:
        raise _unauthorized("azp not authorized")
    return claims


def resolve_user_from_token(token: Optional[str], request: Optional[Request] = None) -> str:
    """Core verification, independent of where the token came from -- a
    'Bearer <token>' Authorization header (the normal case) or a `token`
    query param (SSE/EventSource can't set custom headers; see api.py's
    run_events). Returns DEMO_USER_ID in demo-auth mode. Stores the verified
    claims on `request.state.claims` when a request is supplied."""
    if not auth_configured():
        return DEMO_USER_ID
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    claims = verify_token(token)
    if request is not None:
        request.state.claims = claims
    return claims["sub"]


def get_current_user(request: Request, authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: the calling Clerk user id from a
    'Bearer <session token>' header (the SSE route has its own variant)."""
    if not auth_configured():
        return DEMO_USER_ID
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            token = value.strip()
    return resolve_user_from_token(token, request)


def is_admin(user_id: str, claims: Optional[Dict[str, Any]] = None) -> bool:
    """Admin if the Clerk user id is in ADMIN_USER_IDS, OR the verified
    token carries the admin role in the claim named by ADMIN_ROLE_CLAIM
    (default `role`; a dotted path such as `metadata.role` is supported --
    expose it via a Clerk session-token customization). A claim is only
    trusted because the token's signature was already verified."""
    ids = {a.strip() for a in os.environ.get("ADMIN_USER_IDS", "").split(",") if a.strip()}
    if user_id in ids:
        return True
    if not claims:
        return False
    node: Any = claims
    for part in os.environ.get("ADMIN_ROLE_CLAIM", "role").split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, str):
        return node.lower() == "admin"
    if isinstance(node, list):
        return "admin" in [str(x).lower() for x in node]
    return False


def require_admin(user_id: str, request: Optional[Request] = None) -> None:
    """403 (not 401): the caller IS authenticated, just not permitted."""
    claims = getattr(getattr(request, "state", None), "claims", None) if request is not None else None
    if not is_admin(user_id, claims):
        raise HTTPException(status_code=403, detail="Admin access required")

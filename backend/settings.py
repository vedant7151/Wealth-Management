"""Centralized, validated settings (Phase 10).

One typed object describes every environment variable the API reads, so a
bad/missing value is caught at startup with a message that lists the
variable NAMES (never their values -- a secret must not end up in a crash
log) instead of surfacing as an obscure failure on the first request.

Two profiles, selected by `APP_ENV`:
  - `dev`  (default): permissive. Demo auth, CORS `*`, `DEBUG` are all
    allowed, provider keys are optional (demo mode covers their absence).
  - `prod`: the process REFUSES TO START with CORS `*`, demo auth (Clerk
    unconfigured), `DEBUG` on, a non-HTTPS non-localhost CORS origin, no
    `DATABASE_URL`, or `/metrics` enabled without a token.

Honest scope note: older modules (`ratelimit.py`, `llm_client.py`,
`runs.py`, ...) still read their own env vars at import time with their own
defaults. This module validates the SAME variables up front (types, ranges)
so a typo is caught here rather than silently falling back to a default in
those modules; it is the source of truth for everything added in Phase 10.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class SettingsError(Exception):
    """Raised with a human-readable list of offending variable NAMES (and
    why) -- never the values."""

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", env_file=None)

    # --- profile ---------------------------------------------------------
    app_env: str = "dev"
    debug: bool = False

    # --- core ------------------------------------------------------------
    database_url: Optional[str] = None
    mock_db: str = ""
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"
    log_include_client_names: bool = False
    log_max_message_chars: int = Field(2000, ge=100, le=100_000)

    # --- auth (Clerk) ------------------------------------------------------
    clerk_jwks_url: Optional[str] = None
    clerk_issuer: Optional[str] = None
    clerk_audience: Optional[str] = None
    clerk_authorized_parties: str = ""
    clerk_leeway_seconds: int = Field(10, ge=0, le=300)
    clerk_jwks_cache_seconds: int = Field(3600, ge=30, le=86_400)
    clerk_jwks_refresh_min_interval_seconds: int = Field(30, ge=1, le=3600)
    admin_user_ids: str = ""
    admin_role_claim: str = "role"

    # --- provider keys (required in prod unless demo mode) -----------------
    gemini_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    huggingface_api_key: Optional[str] = None
    demo_mode: str = "auto"
    embedding_provider: str = "gemini"

    # --- HTTP hardening ----------------------------------------------------
    max_request_body_bytes: int = Field(1_048_576, ge=1024, le=64 * 1024 * 1024)
    hsts_max_age_seconds: int = Field(31_536_000, ge=0)
    csp_api: str = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    max_page_size: int = Field(100, ge=1, le=1000)
    max_themes: int = Field(10, ge=1, le=100)
    max_theme_chars: int = Field(120, ge=8, le=1000)
    max_name_chars: int = Field(100, ge=4, le=500)
    max_briefing_chars: int = Field(50_000, ge=1000, le=1_000_000)
    max_clients_per_user: int = Field(50, ge=1, le=10_000)

    # --- data --------------------------------------------------------------
    db_pool_size: int = Field(5, ge=1, le=100)
    db_pool_timeout_seconds: int = Field(10, ge=1, le=300)
    db_connect_timeout_seconds: int = Field(10, ge=1, le=300)
    db_statement_timeout_ms: int = Field(5000, ge=100, le=600_000)
    retention_enabled: bool = False
    retention_chunk_days: int = Field(14, ge=1, le=3650)
    retention_search_cache_days: int = Field(7, ge=1, le=3650)
    retention_api_call_log_days: int = Field(90, ge=1, le=3650)
    retention_rate_limit_hit_days: int = Field(2, ge=1, le=3650)

    # --- reliability / limits ----------------------------------------------
    max_concurrent_runs: int = Field(4, ge=1, le=64)
    llm_max_concurrency: int = Field(4, ge=1, le=64)
    run_limit: int = Field(3, ge=1)
    run_window_seconds: int = Field(3600, ge=1)
    global_api_limit: int = Field(120, ge=1)
    write_limit: int = Field(20, ge=1)
    daily_llm_budget: int = Field(18, ge=1)
    run_timeout_seconds: int = Field(600, ge=10)
    graceful_shutdown_timeout_seconds: int = Field(25, ge=1, le=600)
    llm_call_timeout_seconds: float = Field(30, gt=0, le=600)

    # --- observability -----------------------------------------------------
    metrics_enabled: bool = True
    metrics_token: Optional[str] = None
    sentry_dsn: Optional[str] = None
    sentry_traces_sample_rate: float = Field(0.0, ge=0.0, le=1.0)
    langchain_project: Optional[str] = None
    langsmith_project_suffix_env: bool = True
    # Hide prompt/completion bodies from LangSmith traces (ids, timings and
    # token counts stay visible). None = decide by profile: ON in prod, OFF in dev.
    trace_hide_payloads: Optional[bool] = None

    # ----------------------------------------------------------------------
    @field_validator("app_env")
    @classmethod
    def _check_env(cls, v: str) -> str:
        v = {"production": "prod", "development": "dev"}.get(v.strip().lower(), v.strip().lower())
        if v not in {"dev", "prod"}:
            raise ValueError("must be 'dev' or 'prod' (aliases: production, development)")
        return v

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _LOG_LEVELS:
            raise ValueError(f"must be one of {sorted(_LOG_LEVELS)}")
        return v

    @field_validator("demo_mode")
    @classmethod
    def _check_demo(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"true", "false", "auto"}:
            raise ValueError("must be 'true', 'false' or 'auto'")
        return v

    @field_validator("database_url", "clerk_jwks_url", "clerk_issuer", "metrics_token", "sentry_dsn", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("clerk_jwks_url", "clerk_issuer")
    @classmethod
    def _check_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return v

    # ----------------------------------------------------------------------
    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_configured(self) -> bool:
        return bool(self.clerk_jwks_url and self.clerk_issuer)

    @property
    def admin_ids(self) -> set:
        return {a.strip() for a in self.admin_user_ids.split(",") if a.strip()}

    @property
    def authorized_parties(self) -> set:
        return {a.strip() for a in self.clerk_authorized_parties.split(",") if a.strip()}

    @property
    def hide_trace_payloads(self) -> bool:
        return self.is_prod if self.trace_hide_payloads is None else self.trace_hide_payloads

    @property
    def langsmith_project(self) -> str:
        """Per-environment LangSmith project so dev traces never mix with
        prod ones: `<base>-<env>` (base defaults to `wealth-agent`)."""
        base = (self.langchain_project or "wealth-agent").strip()
        if self.langsmith_project_suffix_env and not base.endswith(f"-{self.app_env}"):
            return f"{base}-{self.app_env}"
        return base

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "Settings":
        problems: List[str] = []
        mock = self.mock_db == "1"

        if not self.database_url and not mock:
            problems.append("DATABASE_URL: required (unless MOCK_DB=1)")
        if self.auth_configured is False and (self.clerk_jwks_url or self.clerk_issuer):
            problems.append("CLERK_JWKS_URL/CLERK_ISSUER: set both or neither (one alone leaves auth half-configured)")

        if self.is_prod:
            if "*" in self.cors_origin_list:
                problems.append("CORS_ORIGINS: '*' is not allowed when APP_ENV=prod")
            if not self.cors_origin_list:
                problems.append("CORS_ORIGINS: at least one explicit origin is required when APP_ENV=prod")
            for origin in self.cors_origin_list:
                parsed = urlparse(origin)
                if origin != "*" and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
                    problems.append("CORS_ORIGINS: every non-localhost origin must be https when APP_ENV=prod")
                    break
            if not self.auth_configured:
                problems.append(
                    "CLERK_JWKS_URL/CLERK_ISSUER: required when APP_ENV=prod (unset means demo auth, which is "
                    "refused in prod)"
                )
            if self.debug:
                problems.append("DEBUG: must be off when APP_ENV=prod")
            if not self.admin_ids:
                problems.append(
                    "ADMIN_USER_IDS: required when APP_ENV=prod (at least one Clerk user id; no admin = no usage panel)"
                )
            if self.metrics_enabled and not self.metrics_token:
                problems.append("METRICS_TOKEN: required when APP_ENV=prod and METRICS_ENABLED is on")
            if self.demo_mode != "true":
                needed = ["gemini_api_key", "tavily_api_key", "groq_api_key"]
                if self.embedding_provider.strip().lower() == "huggingface":
                    needed.append("huggingface_api_key")  # only the HF embedding provider needs it
                for name in needed:
                    if not getattr(self, name):
                        problems.append(f"{name.upper()}: required when APP_ENV=prod and DEMO_MODE is not 'true'")
            if self.log_level == "DEBUG":
                problems.append("LOG_LEVEL: DEBUG is not allowed when APP_ENV=prod (may log request detail)")

        if problems:
            raise SettingsError(problems)
        return self


class _IsolatedSettings(Settings):
    """Reads ONLY explicit kwargs (never os.environ) -- for tests."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
        return (init_settings,)


def load_settings(env: Optional[dict] = None) -> Settings:
    """Builds and validates Settings from the environment (or `env`, for
    tests). Raises SettingsError listing offending variable names only."""
    try:
        if env is None:
            return Settings()
        # Explicit mapping (tests): the real process env must not leak in.
        return _IsolatedSettings(**{k.lower(): v for k, v in env.items()})
    except SettingsError:
        raise
    except ValidationError as e:
        # model_validator raising SettingsError is wrapped by pydantic into
        # ValidationError; unwrap our own, format the rest.
        problems: List[str] = []
        for err in e.errors():
            ctx = err.get("ctx") or {}
            original = ctx.get("error")
            if isinstance(original, SettingsError):
                problems.extend(original.problems)
            else:
                loc = ".".join(str(p) for p in err.get("loc", ())) or "settings"
                problems.append(f"{loc.upper()}: {err.get('msg', 'invalid')}")
        raise SettingsError(problems) from None


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None


def load_or_exit() -> Settings:
    """Fail-fast entry point for process startup: prints the problem list
    (names only) to stderr and exits with EX_CONFIG (78)."""
    try:
        return get_settings()
    except SettingsError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(78)


def apply_runtime_env(s: Settings) -> None:
    """Pushes derived values other libraries read from os.environ (LangSmith
    project) -- called once at startup."""
    os.environ["LANGCHAIN_PROJECT"] = s.langsmith_project
    if s.hide_trace_payloads:
        # LangSmith's client reads these (either prefix) when it is created.
        for prefix in ("LANGSMITH", "LANGCHAIN"):
            os.environ[f"{prefix}_HIDE_INPUTS"] = "true"
            os.environ[f"{prefix}_HIDE_OUTPUTS"] = "true"


# --------------------------------------------------------------------------
# Database connection tuning
# --------------------------------------------------------------------------


def database_url_with_pool(url: str, pool_size: int, pool_timeout: int, connect_timeout: int) -> str:
    """Adds Prisma's connection-pool query params to a Postgres URL unless
    the operator already set them explicitly. A Neon *pooled* endpoint
    (host contains '-pooler', i.e. PgBouncer in transaction mode) also needs
    `pgbouncer=true` so Prisma doesn't use prepared statements there."""
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    wanted = {
        "connection_limit": str(pool_size),
        "pool_timeout": str(pool_timeout),
        "connect_timeout": str(connect_timeout),
    }
    if parsed.hostname and "-pooler" in parsed.hostname:
        wanted["pgbouncer"] = "true"
    for k, v in wanted.items():
        existing.setdefault(k, v)
    return urlunparse(parsed._replace(query=urlencode(existing)))


def make_prisma():
    """The ONE place a Prisma client is constructed with pool sizing and
    timeouts applied (api.py, agent.get_db, scheduler.py all use it)."""
    from prisma import Prisma

    s = get_settings()
    if not s.database_url:
        return Prisma()
    return Prisma(
        datasource={
            "url": database_url_with_pool(
                s.database_url, s.db_pool_size, s.db_pool_timeout_seconds, s.db_connect_timeout_seconds
            )
        },
        connect_timeout=s.db_connect_timeout_seconds,
        http={"timeout": float(s.db_pool_timeout_seconds + s.db_statement_timeout_ms / 1000 + 5)},
    )


if __name__ == "__main__":
    # `python settings.py` -- used by docker-entrypoint.sh to fail fast
    # BEFORE uvicorn starts, with the problem list only (no values).
    load_or_exit()
    print("Configuration OK")

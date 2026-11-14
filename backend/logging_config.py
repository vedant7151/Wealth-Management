"""Structured (JSON) logging for the API and the agent graph.

Every log line carries run_id/client_id/request_id/node so a single run's or
request's lines can be filtered out of concurrent traffic in production.
Configure once at process startup (api.py, scheduler.py, agent.py's __main__
block each call this).

Phase 10 adds REDACTION. Everything that reaches the formatter -- message
text and exception tracebacks -- is scrubbed of: bearer tokens and JWTs,
provider API keys (Google `AIza...`, Groq `gsk_...`, Tavily `tvly-...`,
HuggingFace `hf_...`, OpenAI-style `sk-...`), `token=`/`api_key=` query
params, and the password in a `scheme://user:password@host` URL. Messages
longer than LOG_MAX_MESSAGE_CHARS are truncated (a full prompt or briefing
must not be dumped into logs). Client names are never added by this module;
`client_name`-style fields are dropped unless LOG_INCLUDE_CLIENT_NAMES=true.

Redaction is pattern-based defense in depth, NOT a guarantee: the primary
control is not logging secrets/prompts in the first place."""

import contextvars
import json
import logging
import os
import re
from typing import Optional

# Set by the request-id middleware for the lifetime of a request; background
# threads copy it explicitly (runs.py passes request_id through).
request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)

_REDACTIONS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgsk_[0-9A-Za-z]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\btvly-[0-9A-Za-z_-]{8,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bhf_[0-9A-Za-z]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bsk-[0-9A-Za-z_-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)([?&](?:token|api_key|apikey|access_token|key)=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:authorization|x-api-key)\"?\s*[:=]\s*\"?)[^\"\s,}]+"), r"\1[REDACTED]"),
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+:)[^@/\s]+(@)"), r"\1[REDACTED]\2"),
]

_DROP_UNLESS_FLAG = ("client_name", "clientName")


def redact(text: str, max_chars: Optional[int] = None) -> str:
    if not isinstance(text, str):
        text = str(text)
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"...[TRUNCATED {len(text) - max_chars} chars]"
    return text


def _max_chars() -> int:
    try:
        return int(os.environ.get("LOG_MAX_MESSAGE_CHARS", "2000"))
    except ValueError:
        return 2000


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        limit = _max_chars()
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage(), limit),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
            "run_id": getattr(record, "run_id", None),
            "client_id": getattr(record, "client_id", None),
            "node": getattr(record, "node", None),
        }
        if os.environ.get("LOG_INCLUDE_CLIENT_NAMES", "").lower() == "true":
            for field in _DROP_UNLESS_FLAG:
                if getattr(record, field, None):
                    payload[field] = redact(str(getattr(record, field)), 200)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info), limit * 2)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def get_node_logger(state: dict, node_name: str) -> logging.LoggerAdapter:
    """Returns a LoggerAdapter that stamps every record with this run's
    run_id/client_id/request_id and the current node name, for the JSON
    formatter above to pick up."""
    base = logging.getLogger("agent")
    return logging.LoggerAdapter(
        base,
        {
            "run_id": state.get("run_id"),
            "client_id": state.get("client_id"),
            "request_id": state.get("request_id"),
            "node": node_name,
        },
    )

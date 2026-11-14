"""Compliance engine: deterministic rules + an instruction-following LLM
judge + a (separately purposed) prompt-injection screen for retrieved
content. See rules.py, judge.py, injection_guard.py.

AuditLog writing lives in backend/audit.py, not in this package, because
it's used by both the injection guard (chunk quarantine events) and
anything outside compliance that wants an audit trail (e.g. Phase 7's
approval workflow) -- it's a cross-cutting concern, not compliance-specific
logic."""

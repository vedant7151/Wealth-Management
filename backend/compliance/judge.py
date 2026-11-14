"""Instruction-following LLM judge for the compliance engine.

Goes through backend/llm_client.py's generate_json (Gemini -> Groq
fallback, retries, circuit breaker, ApiCallLog) rather than calling a
provider SDK directly -- see llm_client.py's module docstring for why
every provider call in this codebase is routed through it.

Deterministic rules (rules.py) ALWAYS run first and can veto an "approved"
verdict from this module -- that veto logic lives in agent.py's
compliance_critic_node, not here. This module only reports what the judge
itself thinks.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import llm_client

_PROMPT_TEMPLATE = """You are a strict SEC/FINRA compliance reviewer for an automated wealth-advisory research digest. Assess the following draft morning briefing for:
- directive buy/sell/hold recommendations or any personalized "you should" advice
- guaranteed/promissory performance claims
- specific price targets
- one-sided claims with no risk framing
- a missing compliance disclaimer

Respond with STRICTLY this JSON shape and nothing else (no markdown fences, no commentary):
{{"compliant": <true|false>, "violations": [{{"rule": "<short-id>", "quote": "<exact offending text>", "why": "<one sentence>", "fix": "<one actionable rewrite instruction>"}}], "confidence": <0.0-1.0>}}

If there are no violations, return an empty "violations" array and "compliant": true.

Draft:
{draft}
"""


@dataclass
class JudgeVerdict:
    compliant: bool
    violations: List[dict] = field(default_factory=list)
    confidence: float = 0.0
    model: Optional[str] = None
    # None on success; "all_providers_unavailable" (every provider in the
    # fallback chain failed) or "malformed_response" (a provider responded
    # but never returned valid/well-shaped JSON after its retries) --
    # these are llm_client.GenerationResult's own error vocabulary, passed
    # through unchanged.
    # agent.py's compliance_critic_node treats either as fail-safe:
    # needs_human_review, never compliant.
    error: Optional[str] = None


def _validate_shape(parsed) -> Optional[dict]:
    """Returns a normalized {compliant, violations, confidence} dict if
    `parsed` has the required shape, else None (malformed) -- passed to
    llm_client.generate_json as its `validate` callback. Returning the
    full normalized dict (not just the violations list) lets
    generate_json's `.parsed` carry everything call_judge needs."""
    if not isinstance(parsed, dict) or "compliant" not in parsed:
        return None
    if not isinstance(parsed.get("violations", []), list):
        return None
    violations = []
    for v in parsed.get("violations", []):
        if not isinstance(v, dict):
            return None
        violations.append(
            {
                "rule": str(v.get("rule", "judge")),
                "quote": str(v.get("quote", "")),
                "why": str(v.get("why", "")),
                "fix": str(v.get("fix", "")),
            }
        )
    return {
        "compliant": bool(parsed.get("compliant", False)),
        "violations": violations,
        "confidence": parsed.get("confidence", 0.5),
    }


def call_judge(draft: str, run_id: Optional[str] = None) -> JudgeVerdict:
    """Calls the judge (via llm_client's provider fallback chain + retry),
    and fails safe (never `compliant`) if nothing ever produces a
    valid/well-shaped response."""
    result = llm_client.generate_json(
        prompt=_PROMPT_TEMPLATE.format(draft=draft),
        purpose="compliance_judge",
        run_id=run_id,
        validate=_validate_shape,
    )

    if result.error:
        return JudgeVerdict(compliant=False, violations=[], confidence=0.0, model=result.model, error=result.error)

    normalized = result.parsed
    violations = normalized.get("violations", [])
    confidence = normalized.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    return JudgeVerdict(
        compliant=bool(normalized.get("compliant", False)) and not violations,
        violations=violations,
        confidence=confidence,
        model=result.model,
    )

"""Tests for compliance/judge.py. No live API calls -- llm_client's own
generate_json is monkeypatched, since that module (and its real provider
fallback/retry mechanics) has its own dedicated test_llm_client.py. This
file verifies judge.py's OWN responsibilities: building the prompt, shape
validation, the compliant-but-violations self-contradiction check,
confidence clamping, and mapping llm_client's error kinds through
unchanged."""

import llm_client
from compliance import judge


def _patch_generate_json(monkeypatch, result: llm_client.GenerationResult):
    def _fake_generate_json(prompt, purpose, run_id=None, provider_order=None, validate=None):
        if result.parsed is not None and validate is not None:
            # Mirror real generate_json: run the caller's validate() so
            # judge.py's own _validate_shape is actually exercised too.
            normalized = validate(result.parsed)
            return llm_client.GenerationResult(
                text=result.text,
                parsed=normalized,
                provider=result.provider,
                model=result.model,
                error=None if normalized is not None else "malformed_response",
            )
        return result

    monkeypatch.setattr(llm_client, "generate_json", _fake_generate_json)


def test_valid_response_is_compliant(monkeypatch):
    parsed = {"compliant": True, "violations": [], "confidence": 0.9}
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("A clean draft.")

    assert verdict.compliant is True
    assert verdict.violations == []
    assert verdict.confidence == 0.9
    assert verdict.error is None
    assert verdict.model == "gemini-3.6-flash"


def test_valid_response_with_violations_is_not_compliant(monkeypatch):
    parsed = {
        "compliant": False,
        "violations": [{"rule": "x", "quote": "buy now", "why": "directive", "fix": "soften"}],
        "confidence": 0.8,
    }
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("Buy now.")

    assert verdict.compliant is False
    assert len(verdict.violations) == 1
    assert verdict.violations[0]["rule"] == "x"


def test_judge_says_compliant_but_lists_violations_is_self_contradiction_and_flips_false(monkeypatch):
    parsed = {"compliant": True, "violations": [{"rule": "x", "quote": "q", "why": "w", "fix": "f"}], "confidence": 0.5}
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("draft")

    assert verdict.compliant is False


def test_all_providers_unavailable_fails_safe(monkeypatch):
    _patch_generate_json(monkeypatch, llm_client.GenerationResult(error="all_providers_unavailable"))

    verdict = judge.call_judge("draft")

    assert verdict.compliant is False
    assert verdict.error == "all_providers_unavailable"


def test_malformed_response_fails_safe(monkeypatch):
    _patch_generate_json(monkeypatch, llm_client.GenerationResult(error="malformed_response"))

    verdict = judge.call_judge("draft")

    assert verdict.compliant is False
    assert verdict.error == "malformed_response"


def test_validate_shape_rejects_missing_compliant_key(monkeypatch):
    parsed = {"violations": []}  # missing "compliant"
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("draft")

    assert verdict.error == "malformed_response"


def test_validate_shape_rejects_non_dict_violation_entries(monkeypatch):
    parsed = {"compliant": False, "violations": ["not a dict"]}
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("draft")

    assert verdict.error == "malformed_response"


def test_confidence_is_clamped_to_0_1(monkeypatch):
    parsed = {"compliant": True, "violations": [], "confidence": 5.0}
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("draft")

    assert verdict.confidence == 1.0


def test_confidence_defaults_when_missing_or_invalid(monkeypatch):
    parsed = {"compliant": True, "violations": [], "confidence": "not-a-number"}
    _patch_generate_json(
        monkeypatch, llm_client.GenerationResult(parsed=parsed, provider="gemini", model="gemini-3.6-flash")
    )

    verdict = judge.call_judge("draft")

    assert verdict.confidence == 0.5


def test_call_judge_passes_run_id_through(monkeypatch):
    captured = {}

    def _fake_generate_json(prompt, purpose, run_id=None, provider_order=None, validate=None):
        captured["run_id"] = run_id
        captured["purpose"] = purpose
        return llm_client.GenerationResult(
            parsed={"compliant": True, "violations": []}, provider="gemini", model="gemini-3.6-flash"
        )

    monkeypatch.setattr(llm_client, "generate_json", _fake_generate_json)

    judge.call_judge("draft", run_id="run-123")

    assert captured["run_id"] == "run-123"
    assert captured["purpose"] == "compliance_judge"

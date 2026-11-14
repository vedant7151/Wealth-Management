"""Tests for agent.py's compliance_critic_node + supervisor_node status
transitions and the revision loop's termination. Runs with MOCK_DB=1 (set
by conftest.py) and monkeypatches compliance_judge.call_judge /
compliance_rules.check_text so no live Gemini/Groq calls happen."""

import os

os.environ.setdefault("MOCK_DB", "1")

import agent  # noqa: E402
from compliance import judge as compliance_judge  # noqa: E402
from compliance import rules as compliance_rules  # noqa: E402


def _base_state(**overrides):
    state = agent.new_input_state("client-1", ["AI infrastructure"], run_id="run-1")
    state.update(overrides)
    return state


def test_critic_veto_rules_win_even_if_judge_says_compliant(monkeypatch):
    monkeypatch.setattr(
        compliance_judge,
        "call_judge",
        lambda draft, run_id=None: compliance_judge.JudgeVerdict(
            compliant=True, violations=[], confidence=0.9, model="fake"
        ),
    )
    monkeypatch.setattr(agent, "compliance_judge", compliance_judge)

    state = _base_state(final_briefing="This is a buy. You should buy now.", revision_count=1)
    result = agent.compliance_critic_node(state)

    assert result["is_compliant"] is False
    assert result["judge_unavailable"] is False
    assert any(v["rule"] == "DIRECTIVE_ADVICE" for v in result["compliance_violations"])
    assert "DIRECTIVE_ADVICE" in result["criticism"]


def test_critic_compliant_when_both_rules_and_judge_agree(monkeypatch):
    monkeypatch.setattr(
        compliance_judge,
        "call_judge",
        lambda draft, run_id=None: compliance_judge.JudgeVerdict(
            compliant=True, violations=[], confidence=0.9, model="fake"
        ),
    )

    clean_text = (
        "The sector may grow, though this carries risk and is not guaranteed. "
        "For informational purposes only, not financial advice."
    )
    state = _base_state(final_briefing=clean_text, revision_count=1)
    result = agent.compliance_critic_node(state)

    assert result["is_compliant"] is True
    assert result["compliance_violations"] == []


def test_critic_judge_unavailable_fails_safe_never_compliant(monkeypatch):
    monkeypatch.setattr(
        compliance_judge,
        "call_judge",
        lambda draft, run_id=None: compliance_judge.JudgeVerdict(
            compliant=False, violations=[], confidence=0.0, model="fake", error="judge_unavailable"
        ),
    )

    clean_text = "The sector may grow, though this carries risk. Not financial advice."
    state = _base_state(final_briefing=clean_text, revision_count=1)
    result = agent.compliance_critic_node(state)

    assert result["is_compliant"] is False
    assert result["judge_unavailable"] is True
    assert "human review" in result["criticism"].lower() or result["criticism"] != ""


def test_critic_appends_to_revision_history(monkeypatch):
    monkeypatch.setattr(
        compliance_judge,
        "call_judge",
        lambda draft, run_id=None: compliance_judge.JudgeVerdict(
            compliant=True, violations=[], confidence=0.9, model="fake"
        ),
    )

    state = _base_state(
        final_briefing="This is a buy.",
        revision_count=1,
        revision_history=[
            {
                "revision_index": 0,
                "content": "prior draft",
                "violations": [],
                "critic_model": "rules+fake",
                "is_compliant": False,
            }
        ],
    )
    result = agent.compliance_critic_node(state)

    assert len(result["revision_history"]) == 2
    assert result["revision_history"][0]["content"] == "prior draft"
    assert result["revision_history"][1]["revision_index"] == 1


def test_supervisor_routes_write_failed_straight_to_save():
    state = _base_state(
        research_done=True,
        retrieval_attempted=True,
        final_briefing="placeholder",
        write_failed=True,
        needs_critique=False,
    )
    result = agent.supervisor_node(state)
    assert result["next"] == "save"


def test_supervisor_routes_needs_critique_to_critic():
    state = _base_state(research_done=True, retrieval_attempted=True, final_briefing="draft text", needs_critique=True)
    result = agent.supervisor_node(state)
    assert result["next"] == "compliance_critic"


def test_supervisor_judge_unavailable_finishes_with_needs_human_review():
    state = _base_state(
        research_done=True,
        retrieval_attempted=True,
        final_briefing="draft text",
        needs_critique=False,
        judge_unavailable=True,
    )
    result = agent.supervisor_node(state)
    assert result["next"] == "save"
    assert result["briefing_status"] == "needs_human_review"


def test_supervisor_compliant_finishes_with_compliant_status():
    state = _base_state(
        research_done=True,
        retrieval_attempted=True,
        final_briefing="draft text",
        needs_critique=False,
        is_compliant=True,
    )
    result = agent.supervisor_node(state)
    assert result["next"] == "save"
    assert result["briefing_status"] == "compliant"


def test_supervisor_requests_revision_below_max():
    state = _base_state(
        research_done=True,
        retrieval_attempted=True,
        final_briefing="draft text",
        needs_critique=False,
        is_compliant=False,
        criticism="fix this",
        revision_count=1,
    )
    result = agent.supervisor_node(state)
    assert result["next"] == "writer"


def test_supervisor_max_revisions_hits_needs_human_review_never_compliant():
    state = _base_state(
        research_done=True,
        retrieval_attempted=True,
        final_briefing="draft text",
        needs_critique=False,
        is_compliant=False,
        criticism="still has issues",
        revision_count=agent.MAX_REVISIONS,
    )
    result = agent.supervisor_node(state)
    assert result["next"] == "save"
    assert result["briefing_status"] == "needs_human_review"


def test_full_revision_loop_terminates_within_max_revisions(monkeypatch):
    """Drives writer -> critic -> supervisor repeatedly with an always-non-
    compliant judge/rules verdict and asserts the loop terminates at
    save with needs_human_review, rather than looping forever."""
    monkeypatch.setattr(agent, "compliance_rules", compliance_rules)
    monkeypatch.setattr(
        compliance_judge,
        "call_judge",
        lambda draft, run_id=None: compliance_judge.JudgeVerdict(
            compliant=False,
            violations=[{"rule": "x", "quote": "q", "why": "w", "fix": "f"}],
            confidence=0.9,
            model="fake",
        ),
    )

    state = _base_state(
        research_done=True, retrieval_attempted=True, final_briefing="This is a buy.", needs_critique=True
    )

    steps = 0
    max_steps = 20
    while steps < max_steps:
        steps += 1
        decision = agent.supervisor_node(state)
        state.update(decision)
        if decision["next"] == "save":
            break
        if decision["next"] == "compliance_critic":
            state.update(agent.compliance_critic_node(state))
        elif decision["next"] == "writer":
            state["revision_count"] += 1
            state["needs_critique"] = True
            state["final_briefing"] = "This is a buy."
        else:
            raise AssertionError(f"unexpected routing target: {decision['next']}")

    assert steps < max_steps, "revision loop did not terminate"
    assert state["briefing_status"] == "needs_human_review"

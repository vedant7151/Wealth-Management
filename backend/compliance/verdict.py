"""Combined rules+judge compliance verdict -- the "rules veto judge" rule
that used to live inline in agent.py's compliance_critic_node, extracted
so Phase 7's edit-and-recheck endpoint (PATCH /api/briefings/{id}) can run
the exact same combined system over edited text without re-deriving it or
drifting out of sync with the graph's own critic node. See
docs/PROGRESS.md's Phase 4 handoff note, which flagged this exact
extraction as the "cleaner" option once something other than the graph
needed it.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from compliance import judge as compliance_judge
from compliance import rules as compliance_rules


@dataclass
class CombinedVerdict:
    compliant: bool
    violations: List[dict] = field(default_factory=list)
    judge_unavailable: bool = False
    criticism: str = ""
    critic_model: Optional[str] = None


def build_criticism(rule_violations: list, judge_violations: list) -> str:
    """Actionable feedback: every violation's `fix` hint verbatim, not a
    generic "be more compliant" string. Identical to agent.py's previous
    inline helper (moved here so both the graph and the edit-recheck
    endpoint produce the same feedback text for the same violations)."""
    lines = []
    for v in rule_violations:
        lines.append(f'[{v.rule_id}] "{v.quote}" -- {v.why} Suggested fix: {v.fix}')
    for jv in judge_violations:
        rule = jv.get("rule", "judge")
        quote = jv.get("quote", "")
        why = jv.get("why", "")
        fix = jv.get("fix", "")
        lines.append(f'[{rule}] "{quote}" -- {why} Suggested fix: {fix}')
    if not lines:
        return ""
    return "Compliance issues to address in the next revision:\n" + "\n".join(f"- {line}" for line in lines)


def evaluate(draft: str, run_id: Optional[str] = None) -> CombinedVerdict:
    """Runs the deterministic rules (always, no API call) and the LLM
    judge, and applies the same veto semantics as the graph's critic node:
    the rules can veto an "approved" judge verdict, and a judge
    failure/malformed response fails SAFE (never compliant)."""
    rule_violations = compliance_rules.check_text(draft)
    verdict = compliance_judge.call_judge(draft, run_id=run_id)

    rule_violation_dicts = [v.to_dict() for v in rule_violations]
    merged_violations = rule_violation_dicts + verdict.violations
    criticism = build_criticism(rule_violations, verdict.violations)

    if verdict.error:
        return CombinedVerdict(
            compliant=False,
            violations=merged_violations,
            judge_unavailable=True,
            criticism=criticism or "The compliance judge was unavailable; this requires human review.",
            critic_model=f"rules+{verdict.error}",
        )

    final_compliant = verdict.compliant and not rule_violations
    return CombinedVerdict(
        compliant=final_compliant,
        violations=merged_violations,
        judge_unavailable=False,
        criticism=criticism,
        critic_model=f"rules+{verdict.model or 'unknown'}",
    )

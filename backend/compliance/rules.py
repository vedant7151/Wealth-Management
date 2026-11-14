"""Deterministic compliance rules. These ALWAYS run (judge.py's LLM verdict
can never override them -- see agent.py's compliance_critic_node) and are
the first line of defense: no API call, no nondeterminism, no quota cost.

Each Rule either matches specific phrases (reports up to MAX_MATCHES_PER_RULE
offending spans) or checks for the ABSENCE of required framing across the
whole draft (reports a single document-level violation). Every violation
carries a `fix` hint that is fed back to the writer verbatim on the next
revision (see agent.py's build_criticism), not a generic "be more
compliant" message.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern, Protocol

MAX_MATCHES_PER_RULE = 3


@dataclass
class Violation:
    rule_id: str
    severity: str  # "high" | "medium" | "low"
    quote: str
    why: str
    fix: str
    span: Optional[tuple] = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule_id,
            "severity": self.severity,
            "quote": self.quote,
            "why": self.why,
            "fix": self.fix,
            "span": list(self.span) if self.span else None,
        }


@dataclass
class MatchRule:
    """Flags specific phrases anywhere in the text (directive advice,
    guarantees, price targets, ...)."""

    id: str
    severity: str
    patterns: List[Pattern]
    why: str
    fix: str

    def check(self, text: str) -> List[Violation]:
        violations: List[Violation] = []
        seen_spans = set()
        for pattern in self.patterns:
            for m in pattern.finditer(text):
                if len(violations) >= MAX_MATCHES_PER_RULE:
                    return violations
                if m.span() in seen_spans:
                    continue
                seen_spans.add(m.span())
                violations.append(
                    Violation(
                        rule_id=self.id,
                        severity=self.severity,
                        quote=m.group(0),
                        why=self.why,
                        fix=self.fix,
                        span=m.span(),
                    )
                )
        return violations


@dataclass
class AbsenceRule:
    """Flags the ABSENCE of required framing (a disclaimer, risk language)
    -- a single document-level violation, not a per-match one."""

    id: str
    severity: str
    required_any: List[Pattern]
    why: str
    fix: str

    def check(self, text: str) -> List[Violation]:
        for pattern in self.required_any:
            if pattern.search(text):
                return []
        excerpt = text.strip()[:120] + ("..." if len(text.strip()) > 120 else "")
        return [
            Violation(
                rule_id=self.id,
                severity=self.severity,
                quote=excerpt,
                why=self.why,
                fix=self.fix,
                span=None,
            )
        ]


@dataclass
class UnbalancedClaimRule:
    """Flags one-sided language: directional/definitive claims with zero
    hedging/risk language anywhere in the document. Distinct from
    DEFINITIVE_PERFORMANCE (which flags the directional phrase itself
    regardless of context) and MISSING_RISK_FRAMING (which only checks for
    risk vocabulary, not the presence of directional claims)."""

    id: str = "UNBALANCED_CLAIM"
    severity: str = "medium"
    directional: Pattern = field(
        default_factory=lambda: re.compile(
            r"\b(?:will|strongly|significantly|clearly|definitely|certainly)\s+\w*\s*(?:rise|fall|grow|decline|increase|decrease|outperform|underperform|rally|surge|drop)\b",
            re.IGNORECASE,
        )
    )
    hedge: Pattern = field(
        default_factory=lambda: re.compile(
            r"\b(?:may|might|could|potential(?:ly)?|historically|risk|uncertain|possib(?:le|ly))\b",
            re.IGNORECASE,
        )
    )
    why: str = "The draft makes a one-sided directional claim without any hedging or risk language anywhere in the document, which reads as a prediction rather than a balanced, compliant analysis."
    fix: str = 'Pair the directional statement with explicit hedging (e.g. "may", "could", "historically") and name at least one offsetting risk or uncertainty.'

    def check(self, text: str) -> List[Violation]:
        match = self.directional.search(text)
        if not match:
            return []
        if self.hedge.search(text):
            return []
        return [
            Violation(
                rule_id=self.id,
                severity=self.severity,
                quote=match.group(0),
                why=self.why,
                fix=self.fix,
                span=match.span(),
            )
        ]


def _p(pattern: str) -> Pattern:
    return re.compile(pattern, re.IGNORECASE)


class RuleLike(Protocol):
    """Structural type for the three otherwise-unrelated rule dataclasses
    (MatchRule, AbsenceRule, UnbalancedClaimRule) -- just enough for
    RULES/check_text to call .id and .check(text) without a shared base
    class none of them actually need."""

    id: str

    def check(self, text: str) -> List[Violation]: ...


RULES: List[RuleLike] = [
    MatchRule(
        id="DIRECTIVE_ADVICE",
        severity="high",
        patterns=[
            _p(
                r"\b(?:you should|we (?:strongly )?recommend|we advise|our recommendation is to)\s+(?:buy|sell|hold|invest)\b"
            ),
            _p(r"\b(?:strong\s+)?(?:buy|sell)\s+rating\b"),
            _p(r"\bthis is a\s+(?:buy|sell|hold)\b"),
        ],
        why="Directive buy/sell/hold language is a personalized recommendation, which SEC/FINRA guidance for this kind of research note prohibits without a suitability review.",
        fix='Rephrase as an observation about the theme (e.g. "historically correlates with") instead of an instruction to act.',
    ),
    MatchRule(
        id="GUARANTEE_CLAIM",
        severity="high",
        patterns=[
            # Negative lookbehinds exclude the common, actually-compliant
            # risk-disclaimer phrasing ("no guarantee", "not guaranteed",
            # "cannot guarantee ...") so this rule flags an actual
            # promissory claim, not a hedge that happens to use the word.
            _p(r"\b(?<!no )(?<!not )(?<!cannot )guarantee[sd]?\b"),
            _p(r"\bguaranteed\s+returns?\b"),
            _p(r"\brisk[- ]free\b"),
        ],
        why="No investment outcome can be guaranteed; this language is a classic promissory-claim compliance violation.",
        fix='Remove the guarantee language entirely, or reframe as a historical correlation with explicit uncertainty (e.g. "has historically tended to").',
    ),
    MatchRule(
        id="DEFINITIVE_PERFORMANCE",
        severity="high",
        patterns=[
            _p(r"\bwill\s+(?:outperform|underperform|surge|soar|plummet|rise|fall|rally|crash)\b"),
            _p(r"\bis\s+(?:certain|guaranteed)\s+to\s+(?:rise|fall|increase|decrease)\b"),
        ],
        why='A definitive future-performance claim ("will outperform") is promissory language, not a potential-risk/tailwind framing.',
        fix='Replace the definitive verb with a hedged one (e.g. "may", "could potentially") and tie the claim to a historical correlation rather than a prediction.',
    ),
    MatchRule(
        id="PRICE_TARGET",
        severity="medium",
        patterns=[
            _p(r"\bprice\s+target\s+of\s+\$?\d+(?:\.\d+)?"),
            _p(r"\btarget\s+price\s+(?:of\s+)?\$?\d+(?:\.\d+)?"),
            _p(r"\$\d+(?:\.\d+)?\s+price\s+target\b"),
        ],
        why="A specific price target is a quantitative forecast that requires a licensed analyst's suitability review and disclosure, not an automated research digest.",
        fix="Remove the specific price figure; describe the theme qualitatively instead.",
    ),
    MatchRule(
        id="PERSONALIZED_RECOMMENDATION",
        severity="high",
        patterns=[
            _p(r"\byou\s+should\s+(?:buy|sell|hold|invest|allocate|rebalance)\b"),
            _p(r"\bfor\s+your\s+portfolio,?\s+(?:we|you)\s+(?:recommend|should)\b"),
            _p(r"\bI\s+recommend\b"),
        ],
        why="Addressing the reader directly with an action recommendation personalizes the advice, which requires a suitability determination this automated digest cannot make.",
        fix="Rewrite in the third person about the theme/portfolio impact in general, without telling the reader what to personally do.",
    ),
    UnbalancedClaimRule(),
    AbsenceRule(
        id="MISSING_RISK_FRAMING",
        severity="medium",
        required_any=[
            _p(r"\brisk\b"),
            _p(r"\bdownside\b"),
            _p(r"\bvolatil"),
            _p(r"\buncertain"),
            _p(r"\bmay not\b"),
            _p(r"\bno guarantee\b"),
        ],
        why="The draft contains no risk/downside language anywhere -- every briefing needs explicit risk framing, not just potential upside.",
        fix="Add at least one sentence naming a concrete risk or downside scenario for the themes discussed.",
    ),
    AbsenceRule(
        id="MISSING_DISCLAIMER",
        severity="low",
        required_any=[
            _p(r"not (?:financial|investment) advice"),
            _p(r"not a recommendation"),
            _p(r"informational purposes"),
            _p(r"\bconsult\b"),
            _p(r"\bFINRA\b"),
            _p(r"\bSEC\b"),
            _p(r"\bdisclaimer\b"),
            _p(r"past performance"),
        ],
        why='No compliance disclaimer (e.g. "not financial advice", "for informational purposes") is present anywhere in the draft.',
        fix='Append a standard disclaimer sentence, e.g. "This briefing is for informational purposes only and is not financial advice or a recommendation."',
    ),
]

RULE_IDS = [r.id for r in RULES]


def check_text(text: str) -> List[Violation]:
    """Runs every rule against the draft and returns all violations found,
    in rule-definition order."""
    text = text or ""
    violations: List[Violation] = []
    for rule in RULES:
        violations.extend(rule.check(text))
    return violations

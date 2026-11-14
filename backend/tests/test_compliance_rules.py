from compliance import rules

CLEAN_COMPLIANT_TEXT = (
    "The semiconductor sector may continue to see demand growth, though it has "
    "historically shown volatility and there is no guarantee of future performance. "
    "A potential downside risk exists if demand softens. This briefing is for "
    "informational purposes only and is not financial advice or a recommendation. "
    "Please consult a licensed advisor before making any investment decisions."
)


def _ids(violations):
    return {v.rule_id for v in violations}


def test_clean_compliant_text_triggers_nothing():
    violations = rules.check_text(CLEAN_COMPLIANT_TEXT)
    assert violations == []


def test_directive_advice_positive():
    text = "This is a buy. We recommend buying this stock immediately."
    violations = rules.check_text(text)
    assert "DIRECTIVE_ADVICE" in _ids(violations)


def test_directive_advice_negative_on_clean_text():
    assert "DIRECTIVE_ADVICE" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_guarantee_claim_positive():
    text = "This investment is guaranteed to generate strong returns with no downside."
    violations = rules.check_text(text)
    assert "GUARANTEE_CLAIM" in _ids(violations)
    assert any(v.quote.lower() == "guaranteed" for v in violations if v.rule_id == "GUARANTEE_CLAIM")


def test_guarantee_claim_negative_on_clean_text():
    assert "GUARANTEE_CLAIM" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_definitive_performance_positive():
    text = "Analysts agree this sector will outperform the broader market next quarter."
    violations = rules.check_text(text)
    assert "DEFINITIVE_PERFORMANCE" in _ids(violations)


def test_definitive_performance_negative_on_clean_text():
    assert "DEFINITIVE_PERFORMANCE" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_price_target_positive():
    text = "Our price target of $150 reflects strong growth prospects for this name."
    violations = rules.check_text(text)
    assert "PRICE_TARGET" in _ids(violations)


def test_price_target_negative_on_clean_text():
    assert "PRICE_TARGET" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_personalized_recommendation_positive():
    text = "I recommend increasing your allocation to this theme right away."
    violations = rules.check_text(text)
    assert "PERSONALIZED_RECOMMENDATION" in _ids(violations)


def test_personalized_recommendation_negative_on_clean_text():
    assert "PERSONALIZED_RECOMMENDATION" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_unbalanced_claim_positive():
    text = "The semiconductor sector will significantly grow next year across all major names."
    violations = rules.check_text(text)
    assert "UNBALANCED_CLAIM" in _ids(violations)


def test_unbalanced_claim_negative_when_hedged():
    text = "The semiconductor sector will significantly grow next year, though this may not hold and carries risk."
    violations = rules.check_text(text)
    assert "UNBALANCED_CLAIM" not in _ids(violations)


def test_unbalanced_claim_negative_on_clean_text():
    assert "UNBALANCED_CLAIM" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_missing_risk_framing_positive():
    text = (
        "The semiconductor sector may grow due to strong demand. This briefing is for "
        "informational purposes only and is not financial advice."
    )
    violations = rules.check_text(text)
    assert "MISSING_RISK_FRAMING" in _ids(violations)


def test_missing_risk_framing_negative_on_clean_text():
    assert "MISSING_RISK_FRAMING" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_missing_disclaimer_positive():
    text = "The semiconductor sector may grow due to strong demand, though there is risk of volatility."
    violations = rules.check_text(text)
    assert "MISSING_DISCLAIMER" in _ids(violations)


def test_missing_disclaimer_negative_on_clean_text():
    assert "MISSING_DISCLAIMER" not in _ids(rules.check_text(CLEAN_COMPLIANT_TEXT))


def test_every_rule_id_is_reachable_and_unique():
    assert len(rules.RULE_IDS) == len(set(rules.RULE_IDS))
    assert set(rules.RULE_IDS) == {
        "DIRECTIVE_ADVICE",
        "GUARANTEE_CLAIM",
        "DEFINITIVE_PERFORMANCE",
        "PRICE_TARGET",
        "PERSONALIZED_RECOMMENDATION",
        "UNBALANCED_CLAIM",
        "MISSING_RISK_FRAMING",
        "MISSING_DISCLAIMER",
    }


def test_match_rule_caps_reported_matches():
    text = " ".join(["This is a buy."] * 10)
    violations = [v for v in rules.check_text(text) if v.rule_id == "DIRECTIVE_ADVICE"]
    assert len(violations) == rules.MAX_MATCHES_PER_RULE


def test_violation_to_dict_shape():
    violations = rules.check_text("This is a buy.")
    v = violations[0]
    d = v.to_dict()
    assert set(d.keys()) == {"rule", "severity", "quote", "why", "fix", "span"}
    assert d["rule"] == v.rule_id

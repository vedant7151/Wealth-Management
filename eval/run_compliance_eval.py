"""Evaluates the compliance engine: rules-only, judge-only, and combined
(rules-veto-judge, mirroring agent.py's compliance_critic_node) against
eval/compliance_set.jsonl.

Offline (default): rules-only is always real (rules.py is a pure local
function, no API call). Judge-only/combined use eval/fixtures/judge_cache.json
-- recorded real judge responses from a prior `--live` run. If no cached
verdict exists for a case and --live is not passed, that case's judge
verdict is left unknown and judge-only/combined metrics are reported as
"[MEASURE: run `make eval-live`]" rather than invented (per the HARD SAFETY
RULE: never fabricate a metric).

--live: calls the REAL judge (compliance/judge.py -> llm_client ->
Gemini/Groq) for any case not already in the cache, caches every result,
and is capped at --max-live-calls (default 20) so a eval run can't quietly
burn the project's daily Gemini budget. Do not run --live without the
user's explicit go-ahead -- see docs/PROGRESS.md's "at most 2 real graph
runs" budget note; a judge call is not a graph run, but it is still a real
billed API call outside that budget's accounting, so treat it the same way.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import eval_common  # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(__file__), "compliance_set.jsonl")
CACHE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "judge_cache.json")


def load_cases():
    cases = []
    with open(CASES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def load_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    with open(CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def rules_only_predict(compliance_rules, text):
    violations = compliance_rules.check_text(text)
    rule_ids = sorted({v.rule_id for v in violations})
    return len(violations) == 0, rule_ids


def combined_predict(rules_compliant, judge_cached):
    """Mirrors agent.py's compliance_critic_node:
    final_compliant = verdict.compliant and not rule_violations, and a
    judge error (unavailable/malformed) fails safe -- never compliant."""
    if judge_cached is None or judge_cached.get("error"):
        return False
    return bool(judge_cached.get("compliant", False)) and rules_compliant


def run(live: bool, max_live_calls: int):
    cases = load_cases()
    cache = load_cache()

    rules_y_true, rules_y_pred = [], []
    judge_y_true, judge_y_pred = [], []
    combined_y_true, combined_y_pred = [], []
    per_rule_total: dict = {}
    per_rule_hit: dict = {}
    rules_false_positive_ids = []
    judge_available_count = 0
    live_calls_made = 0
    cache_dirty = False

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
    from compliance import rules as compliance_rules  # noqa: E402

    if live:
        from compliance import judge as compliance_judge  # noqa: E402

    for case in cases:
        text = case["text"]
        expected_noncompliant = not case["expected_compliant"]
        case_id = case["id"]

        rules_compliant, detected_rule_ids = rules_only_predict(compliance_rules, text)
        rules_y_true.append(expected_noncompliant)
        rules_y_pred.append(not rules_compliant)
        if expected_noncompliant is False and not rules_compliant:
            rules_false_positive_ids.append(case_id)

        for rid in case.get("expected_rule_ids", []):
            per_rule_total[rid] = per_rule_total.get(rid, 0) + 1
            if rid in detected_rule_ids:
                per_rule_hit[rid] = per_rule_hit.get(rid, 0) + 1

        judge_cached = cache.get(case_id)
        if judge_cached is None and live and live_calls_made < max_live_calls:
            verdict = compliance_judge.call_judge(text)
            judge_cached = {
                "compliant": verdict.compliant, "violations": verdict.violations,
                "confidence": verdict.confidence, "model": verdict.model, "error": verdict.error,
            }
            cache[case_id] = judge_cached
            cache_dirty = True
            live_calls_made += 1

        if judge_cached is not None:
            judge_available_count += 1
            judge_y_true.append(expected_noncompliant)
            judge_y_pred.append(not bool(judge_cached.get("compliant", False)) or bool(judge_cached.get("error")))
            combined_y_true.append(expected_noncompliant)
            combined_y_pred.append(not combined_predict(rules_compliant, judge_cached))

    if cache_dirty:
        save_cache(cache)

    rules_metrics = eval_common.binary_classification_metrics(rules_y_true, rules_y_pred)
    judge_metrics = eval_common.binary_classification_metrics(judge_y_true, judge_y_pred) if judge_y_true else None
    combined_metrics = eval_common.binary_classification_metrics(combined_y_true, combined_y_pred) if combined_y_true else None

    per_rule_recall = {
        rid: (per_rule_hit.get(rid, 0) / total if total else None)
        for rid, total in per_rule_total.items()
    }

    by_category = {}
    for c in cases:
        by_category[c["category"]] = by_category.get(c["category"], 0) + 1

    metrics_out = {
        "n_cases": len(cases),
        "n_judge_available": judge_available_count,
        "live_calls_made_this_run": live_calls_made,
        "by_category": by_category,
        "rules_only": rules_metrics,
        "judge_only": judge_metrics,
        "combined": combined_metrics,
        "per_rule_recall_rules_only": per_rule_recall,
        "rules_false_positive_case_ids": rules_false_positive_ids,
    }
    eval_common.write_metrics("compliance", metrics_out)

    judge_hint = "run `make eval-live`" if judge_available_count == 0 else None

    lines = []
    lines.append("## Compliance eval (`run_compliance_eval.py`)\n")
    lines.append(f"Dataset: {len(cases)} cases in `eval/compliance_set.jsonl`. "
                 f"Category breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(by_category.items())) + ".\n")
    adversarial_pct = 100.0 * by_category.get("adversarial_paraphrase", 0) / len(cases)
    lines.append(f"Adversarial/paraphrased fraction: {adversarial_pct:.1f}%.\n")
    if judge_available_count < len(cases):
        lines.append(f"\n**Judge coverage**: {judge_available_count}/{len(cases)} cases have a cached judge "
                      f"verdict. {'No' if judge_available_count == 0 else 'Not all'} live judge calls were made "
                      f"this run -- judge-only/combined metrics below reflect only the cached subset, or are "
                      f"marked `[MEASURE]` if the cache is empty. See docs/PROGRESS.md's live-call-budget notes "
                      f"for why `--live` was not run.\n")

    lines.append("\n| System | Precision | Recall | F1 | FPR | Counts |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(eval_common.fmt_metric_row("Rules-only", rules_metrics))
    lines.append(eval_common.fmt_metric_row("Judge-only", judge_metrics, judge_hint))
    lines.append(eval_common.fmt_metric_row("Combined (rules veto judge)", combined_metrics, judge_hint))
    lines.append("")

    lines.append(eval_common.confusion_matrix_md("Rules-only confusion matrix", rules_metrics))
    lines.append(eval_common.confusion_matrix_md("Judge-only confusion matrix", judge_metrics, judge_hint))
    lines.append(eval_common.confusion_matrix_md("Combined confusion matrix", combined_metrics, judge_hint))

    lines.append("**Per-rule recall (rules-only)**:\n")
    lines.append("| Rule | Recall | Cases |")
    lines.append("|---|---|---|")
    for rid in sorted(per_rule_total):
        recall = per_rule_recall[rid]
        lines.append(f"| {rid} | {eval_common.fmt_pct(recall)} | {per_rule_hit.get(rid, 0)}/{per_rule_total[rid]} |")
    lines.append("")

    if rules_false_positive_ids:
        lines.append(f"**Rules-only false positives** (compliant cases incorrectly flagged): "
                      f"{', '.join(rules_false_positive_ids)}\n")
    else:
        lines.append("**Rules-only false positives**: none in this dataset.\n")

    eval_common.write_section("compliance", "\n".join(lines))

    print(f"Compliance eval: {len(cases)} cases.")
    print(f"  Rules-only  -- precision={eval_common.fmt_pct(rules_metrics['precision'])} "
          f"recall={eval_common.fmt_pct(rules_metrics['recall'])} fpr={eval_common.fmt_pct(rules_metrics['fpr'])}")
    if judge_metrics:
        print(f"  Judge-only  -- precision={eval_common.fmt_pct(judge_metrics['precision'])} "
              f"recall={eval_common.fmt_pct(judge_metrics['recall'])} fpr={eval_common.fmt_pct(judge_metrics['fpr'])} "
              f"(n={judge_available_count})")
    else:
        print("  Judge-only  -- [MEASURE: run `make eval-live`] (no cached judge verdicts)")
    if combined_metrics:
        print(f"  Combined    -- precision={eval_common.fmt_pct(combined_metrics['precision'])} "
              f"recall={eval_common.fmt_pct(combined_metrics['recall'])} fpr={eval_common.fmt_pct(combined_metrics['fpr'])}")
    else:
        print("  Combined    -- [MEASURE: run `make eval-live`] (no cached judge verdicts)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Call the real judge for cases not already cached.")
    parser.add_argument("--max-live-calls", type=int, default=20)
    args = parser.parse_args()
    run(live=args.live, max_live_calls=args.max_live_calls)


if __name__ == "__main__":
    main()

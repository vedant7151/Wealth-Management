"""Reads eval/thresholds.json and the metrics each eval script just wrote
to eval/.cache/metrics_*.json, and exits non-zero if any measured number
has regressed below its threshold. Run AFTER run_compliance_eval.py,
retrieval_eval.py, and faithfulness_eval.py (which is what `make eval`
does) -- this script does not run any eval itself.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import eval_common  # noqa: E402

THRESHOLDS_PATH = os.path.join(os.path.dirname(__file__), "thresholds.json")


def check(label: str, value, min_ok=None, max_ok=None) -> bool:
    if value is None:
        print(f"  [SKIP] {label}: no measured value available (likely needs --live)")
        return True
    ok = True
    if min_ok is not None and value < min_ok:
        print(f"  [FAIL] {label}: {value:.3f} < required minimum {min_ok:.3f}")
        ok = False
    if max_ok is not None and value > max_ok:
        print(f"  [FAIL] {label}: {value:.3f} > required maximum {max_ok:.3f}")
        ok = False
    if ok:
        print(f"  [OK]   {label}: {value:.3f}")
    return ok


def main():
    with open(THRESHOLDS_PATH, encoding="utf-8") as f:
        thresholds = json.load(f)

    all_ok = True

    compliance = eval_common.read_metrics("compliance")
    if compliance:
        t = thresholds.get("rules_only", {})
        rules = compliance["rules_only"]
        print("rules_only:")
        all_ok &= check("recall", rules["recall"], min_ok=t.get("recall_min"))
        all_ok &= check("precision", rules["precision"], min_ok=t.get("precision_min"))
        all_ok &= check("fpr", rules["fpr"], max_ok=t.get("fpr_max"))
    else:
        print("rules_only: [SKIP] run_compliance_eval.py has not been run yet this session")

    retrieval = eval_common.read_metrics("retrieval")
    if retrieval:
        t = thresholds.get("retrieval", {})
        print("retrieval:")
        all_ok &= check("recall_at_4", retrieval["recall_at_k"]["4"] if "4" in retrieval["recall_at_k"] else retrieval["recall_at_k"].get(4),
                         min_ok=t.get("recall_at_4_min"))
        all_ok &= check("mrr", retrieval["mrr"], min_ok=t.get("mrr_min"))
    else:
        print("retrieval: [SKIP] retrieval_eval.py has not been run yet this session")

    faithfulness = eval_common.read_metrics("faithfulness")
    if faithfulness:
        t = thresholds.get("faithfulness", {})
        print("faithfulness:")
        all_ok &= check("citation_precision", faithfulness["overall_citation_precision"],
                         min_ok=t.get("citation_precision_min"))
        all_ok &= check("claim_support_recall", faithfulness["claim_support_hallucination_detection"]["recall"],
                         min_ok=t.get("claim_support_recall_min"))
    else:
        print("faithfulness: [SKIP] faithfulness_eval.py has not been run yet this session")

    if not all_ok:
        print("\nOne or more regression thresholds were not met.")
        sys.exit(1)
    print("\nAll regression thresholds met.")


if __name__ == "__main__":
    main()

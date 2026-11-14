"""Shared helpers for the eval/ harness: git sha lookup, binary
precision/recall/F1/FPR + confusion matrix, and small markdown table
builders. No provider/API code lives here -- that's each eval script's own
job (and judge.py/llm_client.py's, which they import directly).
"""

import json
import os
import subprocess
from typing import Dict, List, Optional, Sequence

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(EVAL_DIR, ".cache")


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(EVAL_DIR), text=True
        ).strip()
    except Exception:
        return "unknown"


def ensure_cache_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


def write_section(name: str, markdown: str) -> None:
    """Writes one eval script's markdown fragment, to be assembled by
    generate_results.py."""
    ensure_cache_dir()
    with open(os.path.join(CACHE_DIR, f"section_{name}.md"), "w", encoding="utf-8") as f:
        f.write(markdown)


def write_metrics(name: str, metrics: dict) -> None:
    """Writes one eval script's raw metrics (used by check_thresholds.py),
    keyed by script name so each script owns its own file."""
    ensure_cache_dir()
    with open(os.path.join(CACHE_DIR, f"metrics_{name}.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


def read_metrics(name: str) -> Optional[dict]:
    path = os.path.join(CACHE_DIR, f"metrics_{name}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def binary_classification_metrics(y_true: Sequence[bool], y_pred: Sequence[bool]) -> dict:
    """Positive class = True (e.g. 'non-compliant' / 'relevant', whichever
    the caller defines True to mean). Returns counts + precision/recall/F1
    and the false-positive rate (FP / (FP + TN))."""
    tp = fp = tn = fn = 0
    for t, p in zip(y_true, y_pred):
        if t and p:
            tp += 1
        elif not t and p:
            fp += 1
        elif not t and not p:
            tn += 1
        else:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision and recall and (precision + recall) > 0) else None
    fpr = fp / (fp + tn) if (fp + tn) else None

    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1, "fpr": fpr,
        "n": len(y_true),
    }


def fmt_pct(x: Optional[float]) -> str:
    return "N/A" if x is None else f"{x * 100:.1f}%"


def fmt_metric_row(label: str, m: Optional[dict], measure_hint: Optional[str] = None) -> str:
    if m is None:
        hint = f" [MEASURE: {measure_hint}]" if measure_hint else " [MEASURE]"
        return f"| {label} | N/A{hint} | N/A | N/A | N/A | N/A |"
    return (f"| {label} | {fmt_pct(m['precision'])} | {fmt_pct(m['recall'])} | "
            f"{fmt_pct(m['f1']) if m['f1'] is not None else 'N/A'} | {fmt_pct(m['fpr'])} | "
            f"tp={m['tp']} fp={m['fp']} tn={m['tn']} fn={m['fn']} |")


def confusion_matrix_md(label: str, m: Optional[dict], measure_hint: Optional[str] = None) -> str:
    if m is None:
        hint = f" (MEASURE: {measure_hint})" if measure_hint else ""
        return f"**{label}**: not available{hint}.\n"
    return (f"**{label}** (positive = non-compliant):\n\n"
            f"| | Predicted non-compliant | Predicted compliant |\n"
            f"|---|---|---|\n"
            f"| Actual non-compliant | TP={m['tp']} | FN={m['fn']} |\n"
            f"| Actual compliant | FP={m['fp']} | TN={m['tn']} |\n")

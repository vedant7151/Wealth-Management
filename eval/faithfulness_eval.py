"""Evaluates faithfulness over a fixed, synthetic sample set
(eval/fixtures/faithfulness_samples.json) standing in for "saved sample
briefings": each sample has the retrieved context chunks a run would have
had, the citations it recorded, and a handful of draft claims hand-labeled
as grounded or hallucinated.

Two independent checks:
  1. Citation precision -- for every cited URL, was its source actually
     among the retrieved context chunks? (a real, deterministic check --
     no API call, no approximation).
  2. Claim support -- a lexical-overlap fallback (Jaccard similarity over
     word sets, stopwords removed) between each claim sentence and the
     context; flagged "unsupported" below OVERLAP_THRESHOLD. Scored
     against each claim's hand-labeled expected_supported so the method's
     own precision/recall at catching hallucinations is reported honestly,
     not asserted.

--live would additionally score claim support with the real compliance
judge (LLM-as-faithfulness-judge) instead of lexical overlap; not
implemented in this phase (see eval/results.md's Limitations/Handoff
notes) -- the lexical-overlap numbers below are the real, measured
fallback path.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import eval_common  # noqa: E402

SAMPLES_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "faithfulness_samples.json")

OVERLAP_THRESHOLD = 0.25
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "this", "that", "these", "those", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "for", "and", "or", "as", "at", "by", "with", "its", "it", "has", "have", "had",
    "this", "quarter", "year", "week", "month", "continues", "continue",
}


def _words(text: str) -> set:
    tokens = _WORD_RE.findall((text or "").lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def claim_supported(claim_text: str, context_chunks: list) -> bool:
    claim_words = _words(claim_text)
    for chunk in context_chunks:
        chunk_words = _words(chunk["content"])
        if _jaccard(claim_words, chunk_words) >= OVERLAP_THRESHOLD:
            return True
    return False


def run():
    with open(SAMPLES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    samples = data["samples"]

    # ---- Citation precision -------------------------------------------
    total_citations = 0
    correct_citations = 0
    per_sample_citation_precision = {}
    for s in samples:
        context_urls = {c["url"] for c in s["context_chunks"]}
        cited_urls = [c["url"] for c in s["citations"]]
        correct = sum(1 for u in cited_urls if u in context_urls)
        total_citations += len(cited_urls)
        correct_citations += correct
        per_sample_citation_precision[s["id"]] = (correct / len(cited_urls)) if cited_urls else None

    overall_citation_precision = correct_citations / total_citations if total_citations else None

    # ---- Claim support (lexical-overlap fallback) ----------------------
    y_true_unsupported, y_pred_unsupported = [], []
    per_claim_results = []
    for s in samples:
        for claim in s["draft_claims"]:
            predicted_supported = claim_supported(claim["text"], s["context_chunks"])
            expected_supported = claim["expected_supported"]
            y_true_unsupported.append(not expected_supported)
            y_pred_unsupported.append(not predicted_supported)
            per_claim_results.append({
                "sample": s["id"], "claim": claim["text"],
                "expected_supported": expected_supported, "predicted_supported": predicted_supported,
            })

    claim_metrics = eval_common.binary_classification_metrics(y_true_unsupported, y_pred_unsupported)

    metrics_out = {
        "n_samples": len(samples),
        "overall_citation_precision": overall_citation_precision,
        "per_sample_citation_precision": per_sample_citation_precision,
        "claim_support_hallucination_detection": claim_metrics,
        "n_claims": len(per_claim_results),
        "overlap_threshold": OVERLAP_THRESHOLD,
    }
    eval_common.write_metrics("faithfulness", metrics_out)

    lines = []
    lines.append("## Faithfulness eval (`faithfulness_eval.py`)\n")
    lines.append(f"Fixture set: {len(samples)} synthetic sample briefings, {len(per_claim_results)} hand-labeled "
                 f"draft claims (`eval/fixtures/faithfulness_samples.json`).\n")

    lines.append(f"\n**Citation precision (overall)**: {eval_common.fmt_pct(overall_citation_precision)} "
                 f"({correct_citations}/{total_citations} cited URLs were actually in the retrieved context).\n")
    lines.append("\n| Sample | Citation precision |")
    lines.append("|---|---|")
    for sid, p in per_sample_citation_precision.items():
        lines.append(f"| {sid} | {eval_common.fmt_pct(p)} |")

    lines.append(f"\n**Claim-support check (lexical-overlap fallback, threshold={OVERLAP_THRESHOLD})** -- "
                 f"treated as a hallucination detector (positive class = claim is actually unsupported):\n")
    lines.append("\n| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Precision (of flagged-unsupported, how many truly are) | {eval_common.fmt_pct(claim_metrics['precision'])} |")
    lines.append(f"| Recall (of truly-unsupported, how many were caught) | {eval_common.fmt_pct(claim_metrics['recall'])} |")
    lines.append(f"| F1 | {eval_common.fmt_pct(claim_metrics['f1']) if claim_metrics['f1'] is not None else 'N/A'} |")
    lines.append(f"| False positive rate (grounded claims wrongly flagged) | {eval_common.fmt_pct(claim_metrics['fpr'])} |")
    lines.append(f"| Counts | tp={claim_metrics['tp']} fp={claim_metrics['fp']} tn={claim_metrics['tn']} fn={claim_metrics['fn']} |")

    wrong = [r for r in per_claim_results if r["expected_supported"] != r["predicted_supported"]]
    if wrong:
        lines.append("\n**Claims the lexical-overlap fallback got wrong**:\n")
        for r in wrong:
            lines.append(f"- [{r['sample']}] expected_supported={r['expected_supported']}, "
                         f"predicted_supported={r['predicted_supported']}: \"{r['claim']}\"")
    else:
        lines.append("\nThe lexical-overlap fallback matched every hand-label in this fixture set.\n")

    eval_common.write_section("faithfulness", "\n".join(lines))

    print(f"Faithfulness eval: {len(samples)} samples, {len(per_claim_results)} claims.")
    print(f"  Citation precision = {eval_common.fmt_pct(overall_citation_precision)}")
    print(f"  Claim-support (hallucination detection) -- precision={eval_common.fmt_pct(claim_metrics['precision'])} "
          f"recall={eval_common.fmt_pct(claim_metrics['recall'])}")


if __name__ == "__main__":
    run()

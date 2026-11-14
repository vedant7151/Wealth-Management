"""Evaluates retriever_node's RAG ranking approach (per-theme query embedding
-> top-k pgvector L2 distance search -> RETRIEVAL_MAX_DISTANCE cutoff)
against a fixed fixture corpus with known relevant-doc labels
(eval/fixtures/retrieval_corpus.json).

Fully offline and deterministic: uses embedding_stub.py (a hashed
bag-of-words vector, NOT the real HuggingFace model) instead of a live
embedding API call, and ranks in-process instead of touching MockDB/real
pgvector -- the ranking math (sort by L2 distance, then apply a distance
cutoff, same as backend/agent.py's retriever_node) is replicated directly
here rather than importing agent.py, so this script has zero dependency on
a database connection or any provider credentials.

Reports recall@k for k in {1, 2, 4 (the real RETRIEVAL_TOP_K default), 8},
MRR, and a sweep of the distance-cutoff threshold with a data-driven
recommendation for RETRIEVAL_MAX_DISTANCE.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import embedding_stub  # noqa: E402
import eval_common  # noqa: E402

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "retrieval_corpus.json")

# Matches backend/agent.py's defaults (RETRIEVAL_TOP_K=4; RETRIEVAL_MAX_DISTANCE
# env-default 1.0) so the sweep's recommendation is directly comparable to
# what's actually configured today.
CURRENT_RETRIEVAL_TOP_K = 4
CURRENT_RETRIEVAL_MAX_DISTANCE = float(os.environ.get("RETRIEVAL_MAX_DISTANCE", "1.0"))

K_VALUES = [1, 2, 4, 8]


def load_corpus():
    with open(CORPUS_PATH, encoding="utf-8") as f:
        return json.load(f)


def rank_docs_for_theme(theme: str, docs: list) -> list:
    """Returns docs sorted by L2 distance to the theme's embedding
    (ascending = most similar first), each annotated with its distance --
    mirrors retriever_node's `ORDER BY embedding <-> $1::vector` query."""
    query_emb = embedding_stub.embed(theme)
    scored = []
    for doc in docs:
        dist = embedding_stub.l2_distance(query_emb, embedding_stub.embed(doc["content"]))
        scored.append({**doc, "distance": dist})
    scored.sort(key=lambda d: d["distance"])
    return scored


def recall_at_k(ranked: list, relevant_ids: set, k: int) -> float:
    top_k_ids = {d["id"] for d in ranked[:k]}
    hit = len(top_k_ids & relevant_ids)
    return hit / len(relevant_ids) if relevant_ids else None


def reciprocal_rank(ranked: list, relevant_ids: set) -> float:
    for i, d in enumerate(ranked, start=1):
        if d["id"] in relevant_ids:
            return 1.0 / i
    return 0.0


def sweep_thresholds(all_pairs: list, candidate_thresholds: list) -> list:
    """all_pairs: list of (distance, is_relevant) across every (theme, doc)
    pair in the corpus (not just top-k) -- the same population
    RETRIEVAL_MAX_DISTANCE actually filters in retriever_node, regardless
    of top-k rank. Returns a list of {threshold, precision, recall, f1}."""
    results = []
    for t in candidate_thresholds:
        y_true = [is_rel for _, is_rel in all_pairs]
        y_pred = [dist <= t for dist, _ in all_pairs]
        m = eval_common.binary_classification_metrics(y_true, y_pred)
        results.append({"threshold": t, **m})
    return results


def run():
    corpus = load_corpus()
    themes = corpus["themes"]
    docs = corpus["documents"]

    per_theme = {}
    all_pairs = []  # (distance, is_relevant) across every (theme, doc) pair

    for theme in themes:
        relevant_ids = {d["id"] for d in docs if d["theme"] == theme}
        ranked = rank_docs_for_theme(theme, docs)
        recalls = {k: recall_at_k(ranked, relevant_ids, k) for k in K_VALUES}
        mrr = reciprocal_rank(ranked, relevant_ids)
        per_theme[theme] = {"recalls": recalls, "mrr": mrr, "ranked": ranked}
        for d in ranked:
            all_pairs.append((d["distance"], d["id"] in relevant_ids))

    mean_recalls = {k: sum(per_theme[t]["recalls"][k] for t in themes) / len(themes) for k in K_VALUES}
    mean_mrr = sum(per_theme[t]["mrr"] for t in themes) / len(themes)

    distances = sorted({round(d, 4) for d, _ in all_pairs})
    candidate_thresholds = sorted(set(distances) | {CURRENT_RETRIEVAL_MAX_DISTANCE})
    sweep = sweep_thresholds(all_pairs, candidate_thresholds)
    best = max((s for s in sweep if s["f1"] is not None), key=lambda s: s["f1"], default=None)
    current = next((s for s in sweep if abs(s["threshold"] - CURRENT_RETRIEVAL_MAX_DISTANCE) < 1e-9), None)

    metrics_out = {
        "n_themes": len(themes),
        "n_docs": len(docs),
        "recall_at_k": mean_recalls,
        "mrr": mean_mrr,
        "current_retrieval_max_distance": CURRENT_RETRIEVAL_MAX_DISTANCE,
        "current_threshold_metrics": current,
        "recommended_threshold": best,
        "per_theme": {t: {"recalls": per_theme[t]["recalls"], "mrr": per_theme[t]["mrr"]} for t in themes},
    }
    eval_common.write_metrics("retrieval", metrics_out)

    lines = []
    lines.append("## Retrieval eval (`retrieval_eval.py`)\n")
    lines.append(f"Fixture corpus: {len(docs)} documents across {len(themes)} themes "
                 f"(`eval/fixtures/retrieval_corpus.json`), ranked with a deterministic local hashed-bag-of-words "
                 f"embedding stub (`embedding_stub.py`) -- NOT the real HuggingFace model (see Limitations).\n")

    lines.append("\n**Recall@k (mean across themes)**:\n")
    lines.append("| k | Recall@k |")
    lines.append("|---|---|")
    for k in K_VALUES:
        marker = " (current RETRIEVAL_TOP_K)" if k == CURRENT_RETRIEVAL_TOP_K else ""
        lines.append(f"| {k}{marker} | {eval_common.fmt_pct(mean_recalls[k])} |")
    lines.append(f"\n**MRR (mean across themes)**: {mean_mrr:.3f}\n")

    lines.append("\n**Per-theme detail**:\n")
    lines.append("| Theme | Recall@4 | MRR |")
    lines.append("|---|---|---|")
    for t in themes:
        lines.append(f"| {t} | {eval_common.fmt_pct(per_theme[t]['recalls'][4])} | {per_theme[t]['mrr']:.3f} |")

    lines.append("\n**Distance-threshold sweep** (RETRIEVAL_MAX_DISTANCE candidate -> precision/recall/F1 "
                  "over every (theme, doc) pair in the corpus, not just top-k):\n")
    lines.append("| Threshold | Precision | Recall | F1 |")
    lines.append("|---|---|---|---|")
    for s in sweep:
        marker = " <- current default" if abs(s["threshold"] - CURRENT_RETRIEVAL_MAX_DISTANCE) < 1e-9 else ""
        marker += " <- recommended (max F1)" if best and abs(s["threshold"] - best["threshold"]) < 1e-9 else ""
        lines.append(f"| {s['threshold']:.4f}{marker} | {eval_common.fmt_pct(s['precision'])} | "
                      f"{eval_common.fmt_pct(s['recall'])} | {eval_common.fmt_pct(s['f1']) if s['f1'] is not None else 'N/A'} |")

    if best:
        lines.append(f"\n**Recommendation**: on this fixture corpus (with the embedding stub, not the real "
                      f"model), `RETRIEVAL_MAX_DISTANCE={best['threshold']:.4f}` maximizes F1 "
                      f"({eval_common.fmt_pct(best['f1'])}) vs. the current default "
                      f"`{CURRENT_RETRIEVAL_MAX_DISTANCE}` ({eval_common.fmt_pct(current['f1']) if current and current['f1'] is not None else 'N/A'}). "
                      f"This is a stub-embedding-space number, not a literal production value -- see Limitations "
                      f"for why it should inform, not replace, tuning against real embeddings.\n")

    eval_common.write_section("retrieval", "\n".join(lines))

    print(f"Retrieval eval: {len(docs)} docs, {len(themes)} themes.")
    for k in K_VALUES:
        print(f"  recall@{k} = {eval_common.fmt_pct(mean_recalls[k])}")
    print(f"  MRR = {mean_mrr:.3f}")
    if best:
        print(f"  recommended RETRIEVAL_MAX_DISTANCE (stub space) = {best['threshold']:.4f} (F1={eval_common.fmt_pct(best['f1'])})")


if __name__ == "__main__":
    run()

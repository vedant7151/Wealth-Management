"""Per-model price ESTIMATES (USD), used only to attach a rough cost figure
to ApiCallLog rows for the admin usage view (GET /api/admin/usage; a UI for
it is Phase 7 scope). These are NOT fetched from a live pricing API, NOT
contractual, and NOT billing-accurate -- treat every `estCostUsd` value in
the DB as directional ("roughly how much did today cost"), never as an
invoice. Update the numbers here if a provider changes its public pricing;
there's no code elsewhere that needs to change.
"""

from typing import Optional

# USD per 1,000,000 tokens. Source: each provider's public pricing page,
# captured 2026-11 -- ESTIMATES, re-check before using these for anything
# that matters financially.
MODEL_PRICES_PER_MILLION_TOKENS = {
    "gemini-3.6-flash": {"input": 0.075, "output": 0.30},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    "llama-3.1-70b-versatile": {"input": 0.59, "output": 0.79},
    # Classifier model, not priced per-token the way a chat model is --
    # treated as negligible/zero for this estimate.
    "meta-llama/llama-prompt-guard-2-86m": {"input": 0.0, "output": 0.0},
    # HuggingFace inference-router embeddings -- free tier in this project
    # today; treated as zero for the estimate (update if that changes).
    "BAAI/bge-small-en-v1.5": {"input": 0.0, "output": 0.0},
    # Tavily is priced per-search, not per-token; see TAVILY_PRICE_PER_SEARCH_USD.
}

# USD per search call. ESTIMATE.
TAVILY_PRICE_PER_SEARCH_USD = 0.008


def estimate_tokens(text: str) -> int:
    """Extremely rough estimate (~4 chars/token for English prose).
    ESTIMATE only -- do not use for anything billing-accurate."""
    return max(1, len(text or "") // 4)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> Optional[float]:
    if model == "tavily-search":
        return TAVILY_PRICE_PER_SEARCH_USD
    prices = MODEL_PRICES_PER_MILLION_TOKENS.get(model)
    if not prices:
        return None
    return (input_tokens / 1_000_000) * prices["input"] + (output_tokens / 1_000_000) * prices["output"]

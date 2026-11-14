"""Phase 7: researcher_node builds ticker/sector-aware search queries when
a client has uploaded holdings, falling back to theme-only queries when it
doesn't. No live Gemini/Tavily/HF calls -- llm_client is monkeypatched."""

import os

os.environ.setdefault("MOCK_DB", "1")

import agent  # noqa: E402
import llm_client  # noqa: E402


def _base_state(**overrides):
    state = agent.new_input_state("client-1", ["AI infrastructure"], run_id="run-1")
    state.update(overrides)
    return state


class _FakeHolding:
    def __init__(self, ticker, sector):
        self.ticker = ticker
        self.sector = sector


def test_researcher_prompt_includes_holdings_when_present(monkeypatch):
    captured = {}

    def fake_generate_json(prompt, purpose, run_id=None, validate=None, **kwargs):
        captured["prompt"] = prompt
        return llm_client.GenerationResult(parsed=["AAPL earnings outlook"], provider="gemini", model="fake")

    monkeypatch.setattr(llm_client, "generate_json", fake_generate_json)
    monkeypatch.setattr(llm_client, "embed_text", lambda *a, **k: [0.0] * 384)
    monkeypatch.setattr(llm_client, "tavily_search", lambda *a, **k: [])

    db = agent.get_db()
    fake_accessor = type(
        "FakeHoldingAccessor", (), {"find_many": lambda self, where=None: [_FakeHolding("AAPL", "Technology")]}
    )()
    monkeypatch.setattr(db, "holding", fake_accessor)

    state = _base_state()
    agent.researcher_node(state)

    assert "AAPL" in captured["prompt"]
    assert "Technology" in captured["prompt"]


def test_researcher_prompt_falls_back_to_themes_when_no_holdings(monkeypatch):
    captured = {}

    def fake_generate_json(prompt, purpose, run_id=None, validate=None, **kwargs):
        captured["prompt"] = prompt
        return llm_client.GenerationResult(parsed=["AI infrastructure financial news"], provider="gemini", model="fake")

    monkeypatch.setattr(llm_client, "generate_json", fake_generate_json)
    monkeypatch.setattr(llm_client, "embed_text", lambda *a, **k: [0.0] * 384)
    monkeypatch.setattr(llm_client, "tavily_search", lambda *a, **k: [])

    state = _base_state()
    agent.researcher_node(state)

    assert "AI infrastructure" in captured["prompt"]
    assert "AAPL" not in captured["prompt"]

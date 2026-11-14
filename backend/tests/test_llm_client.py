"""Tests for llm_client.py: provider fallback, retries, circuit breaker,
the ApiCallLog ledger, embedding dimension assertion, and the real-usage
daily budget check. No live API calls -- the provider-dispatch functions
(_gemini_call/_groq_call/_hf_embed_call) and time.sleep are monkeypatched,
and a fake db stands in for Prisma."""

import pytest

import llm_client


class _FakeApiCallLog:
    def __init__(self):
        self.rows = []

    def create(self, data):
        self.rows.append(data)


class _FakeDB:
    """Minimal fake satisfying llm_client's _log_call/check_daily_budget
    usage: an .apicalllog.create() sink and a .query_raw() that counts
    rows already recorded in the sink, scoped by provider."""

    def __init__(self):
        self.apicalllog = _FakeApiCallLog()

    def query_raw(self, sql, *args):
        provider = args[0]
        count = sum(1 for r in self.apicalllog.rows if r["provider"] == provider)
        return [{"cnt": count}]


@pytest.fixture(autouse=True)
def _reset_breakers_and_db(monkeypatch):
    llm_client._breakers.clear()
    fake_db = _FakeDB()
    llm_client.configure(lambda: fake_db)
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_args, **_kwargs: None)
    yield fake_db
    llm_client.configure(None)
    llm_client._breakers.clear()


def _fake_gemini(prompt, model, json_mode):
    raise RuntimeError("429 RESOURCE_EXHAUSTED")


def _fake_groq_success(prompt, model, json_mode):
    return '{"ok": true}' if json_mode else "groq text response"


# --------------------------------------------------------------------------
# Provider fallback
# --------------------------------------------------------------------------


def test_primary_429_falls_back_to_secondary_provider(monkeypatch):
    monkeypatch.setattr(llm_client, "_gemini_call", _fake_gemini)
    monkeypatch.setattr(llm_client, "_groq_call", _fake_groq_success)

    result = llm_client.generate_text("prompt", purpose="test")

    assert result.error is None
    assert result.provider == "groq"
    assert result.text == "groq text response"


def test_both_providers_fail_returns_controlled_error(monkeypatch):
    monkeypatch.setattr(llm_client, "_gemini_call", _fake_gemini)
    monkeypatch.setattr(llm_client, "_groq_call", _fake_gemini)

    result = llm_client.generate_text("prompt", purpose="test")

    assert result.error == "all_providers_unavailable"
    assert result.text is None


def test_successful_primary_never_touches_fallback(monkeypatch):
    calls = {"groq": 0}

    def _gemini_ok(prompt, model, json_mode):
        return "gemini text"

    def _groq_should_not_be_called(prompt, model, json_mode):
        calls["groq"] += 1
        return "unused"

    monkeypatch.setattr(llm_client, "_gemini_call", _gemini_ok)
    monkeypatch.setattr(llm_client, "_groq_call", _groq_should_not_be_called)

    result = llm_client.generate_text("prompt", purpose="test")

    assert result.provider == "gemini"
    assert calls["groq"] == 0


def test_non_retryable_error_moves_to_fallback_without_exhausting_retries(monkeypatch):
    attempts = {"gemini": 0}

    def _gemini_bad_request(prompt, model, json_mode):
        attempts["gemini"] += 1
        raise RuntimeError("400 Bad Request: invalid argument")

    monkeypatch.setattr(llm_client, "_gemini_call", _gemini_bad_request)
    monkeypatch.setattr(llm_client, "_groq_call", _fake_groq_success)

    result = llm_client.generate_text("prompt", purpose="test")

    assert result.provider == "groq"
    assert attempts["gemini"] == 1  # not retried -- non-retryable error


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def test_circuit_opens_after_threshold_consecutive_failures_and_skips_provider(monkeypatch):
    attempts = {"gemini": 0}

    def _gemini_fail(prompt, model, json_mode):
        attempts["gemini"] += 1
        raise RuntimeError("503 UNAVAILABLE")

    monkeypatch.setattr(llm_client, "_gemini_call", _gemini_fail)
    monkeypatch.setattr(llm_client, "_groq_call", _fake_groq_success)

    for _ in range(llm_client.CIRCUIT_BREAKER_THRESHOLD):
        llm_client.generate_text("prompt", purpose="test")

    assert llm_client.get_breaker("gemini").is_open() is True

    attempts_before = attempts["gemini"]
    result = llm_client.generate_text("prompt", purpose="test")

    assert attempts["gemini"] == attempts_before  # gemini was skipped entirely
    assert result.provider == "groq"


def test_circuit_closes_after_cooldown_elapses():
    breaker = llm_client.get_breaker("gemini")
    for _ in range(llm_client.CIRCUIT_BREAKER_THRESHOLD):
        breaker.record_failure(now=1000.0)

    assert breaker.is_open(now=1000.0) is True
    assert breaker.is_open(now=1000.0 + breaker.cooldown_seconds + 1) is False


def test_breaker_success_resets_consecutive_failures():
    breaker = llm_client.get_breaker("gemini")
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state()["consecutive_failures"] == 0


def test_breaker_states_exposes_every_tracked_provider():
    llm_client.get_breaker("gemini").record_failure()
    llm_client.get_breaker("tavily").record_success()
    states = llm_client.breaker_states()
    assert "gemini" in states
    assert "tavily" in states
    assert states["gemini"]["consecutive_failures"] == 1


# --------------------------------------------------------------------------
# JSON repair / malformed-response retry
# --------------------------------------------------------------------------


def test_malformed_json_retries_same_provider_before_falling_back(monkeypatch):
    responses = iter(["not json", '{"ok": true}'])
    attempts = {"gemini": 0}

    def _gemini_then_fix(prompt, model, json_mode):
        attempts["gemini"] += 1
        return next(responses)

    monkeypatch.setattr(llm_client, "_gemini_call", _gemini_then_fix)

    result = llm_client.generate_json("prompt", purpose="test")

    assert result.error is None
    assert result.provider == "gemini"
    assert attempts["gemini"] == 2


def test_validate_callback_rejecting_shape_counts_as_malformed(monkeypatch):
    def _gemini_valid_json_wrong_shape(prompt, model, json_mode):
        return '{"unexpected": "shape"}'

    monkeypatch.setattr(llm_client, "_gemini_call", _gemini_valid_json_wrong_shape)
    monkeypatch.setattr(llm_client, "_groq_call", _gemini_valid_json_wrong_shape)

    result = llm_client.generate_json("prompt", purpose="test", validate=lambda p: None)

    assert result.error == "malformed_response"


# --------------------------------------------------------------------------
# ApiCallLog: every call is logged
# --------------------------------------------------------------------------


def test_every_call_is_logged_to_api_call_log(monkeypatch, _reset_breakers_and_db):
    fake_db = _reset_breakers_and_db
    monkeypatch.setattr(llm_client, "_gemini_call", lambda p, m, j: "ok")

    llm_client.generate_text("prompt", purpose="test", run_id="run-1")

    assert len(fake_db.apicalllog.rows) == 1
    row = fake_db.apicalllog.rows[0]
    assert row["provider"] == "gemini"
    assert row["purpose"] == "test"
    assert row["runId"] == "run-1"
    assert row["status"] == "ok"


def test_failed_call_is_also_logged(monkeypatch, _reset_breakers_and_db):
    fake_db = _reset_breakers_and_db
    monkeypatch.setattr(llm_client, "_gemini_call", _fake_gemini)
    monkeypatch.setattr(llm_client, "_groq_call", _fake_gemini)

    llm_client.generate_text("prompt", purpose="test")

    statuses = [r["status"] for r in fake_db.apicalllog.rows]
    assert "error" in statuses


# --------------------------------------------------------------------------
# Daily budget guard: real usage from ApiCallLog
# --------------------------------------------------------------------------


def test_daily_budget_counts_real_logged_calls(monkeypatch, _reset_breakers_and_db):
    monkeypatch.setattr(llm_client, "DAILY_LLM_BUDGET", 3)
    monkeypatch.setattr(llm_client, "_gemini_call", lambda p, m, j: "ok")

    for _ in range(2):
        llm_client.generate_text("prompt", purpose="test")

    status = llm_client.check_daily_budget(provider="gemini")
    assert status == {"allowed": True, "used": 2, "limit": 3}

    llm_client.generate_text("prompt", purpose="test")
    status = llm_client.check_daily_budget(provider="gemini")
    assert status["allowed"] is False
    assert status["used"] == 3


def test_daily_budget_fails_closed_when_ledger_unreadable(monkeypatch):
    def _broken_getter():
        raise RuntimeError("db down")

    llm_client.configure(_broken_getter)
    status = llm_client.check_daily_budget(provider="gemini")
    assert status["allowed"] is False


def test_daily_budget_fails_closed_when_not_configured():
    llm_client.configure(None)
    status = llm_client.check_daily_budget(provider="gemini")
    assert status["allowed"] is False


# --------------------------------------------------------------------------
# Embeddings: dimension assertion + batching
# --------------------------------------------------------------------------


def test_embed_text_returns_vector_of_configured_dimension(monkeypatch):
    monkeypatch.setattr(llm_client, "_embed_call", lambda texts: [[0.0] * llm_client.EMBEDDING_DIM for _ in texts])

    vec = llm_client.embed_text("hello")

    assert len(vec) == llm_client.EMBEDDING_DIM


def test_embed_dimension_mismatch_raises_loudly_and_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def _wrong_dim(texts):
        calls["n"] += 1
        return [[0.0] * (llm_client.EMBEDDING_DIM - 1) for _ in texts]

    monkeypatch.setattr(llm_client, "_embed_call", _wrong_dim)

    with pytest.raises(llm_client.EmbeddingDimensionError):
        llm_client.embed_text("hello")

    assert calls["n"] == 1  # not retried -- a dimension mismatch is a contract violation, not transient


def test_embed_batch_splits_into_chunks_and_preserves_order(monkeypatch):
    def _echo_dim(texts):
        return [[float(i)] * llm_client.EMBEDDING_DIM for i, _ in enumerate(texts)]

    monkeypatch.setattr(llm_client, "_embed_call", _echo_dim)
    monkeypatch.setattr(llm_client, "EMBEDDING_BATCH_SIZE", 2)

    texts = [f"text-{i}" for i in range(5)]
    vectors = llm_client.embed_batch(texts)

    assert len(vectors) == 5
    assert all(len(v) == llm_client.EMBEDDING_DIM for v in vectors)


def test_embed_batch_empty_input_returns_empty_list():
    assert llm_client.embed_batch([]) == []


def test_gemini_embedding_requests_384_dims_and_normalizes(monkeypatch):
    """Provider default is Gemini: one embed_content call per batch with
    output_dimensionality=EMBEDDING_DIM, outputs L2-normalized."""
    from types import SimpleNamespace

    seen = {}

    def fake_embed_content(model, contents, config):
        seen["model"], seen["n"], seen["dim"] = model, len(contents), config.output_dimensionality
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=[3.0, 4.0] + [0.0] * (llm_client.EMBEDDING_DIM - 2)) for _ in contents]
        )

    monkeypatch.setattr(llm_client, "EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setattr(
        llm_client, "gemini_client", SimpleNamespace(models=SimpleNamespace(embed_content=fake_embed_content))
    )
    vecs = llm_client._embed_call(["a", "b"])
    assert seen["dim"] == llm_client.EMBEDDING_DIM == 384 and seen["n"] == 2
    assert abs(sum(x * x for x in vecs[0]) ** 0.5 - 1.0) < 1e-9 and vecs[0][:2] == [0.6, 0.8]


def test_embedding_provider_default_is_gemini_and_logged_separately_from_the_llm_budget():
    assert llm_client.EMBEDDING_PROVIDER == "gemini"
    # must not be "gemini": check_daily_budget counts ApiCallLog rows for that provider
    assert llm_client.EMBEDDING_PROVIDER_LABEL == "gemini_embeddings"


def test_importing_llm_client_needs_no_provider_keys_and_failure_is_at_use():
    """Regression: the SDK clients used to be built at import, so a missing key
    crashed the API/CI at startup (and broke key-less demo mode). Blank values
    win over any developer .env (load_dotenv never overrides existing vars)."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    for k in ("GEMINI_API_KEY", "TAVILY_API_KEY", "GROQ_API_KEY", "HUGGINGFACE_API_KEY", "GOOGLE_API_KEY"):
        env[k] = ""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, "-c", "import llm_client; print('imported')"],
        capture_output=True,
        text=True,
        env=env,
        cwd=backend,
        timeout=120,
    )
    assert "imported" in r.stdout, r.stderr[-300:]

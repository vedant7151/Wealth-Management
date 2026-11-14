import pytest

import ratelimit


def test_run_policy_allows_first_three_with_correct_remaining():
    now = 1_000_000.0
    r1 = ratelimit.check("run", "user:alice", now=now)
    r2 = ratelimit.check("run", "user:alice", now=now + 1)
    r3 = ratelimit.check("run", "user:alice", now=now + 2)

    assert (r1.allowed, r1.remaining) == (True, 2)
    assert (r2.allowed, r2.remaining) == (True, 1)
    assert (r3.allowed, r3.remaining) == (True, 0)


def test_run_policy_rejects_fourth_call_with_429_style_headers():
    now = 2_000_000.0
    for i in range(3):
        result = ratelimit.check("run", "user:bob", now=now + i)
        assert result.allowed

    fourth = ratelimit.check("run", "user:bob", now=now + 3)

    assert fourth.allowed is False
    assert fourth.limit == ratelimit.RUN_LIMIT
    assert fourth.remaining == 0
    assert fourth.retry_after >= 0
    assert fourth.reset_at > now


def test_run_window_resets_after_time_advances_past_window():
    now = 3_000_000.0
    for i in range(3):
        assert ratelimit.check("run", "user:carol", now=now + i).allowed
    assert ratelimit.check("run", "user:carol", now=now + 3).allowed is False

    # Advance past the full window: the oldest hit should have expired and
    # a new call should be allowed again.
    later = now + ratelimit.RUN_WINDOW_SECONDS + 10
    result = ratelimit.check("run", "user:carol", now=later)
    assert result.allowed is True


def test_run_policy_is_isolated_per_user():
    now = 4_000_000.0
    for i in range(3):
        assert ratelimit.check("run", "user:dave", now=now + i).allowed

    # dave is now exhausted, but a completely different identity must not
    # be affected by dave's usage.
    assert ratelimit.check("run", "user:dave", now=now + 3).allowed is False
    assert ratelimit.check("run", "user:erin", now=now + 3).allowed is True


def test_concurrency_lock_blocks_second_acquire_then_releases():
    key = "run:client-123"
    now = 5_000_000.0

    assert ratelimit.try_lock(key, ttl_seconds=300, now=now) is True
    # Second attempt while still held must fail (this is what api.py turns
    # into an HTTP 409 for a concurrent /run on the same client).
    assert ratelimit.try_lock(key, ttl_seconds=300, now=now + 1) is False

    ratelimit.release_lock(key)
    assert ratelimit.try_lock(key, ttl_seconds=300, now=now + 2) is True


def test_stale_lock_is_reclaimable_after_ttl():
    key = "run:client-456"
    now = 6_000_000.0
    assert ratelimit.try_lock(key, ttl_seconds=10, now=now) is True
    assert ratelimit.try_lock(key, ttl_seconds=10, now=now + 5) is False
    # Past the TTL without an explicit release (e.g. a crashed worker) --
    # the lock must not be held forever.
    assert ratelimit.try_lock(key, ttl_seconds=10, now=now + 11) is True


def test_daily_llm_budget_is_global_and_rejects_once_exhausted():
    now = 7_000_000.0
    for i in range(ratelimit.DAILY_LLM_BUDGET):
        result = ratelimit.check("llm_budget", "global", now=now + i)
        assert result.allowed, f"call {i} should be allowed"

    exhausted = ratelimit.check("llm_budget", "global", now=now + ratelimit.DAILY_LLM_BUDGET)
    assert exhausted.allowed is False
    assert exhausted.remaining == 0


def test_peek_does_not_consume_a_slot():
    now = 8_000_000.0
    peeked = ratelimit.peek("run", "user:frank", now=now)
    assert peeked.allowed is True
    assert peeked.remaining == ratelimit.RUN_LIMIT

    # peek must not have consumed anything -- a real check should still see
    # the full limit available.
    first_real_hit = ratelimit.check("run", "user:frank", now=now + 1)
    assert first_real_hit.remaining == ratelimit.RUN_LIMIT - 1


def test_store_failure_fails_closed_for_run_policy(monkeypatch):
    def _raise(*args, **kwargs):
        raise ratelimit.RateLimitStoreError("simulated store outage")

    monkeypatch.setattr(ratelimit._memory_store, "hit", _raise)

    result = ratelimit.check("run", "user:grace", now=9_000_000.0)

    assert result.allowed is False
    assert result.remaining == 0


def test_store_failure_fails_open_for_global_api_policy(monkeypatch):
    def _raise(*args, **kwargs):
        raise ratelimit.RateLimitStoreError("simulated store outage")

    monkeypatch.setattr(ratelimit._memory_store, "hit", _raise)

    result = ratelimit.check("global_api", "ip:1.2.3.4", now=9_000_000.0)

    assert result.allowed is True


def test_try_lock_failure_fails_closed(monkeypatch):
    def _raise(*args, **kwargs):
        raise ratelimit.RateLimitStoreError("simulated store outage")

    monkeypatch.setattr(ratelimit._memory_store, "try_lock", _raise)

    assert ratelimit.try_lock("run:client-789", now=9_000_000.0) is False


@pytest.mark.parametrize("policy_name", ["run", "llm_budget", "global_api", "write"])
def test_every_policy_is_registered_with_a_fail_mode(policy_name):
    assert ratelimit.POLICIES[policy_name].fail_mode in ("open", "closed")

from __future__ import annotations

import random
from datetime import datetime, timezone

from claude_orchestrator.config import RetryPolicyConfig
from claude_orchestrator.models import RetryDisposition
from claude_orchestrator.retry import HttpFailure, RetryPolicy


def test_retry_after_header_takes_precedence():
    policy = RetryPolicy(RetryPolicyConfig(), rng=random.Random(0))
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    decision = policy.classify_failure(
        HttpFailure(
            status_code=429,
            body={"error": {"type": "rate_limit_error", "message": "slow down"}},
            headers={"retry-after": "42"},
            message="rate limited",
        ),
        attempt_count=1,
        max_attempts=5,
        now=now,
    )
    assert decision.disposition == RetryDisposition.RETRY
    assert decision.retry_after_seconds == 42
    assert int((decision.retry_at - now).total_seconds()) == 42


def test_exponential_backoff_without_retry_after():
    policy = RetryPolicy(RetryPolicyConfig(base_delay_seconds=15, max_delay_seconds=300, jitter_ratio=0))
    assert policy.compute_delay_seconds(1) == 15
    assert policy.compute_delay_seconds(2) == 30
    assert policy.compute_delay_seconds(3) == 60
    assert policy.compute_delay_seconds(4) == 120
    assert policy.compute_delay_seconds(6) == 300


def test_invalid_key_failure_is_permanent():
    policy = RetryPolicy(RetryPolicyConfig())
    decision = policy.classify_failure(
        HttpFailure(
            status_code=401,
            body={"error": {"type": "authentication_error", "message": "bad key"}},
            headers={},
            message="unauthorized",
        ),
        attempt_count=1,
        max_attempts=5,
        now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )
    assert decision.disposition == RetryDisposition.FAIL
    assert decision.reason == "invalid_request_or_auth"


def test_upstream_5xx_is_retryable():
    policy = RetryPolicy(RetryPolicyConfig(jitter_ratio=0))
    now = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    decision = policy.classify_failure(
        HttpFailure(
            status_code=503,
            body={"error": {"type": "overloaded_error", "message": "try later"}},
            headers={},
            message="service unavailable",
        ),
        attempt_count=1,
        max_attempts=5,
        now=now,
    )
    assert decision.disposition == RetryDisposition.RETRY
    assert int((decision.retry_at - now).total_seconds()) == 15


def test_transient_filesystem_lock_is_retryable():
    policy = RetryPolicy(RetryPolicyConfig())
    decision = policy.classify_failure(
        RuntimeError("database is locked"),
        attempt_count=1,
        max_attempts=5,
        now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
    )
    assert decision.disposition == RetryDisposition.RETRY

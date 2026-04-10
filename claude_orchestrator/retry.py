"""Centralized retry classification and backoff logic."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional

from .config import RetryPolicyConfig
from .models import RetryDecision, RetryDisposition


class OrchestratorError(Exception):
    """Base error for orchestrator-specific failures."""


class ConfigurationError(OrchestratorError):
    """Raised when local operator configuration is invalid."""


class PermanentBackendError(OrchestratorError):
    """Raised when the upstream request should not be retried."""


class RetryableBackendError(OrchestratorError):
    """Raised when the upstream failure can be retried."""


class UserCancelledError(OrchestratorError):
    """Raised when an operator cancels a running job."""


@dataclass
class HttpFailure:
    status_code: int
    body: Dict[str, Any]
    headers: Mapping[str, Any]
    message: str


def parse_retry_after(value: Optional[str], now: datetime) -> Optional[int]:
    """Parse Retry-After seconds or HTTP date."""

    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return max(int(stripped), 0)
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed - now.astimezone(timezone.utc)
    return max(int(delta.total_seconds()), 0)


class RetryPolicy:
    """Bounded exponential backoff with jitter and Retry-After precedence."""

    def __init__(self, config: RetryPolicyConfig, rng: Optional[random.Random] = None) -> None:
        self.config = config
        self.rng = rng or random.Random()

    def compute_delay_seconds(self, attempt_count: int, retry_after_seconds: Optional[int] = None) -> int:
        if retry_after_seconds is not None:
            return max(retry_after_seconds, 0)
        exponent = max(attempt_count - 1, 0)
        raw_delay = min(self.config.base_delay_seconds * (2 ** exponent), self.config.max_delay_seconds)
        jitter_window = max(int(raw_delay * self.config.jitter_ratio), 0)
        if jitter_window == 0:
            return raw_delay
        return min(raw_delay + self.rng.randint(0, jitter_window), self.config.max_delay_seconds)

    def schedule_retry(
        self,
        attempt_count: int,
        *,
        now: datetime,
        reason: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        headers: Optional[Mapping[str, Any]] = None,
    ) -> RetryDecision:
        retry_after_seconds = parse_retry_after(_header_value(headers, "retry-after"), now)
        delay = self.compute_delay_seconds(attempt_count, retry_after_seconds=retry_after_seconds)
        return RetryDecision(
            disposition=RetryDisposition.RETRY,
            reason=reason,
            error_code=error_code,
            error_message=error_message,
            retry_at=now + timedelta(seconds=delay),
            retry_after_seconds=retry_after_seconds,
            headers={str(key): str(value) for key, value in (headers or {}).items()},
            details={"delay_seconds": delay},
        )

    def classify_failure(
        self,
        failure: Any,
        *,
        attempt_count: int,
        max_attempts: int,
        now: datetime,
        headers: Optional[Mapping[str, Any]] = None,
    ) -> RetryDecision:
        if isinstance(failure, UserCancelledError):
            return RetryDecision(
                disposition=RetryDisposition.CANCEL,
                reason="cancelled_by_operator",
                error_code="cancelled",
                error_message=str(failure),
                headers={str(key): str(value) for key, value in (headers or {}).items()},
            )

        if isinstance(failure, ConfigurationError):
            return RetryDecision(
                disposition=RetryDisposition.FAIL,
                reason="missing_or_invalid_configuration",
                error_code="configuration_error",
                error_message=str(failure),
                headers={str(key): str(value) for key, value in (headers or {}).items()},
            )

        if isinstance(failure, PermanentBackendError):
            return RetryDecision(
                disposition=RetryDisposition.FAIL,
                reason="permanent_backend_error",
                error_code="permanent_backend_error",
                error_message=str(failure),
                headers={str(key): str(value) for key, value in (headers or {}).items()},
            )

        http_failure = failure if isinstance(failure, HttpFailure) else None
        merged_headers = headers or (http_failure.headers if http_failure else {})
        if http_failure:
            decision = self._classify_http_failure(
                http_failure,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                now=now,
            )
            if decision.disposition == RetryDisposition.RETRY and attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code=decision.error_code,
                    error_message=decision.error_message,
                    headers=decision.headers,
                    details=decision.details,
                )
            return decision

        if _is_transient_transport_error(failure):
            if attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code="transient_transport_failure",
                    error_message=str(failure),
                    headers={str(key): str(value) for key, value in merged_headers.items()},
                )
            return self.schedule_retry(
                attempt_count,
                now=now,
                reason="transient_transport_failure",
                error_code="transient_transport_failure",
                error_message=str(failure),
                headers=merged_headers,
            )

        if isinstance(failure, RetryableBackendError):
            if attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code="retryable_backend_error",
                    error_message=str(failure),
                    headers={str(key): str(value) for key, value in merged_headers.items()},
                )
            return self.schedule_retry(
                attempt_count,
                now=now,
                reason="retryable_backend_error",
                error_code="retryable_backend_error",
                error_message=str(failure),
                headers=merged_headers,
            )

        return RetryDecision(
            disposition=RetryDisposition.FAIL,
            reason="unclassified_failure",
            error_code="unclassified_failure",
            error_message=str(failure),
            headers={str(key): str(value) for key, value in merged_headers.items()},
        )

    def _classify_http_failure(
        self,
        failure: HttpFailure,
        *,
        attempt_count: int,
        max_attempts: int,
        now: datetime,
    ) -> RetryDecision:
        status_code = failure.status_code
        body = failure.body or {}
        headers = {str(key): str(value) for key, value in failure.headers.items()}
        error_type = (
            body.get("error", {}).get("type")
            or body.get("type")
            or body.get("error_type")
            or f"http_{status_code}"
        )
        error_message = (
            body.get("error", {}).get("message")
            or body.get("message")
            or failure.message
            or f"HTTP {status_code}"
        )

        if status_code == 429:
            if attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code=str(error_type),
                    error_message=error_message,
                    headers=headers,
                )
            return self.schedule_retry(
                attempt_count,
                now=now,
                reason="rate_limited",
                error_code=str(error_type),
                error_message=error_message,
                headers=headers,
            )

        if status_code in {500, 502, 503, 504}:
            if attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code=str(error_type),
                    error_message=error_message,
                    headers=headers,
                )
            return self.schedule_retry(
                attempt_count,
                now=now,
                reason="upstream_unavailable",
                error_code=str(error_type),
                error_message=error_message,
                headers=headers,
            )

        if status_code in {408, 409, 423}:
            if attempt_count >= max_attempts:
                return RetryDecision(
                    disposition=RetryDisposition.FAIL,
                    reason="max_attempts_exhausted",
                    error_code=str(error_type),
                    error_message=error_message,
                    headers=headers,
                )
            return self.schedule_retry(
                attempt_count,
                now=now,
                reason="temporary_conflict",
                error_code=str(error_type),
                error_message=error_message,
                headers=headers,
            )

        if status_code in {400, 401, 403, 404, 422}:
            return RetryDecision(
                disposition=RetryDisposition.FAIL,
                reason="invalid_request_or_auth",
                error_code=str(error_type),
                error_message=error_message,
                headers=headers,
            )

        return RetryDecision(
            disposition=RetryDisposition.FAIL,
            reason="unexpected_http_failure",
            error_code=str(error_type),
            error_message=error_message,
            headers=headers,
        )


def _header_value(headers: Optional[Mapping[str, Any]], name: str) -> Optional[str]:
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _is_transient_transport_error(error: Any) -> bool:
    name = error.__class__.__name__.lower()
    module = error.__class__.__module__.lower()
    message = str(error).lower()
    if "timeout" in name or "timeout" in module:
        return True
    if "connect" in name and "error" in name:
        return True
    if "read" in name and "timeout" in name:
        return True
    if "database is locked" in message or "resource temporarily unavailable" in message:
        return True
    if isinstance(error, BlockingIOError):
        return True
    if isinstance(error, TimeoutError):
        return True
    return False

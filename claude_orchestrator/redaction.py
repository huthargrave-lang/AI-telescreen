"""Secret-aware redaction for logs, events, and persisted payload summaries."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, Mapping

DEFAULT_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_API_KEY",
    "CLAUDE_ORCHESTRATOR_ENCRYPTION_KEY",
)
REDACTION_TOKEN = "[redacted]"

_SECRET_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]+\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-_\.]+\b", re.IGNORECASE),
)


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Best-effort redaction for known secret values and common token shapes."""

    result = text
    secrets = set(extra_secrets)
    for env_name in DEFAULT_SECRET_ENV_NAMES:
        env_value = os.getenv(env_name)
        if env_value:
            secrets.add(env_value)
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        result = result.replace(secret, REDACTION_TOKEN)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(REDACTION_TOKEN, result)
    return result


def redact_value(value: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Recursively redact secrets from nested JSON-like values."""

    if isinstance(value, str):
        return redact_text(value, extra_secrets=extra_secrets)
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, extra_secrets=extra_secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, extra_secrets=extra_secrets) for item in value]
    return value


def redact_mapping(value: Mapping[str, Any], extra_secrets: Iterable[str] = ()) -> Dict[str, Any]:
    """Typed helper for mappings."""

    return dict(redact_value(value, extra_secrets=extra_secrets))

"""Provider inference and validation helpers for provider/backend separation."""

from __future__ import annotations

from .models import ProviderName


BACKEND_PROVIDER_MAP = {
    "messages_api": ProviderName.ANTHROPIC.value,
    "message_batches": ProviderName.ANTHROPIC.value,
    "agent_sdk": ProviderName.ANTHROPIC.value,
    "claude_code_cli": ProviderName.ANTHROPIC.value,
    "codex_cli": ProviderName.OPENAI.value,
}


def infer_provider(backend: str) -> str:
    """Infer the provider implied by a configured backend name."""

    try:
        return BACKEND_PROVIDER_MAP[backend]
    except KeyError as exc:
        raise ValueError(f"Unknown provider for backend: {backend}") from exc


def validate_provider_backend(provider: str, backend: str) -> str:
    """Validate a provider/backend pairing and return the normalized provider."""

    normalized_provider = provider.strip().lower()
    expected_provider = infer_provider(backend)
    if normalized_provider != expected_provider:
        raise ValueError(
            f"Backend {backend!r} requires provider {expected_provider!r}, got {normalized_provider!r}."
        )
    return normalized_provider

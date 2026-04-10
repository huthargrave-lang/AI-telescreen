"""Compatibility helpers for slightly older local interpreters."""

from __future__ import annotations

try:  # pragma: no cover - exercised implicitly per runtime.
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - only used on <3.11.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover - import error surfaced by caller.
        raise RuntimeError(
            "TOML support requires Python 3.11+ or the optional 'tomli' package."
        ) from exc

__all__ = ["tomllib"]

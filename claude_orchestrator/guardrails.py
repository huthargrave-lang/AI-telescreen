"""Compliance guardrails that keep this project on official Anthropic surfaces only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

RATIONALE = (
    "claude-orchestrator is intentionally limited to official Anthropic interfaces. "
    "It does not scrape Claude.ai, automate the consumer website, inspect browser "
    "storage, or rely on reverse-engineered private endpoints."
)


@dataclass(frozen=True)
class GuardrailViolation:
    pattern: str
    reason: str


FORBIDDEN_PATTERNS: Sequence[GuardrailViolation] = (
    GuardrailViolation(r"claude\.ai", "Consumer website references are disallowed in implementation flows."),
    GuardrailViolation(r"playwright", "Browser automation against Claude.ai is forbidden."),
    GuardrailViolation(r"puppeteer", "Browser automation against Claude.ai is forbidden."),
    GuardrailViolation(r"selenium", "Browser automation against Claude.ai is forbidden."),
    GuardrailViolation(r"cookie", "Consumer-site cookie reuse is forbidden."),
    GuardrailViolation(r"localstorage", "Consumer-site storage reuse is forbidden."),
    GuardrailViolation(r"session[-_ ]limit", "Consumer session counter inspection is out of scope."),
    GuardrailViolation(r"devtools", "Browser devtools interception is forbidden."),
    GuardrailViolation(r"intercept(?:ing)? private", "Private endpoint interception is forbidden."),
)


def find_violations(text: str) -> List[GuardrailViolation]:
    """Return forbidden-pattern matches in the supplied text."""

    lowered = text.lower()
    return [rule for rule in FORBIDDEN_PATTERNS if re.search(rule.pattern, lowered)]


def assert_compliant_text(text: str) -> None:
    """Raise when text suggests a forbidden implementation direction."""

    violations = find_violations(text)
    if violations:
        joined = "; ".join(violation.reason for violation in violations)
        raise ValueError(f"Guardrail violation detected: {joined}")


def scan_paths(paths: Iterable[Path]) -> List[str]:
    """Scan files for forbidden language."""

    findings: List[str] = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        violations = find_violations(content)
        if violations:
            reasons = ", ".join(sorted({violation.reason for violation in violations}))
            findings.append(f"{path}: {reasons}")
    return findings

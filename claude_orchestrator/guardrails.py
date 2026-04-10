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
    GuardrailViolation(
        r"(?:^|\n)\s*(?:from|import)\s+playwright\b|playwright\.(?:sync_api|async_api)|page\.goto\([^)]*claude\.ai",
        "Browser automation against Claude.ai is forbidden.",
    ),
    GuardrailViolation(
        r"(?:^|\n)\s*(?:from|import)\s+selenium\b|webdriver\.[A-Za-z_]+|selenium\.webdriver",
        "Browser automation against Claude.ai is forbidden.",
    ),
    GuardrailViolation(
        r"puppeteer(?:\.launch|\s*=|require\()|page\.goto\([^)]*claude\.ai",
        "Browser automation against Claude.ai is forbidden.",
    ),
    GuardrailViolation(
        r"(?:requests|httpx)\.(?:get|post|put|patch|delete)\([^)]*claude\.ai|client\.(?:get|post|put|patch|delete)\([^)]*claude\.ai",
        "Direct HTTP access to the Claude consumer site is forbidden.",
    ),
    GuardrailViolation(
        r"(?:document\.cookie|browser_cookie3|cookiejar|localstorage|sessionstorage)",
        "Consumer-site credential or storage reuse is forbidden.",
    ),
    GuardrailViolation(
        r"(?:devtools|cdp|network\.intercept|chrome\.debugger|intercept(?:ing)? private)",
        "Browser interception and private endpoint reverse engineering are forbidden.",
    ),
    GuardrailViolation(
        r"(?:scrape|read|watch|inspect|parse)[^\n]{0,80}session[-_ ]limit|session[-_ ]limit[^\n]{0,80}(?:scrape|read|watch|inspect|parse)",
        "Consumer session counter inspection is out of scope.",
    ),
)


def find_violations(text: str) -> List[GuardrailViolation]:
    """Return forbidden-pattern matches in the supplied text."""

    lowered = _executable_text(text.lower())
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


def _executable_text(text: str) -> str:
    """Reduce false positives by ignoring obviously narrative or comment-only lines."""

    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//", "*", "-", ">", '"""', "'''")):
            continue
        lines.append(raw_line)
    return "\n".join(lines)

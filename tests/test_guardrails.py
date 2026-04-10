from __future__ import annotations

import pytest

from claude_orchestrator.guardrails import assert_compliant_text, find_violations


def test_guardrails_flag_consumer_site_automation_language():
    violations = find_violations(
        'from playwright.async_api import async_playwright\nawait page.goto("https://claude.ai")'
    )
    reasons = {violation.reason for violation in violations}
    assert any("Browser automation" in reason for reason in reasons)
    assert len(reasons) >= 1


def test_guardrails_allow_official_api_language():
    assert_compliant_text("Use the Messages API, Message Batches API, and documented CLI integrations only.")


def test_guardrails_raise_on_forbidden_text():
    with pytest.raises(ValueError):
        assert_compliant_text('cookies = document.cookie\npage.goto("https://claude.ai")')


def test_guardrails_ignore_narrative_commentary_mentions():
    assert_compliant_text("# Do not use Playwright against Claude.ai")

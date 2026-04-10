from __future__ import annotations

import pytest

from claude_orchestrator.guardrails import assert_compliant_text, find_violations


def test_guardrails_flag_consumer_site_automation_language():
    violations = find_violations("Use Playwright to inspect Claude.ai cookies and session-limit counters.")
    reasons = {violation.reason for violation in violations}
    assert any("Browser automation" in reason for reason in reasons)
    assert any("Consumer website references" in reason for reason in reasons)


def test_guardrails_allow_official_api_language():
    assert_compliant_text("Use the Messages API, Message Batches API, and documented CLI integrations only.")


def test_guardrails_raise_on_forbidden_text():
    with pytest.raises(ValueError):
        assert_compliant_text("Read localStorage from Claude.ai and replay cookies.")

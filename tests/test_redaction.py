from __future__ import annotations

import logging

from claude_orchestrator.logging_utils import JsonFormatter


def test_json_logs_redact_environment_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-123")
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="using %s",
        args=("sk-ant-secret-123",),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert "sk-ant-secret-123" not in rendered
    assert "[redacted]" in rendered

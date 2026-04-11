from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from claude_orchestrator.backends.claude_code_cli import ClaudeCodeCliBackend
from claude_orchestrator.config import AppConfig
from claude_orchestrator.integrations import IntegrationCapability, WorkspaceIntegrationSummary
from claude_orchestrator.models import ConversationState, Job, JobStatus, ProviderName
from claude_orchestrator.retry import ConfigurationError, PermanentBackendError
from claude_orchestrator.timeutils import utcnow


class FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self._returncode = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.killed = False

    async def wait(self) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True


def _job() -> Job:
    now = utcnow()
    return Job(
        id="cli-job",
        created_at=now,
        updated_at=now,
        status=JobStatus.QUEUED,
        provider=ProviderName.ANTHROPIC.value,
        backend="claude_code_cli",
        task_type="code",
        priority=0,
        prompt="hello",
        metadata={},
        attempt_count=0,
        next_retry_at=None,
        last_error_code=None,
        last_error_message=None,
        max_attempts=5,
        idempotency_key=None,
    )


def _context(tmp_path):
    config = AppConfig()
    config.backends.claude_code_cli.enabled = True
    config.backends.claude_code_cli.executable = Path(sys.executable).name
    config.backends.claude_code_cli.command_template = ["{executable}", "{prompt_file}"]
    return config, type(
        "Context",
        (),
        {"config": config, "workspace_root": tmp_path / "workspaces", "worker_id": "worker-1"},
    )()


def test_cli_backend_rejects_unapproved_hooks(tmp_path):
    config, context = _context(tmp_path)
    config.backends.claude_code_cli.hook_commands = [f"{Path(sys.executable).name} -c pass"]
    backend = ClaudeCodeCliBackend(config)

    with pytest.raises(ConfigurationError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="cli-job", system_prompt=None), context))


def test_cli_backend_classifies_auth_failure(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = ClaudeCodeCliBackend(config)

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=1, stderr=b"authentication token invalid")

    monkeypatch.setattr("claude_orchestrator.backends.claude_code_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(PermanentBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="cli-job", system_prompt=None), context))


def test_cli_backend_truncates_large_output(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    config.backends.claude_code_cli.max_output_bytes = 16
    backend = ClaudeCodeCliBackend(config)

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=0, stdout=b"x" * 64, stderr=b"y" * 32)

    monkeypatch.setattr("claude_orchestrator.backends.claude_code_cli.asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(backend.submit(_job(), ConversationState(job_id="cli-job", system_prompt=None), context))

    assert result.response_summary["stdout_truncated"] is True
    assert result.artifacts[0].metadata["stderr_truncated"] is True


def test_cli_backend_emits_integration_context_event(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = ClaudeCodeCliBackend(config)
    events = []
    context.emit_stream_event = lambda event_type, phase, message, metadata: events.append(
        (event_type, phase, message, metadata)
    )
    context.integration_summary = WorkspaceIntegrationSummary(
        workspace_path=str((tmp_path / "workspaces" / "cli-job").resolve()),
        repo_path=None,
        capabilities=[
            IntegrationCapability(
                name="github",
                source="project_mcp",
                kind="github_tool",
            )
        ],
        config_paths=[str((tmp_path / ".claude" / "settings.json").resolve())],
        notes=[],
    )

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=0, stdout=b"ok")

    monkeypatch.setattr("claude_orchestrator.backends.claude_code_cli.asyncio.create_subprocess_exec", fake_exec)

    asyncio.run(backend.submit(_job(), ConversationState(job_id="cli-job", system_prompt=None), context))

    assert any(event[0] == "integration_context_loaded" for event in events)

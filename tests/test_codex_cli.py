from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from claude_orchestrator.backends.codex_cli import CodexCliBackend
from claude_orchestrator.config import AppConfig
from claude_orchestrator.models import ConversationState, Job, JobStatus, ProviderName, RetryDisposition
from claude_orchestrator.retry import PermanentBackendError, RetryableBackendError
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
        id="codex-job",
        created_at=now,
        updated_at=now,
        status=JobStatus.QUEUED,
        provider=ProviderName.OPENAI.value,
        backend="codex_cli",
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
    config.backends.codex_cli.enabled = True
    config.backends.codex_cli.executable = Path(sys.executable).name
    config.backends.codex_cli.command_template = ["{executable}", "{prompt_file}"]
    return config, type(
        "Context",
        (),
        {"config": config, "workspace_root": tmp_path / "workspaces", "worker_id": "worker-1"},
    )()


def test_codex_cli_backend_success(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = CodexCliBackend(config)

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=0, stdout=b"done", stderr=b"warning")

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))

    assert result.status == JobStatus.COMPLETED
    assert result.output == "done"
    assert result.artifacts[0].kind == "codex_cli_output"
    assert result.request_payload["provider"] == ProviderName.OPENAI.value


def test_codex_cli_backend_auth_failure(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = CodexCliBackend(config)

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=1, stderr=b"authentication failed: api key invalid")

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(PermanentBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))


def test_codex_cli_backend_transient_failure(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = CodexCliBackend(config)

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=1, stderr=b"temporarily unavailable, try again later")

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RetryableBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))


def test_codex_cli_backend_classifies_retryable_error(tmp_path):
    config, _ = _context(tmp_path)
    backend = CodexCliBackend(config)

    decision = backend.classify_error(RetryableBackendError("slow down"))

    assert decision.disposition == RetryDisposition.RETRY

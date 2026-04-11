from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_orchestrator.backends.codex_cli import CodexCliBackend
from claude_orchestrator.config import AppConfig
from claude_orchestrator.db import Database
from claude_orchestrator.models import ConversationState, EnqueueJobRequest, Job, JobStatus, ProviderName, RetryDisposition
from claude_orchestrator.repository import JobRepository
from claude_orchestrator.retry import PermanentBackendError, RetryableBackendError
from claude_orchestrator.services.orchestrator import OrchestratorService
from claude_orchestrator.services.worker import WorkerService
from claude_orchestrator.timeutils import utcnow


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout_chunks: list[bytes] | None = None,
        stderr_chunks: list[bytes] | None = None,
        delay: float = 0.0,
        wait_forever: bool = False,
    ) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.pid = 4321
        self.returncode: int | None = None
        self._returncode = returncode
        self._stdout_chunks = stdout_chunks or []
        self._stderr_chunks = stderr_chunks or []
        self._delay = delay
        self._wait_forever = wait_forever
        self._done = asyncio.Event()
        self.killed = False
        self.terminated = False
        self._feed_task = asyncio.create_task(self._feed())

    async def _feed(self) -> None:
        try:
            for chunk in self._stdout_chunks:
                self.stdout.feed_data(chunk)
                if self._delay:
                    await asyncio.sleep(self._delay)
            for chunk in self._stderr_chunks:
                self.stderr.feed_data(chunk)
                if self._delay:
                    await asyncio.sleep(self._delay)
            if self._wait_forever:
                return
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = self._returncode
            self._done.set()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            raise

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode if self.returncode is not None else self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._feed_task.cancel()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._feed_task.cancel()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()


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


def _context(tmp_path, *, emit_stream_event=None):
    config = AppConfig()
    config.backends.codex_cli.enabled = True
    config.backends.codex_cli.executable = "codex"
    config.backends.codex_cli.args = []
    return config, SimpleNamespace(
        config=config,
        workspace_root=tmp_path / "workspaces",
        worker_id="worker-1",
        emit_stream_event=emit_stream_event,
    )


def _build_codex_orchestrator(tmp_path: Path) -> tuple[OrchestratorService, JobRepository, AppConfig]:
    config = AppConfig()
    config.storage.sqlite_path = str(tmp_path / "claude-orchestrator.db")
    config.logging.json_log_path = str(tmp_path / "logs" / "orchestrator.jsonl")
    config.workspace_root = str(tmp_path / "workspaces")
    config.privacy.store_response_artifacts = True
    config.backends.codex_cli.enabled = True
    config.backends.codex_cli.executable = "codex"
    config.backends.codex_cli.args = []
    db = Database(Path(config.storage.sqlite_path))
    db.initialize()
    repository = JobRepository(db)
    backend = CodexCliBackend(config)
    orchestrator = OrchestratorService(
        root=tmp_path,
        config=config,
        repository=repository,
        backends={"codex_cli": backend},
    )
    return orchestrator, repository, config


def test_codex_cli_backend_uses_exec_with_direct_prompt_invocation(monkeypatch, tmp_path):
    emitted_events: list[tuple[str, str | None, str]] = []
    captured: dict[str, object] = {}
    config, context = _context(
        tmp_path,
        emit_stream_event=lambda event_type, phase, message, metadata: emitted_events.append(
            (event_type, phase, message)
        ),
    )
    backend = CodexCliBackend(config)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        return FakeProcess(returncode=0, stdout_chunks=[b"planning step\n", b"done\n"], stderr_chunks=[b"warning\n"])

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))

    assert result.status == JobStatus.COMPLETED
    assert result.output == "planning step\ndone"
    assert result.artifacts[0].kind == "codex_cli_output"
    assert result.request_payload["provider"] == ProviderName.OPENAI.value
    assert result.request_payload["prompt_delivery"] == "argv"
    assert captured["args"] == ["/usr/bin/codex", "exec", "hello"]
    assert "--workspace" not in captured["args"]
    assert "--prompt-file" not in captured["args"]
    assert captured["cwd"] == str((tmp_path / "workspaces" / "codex-job").resolve())
    assert any(event_type == "process_started" for event_type, _, _ in emitted_events)
    assert any(event_type == "stdout_line" for event_type, _, _ in emitted_events)
    assert any(event_type == "process_completed" for event_type, _, _ in emitted_events)


def test_codex_cli_backend_auth_failure(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = CodexCliBackend(config)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=1, stderr_chunks=[b"authentication failed: api key invalid\n"])

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(PermanentBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))


def test_codex_cli_backend_transient_failure(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    backend = CodexCliBackend(config)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        return FakeProcess(returncode=1, stderr_chunks=[b"temporarily unavailable, try again later\n"])

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RetryableBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))


def test_codex_cli_backend_classifies_retryable_error(tmp_path):
    config, _ = _context(tmp_path)
    backend = CodexCliBackend(config)

    decision = backend.classify_error(RetryableBackendError("slow down"))

    assert decision.disposition == RetryDisposition.RETRY


def test_codex_cli_backend_timeout(monkeypatch, tmp_path):
    config, context = _context(tmp_path)
    config.backends.codex_cli.timeout_seconds = 0.01
    backend = CodexCliBackend(config)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        return FakeProcess(wait_forever=True)

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(RetryableBackendError):
        asyncio.run(backend.submit(_job(), ConversationState(job_id="codex-job", system_prompt=None), context))


def test_codex_cli_stream_events_are_persisted(monkeypatch, tmp_path):
    orchestrator, repository, config = _build_codex_orchestrator(tmp_path)
    config.worker.lease_seconds = 0.1
    config.worker.heartbeat_interval_seconds = 0.02
    job = orchestrator.enqueue(
        EnqueueJobRequest(
            backend="codex_cli",
            task_type="code",
            prompt="inspect repo",
        )
    )
    claimed = repository.claim_job(job.id, "worker-1", lease_seconds=config.effective_lease_seconds())
    assert claimed is not None
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        return FakeProcess(
            returncode=0,
            stdout_chunks=[b"planning task\n", b"editing files\n", b"running tests\n", b"done\n"],
            delay=0.03,
        )

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    async def scenario():
        task = asyncio.create_task(orchestrator.process_claimed_job(claimed, "worker-1"))
        await asyncio.sleep(0.16)
        assert orchestrator.recover_stale_jobs() == []
        await task

    asyncio.run(scenario())

    events = repository.get_stream_events(job.id)
    event_types = [event.event_type for event in events]
    assert "process_started" in event_types
    assert "stdout_line" in event_types
    assert "phase_changed" in event_types
    assert "process_completed" in event_types
    assert repository.get_job(job.id).status == JobStatus.COMPLETED


def test_worker_shutdown_interrupts_active_codex_job(monkeypatch, tmp_path):
    orchestrator, repository, config = _build_codex_orchestrator(tmp_path)
    config.worker.max_concurrency = 1
    config.worker.poll_interval_seconds = 0.01
    config.worker.shutdown_drain_timeout_seconds = 0.05
    config.worker.shutdown_poll_interval_seconds = 0.01
    config.worker.lease_seconds = 0.1
    config.worker.heartbeat_interval_seconds = 0.02
    job = orchestrator.enqueue(
        EnqueueJobRequest(
            backend="codex_cli",
            task_type="code",
            prompt="inspect repo",
        )
    )
    process_holder: dict[str, FakeProcess] = {}
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        process = FakeProcess(
            stdout_chunks=[b"planning task\n"],
            wait_forever=True,
        )
        process_holder["process"] = process
        return process

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)
    worker = WorkerService(repository, orchestrator)

    async def scenario():
        task = asyncio.create_task(worker.run_forever())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if repository.get_stream_events(job.id):
                break
        worker.shutdown()
        await task

    asyncio.run(scenario())

    updated = repository.get_job(job.id)
    stream_events = repository.get_stream_events(job.id)

    assert process_holder["process"].terminated is True or process_holder["process"].killed is True
    assert updated.status == JobStatus.WAITING_RETRY
    assert any(event.event_type == "process_interrupted" for event in stream_events)

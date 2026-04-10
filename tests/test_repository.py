from __future__ import annotations

import sqlite3
from pathlib import Path

from claude_orchestrator.db import Database
from claude_orchestrator.models import ProviderName
from claude_orchestrator.providers import infer_provider, validate_provider_backend
from claude_orchestrator.repository import JobRepository
from tests.helpers import build_test_orchestrator, create_job


def test_sqlite_persistence_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    created = create_job(orchestrator, prompt="persist me")
    loaded = repository.get_job(created.id)
    state = repository.get_conversation_state(created.id)

    assert loaded.prompt == "persist me"
    assert state.job_id == created.id
    assert state.message_history == []


def test_provider_round_trip_and_inference(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path, backends={"codex_cli": object()})
    created = create_job(orchestrator, backend="codex_cli", prompt="ship it")
    run_id = repository.record_run_start(created, request_payload={"step": "test"})
    repository.finish_run(
        run_id,
        request_payload={"step": "test"},
        response_summary={"ok": True},
        usage={},
        headers={},
        error={},
        exit_reason="completed",
    )

    loaded = repository.get_job(created.id)
    run = repository.get_job_runs(created.id)[0]

    assert loaded.provider == ProviderName.OPENAI.value
    assert run.provider == ProviderName.OPENAI.value
    assert infer_provider("messages_api") == ProviderName.ANTHROPIC.value
    assert infer_provider("codex_cli") == ProviderName.OPENAI.value
    assert validate_provider_backend("openai", "codex_cli") == ProviderName.OPENAI.value


def test_provider_backend_validation_rejects_mismatch():
    try:
        validate_provider_backend("anthropic", "codex_cli")
    except ValueError as exc:
        assert "requires provider" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected provider/backend validation to fail.")


def test_duplicate_execution_is_prevented(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    job = create_job(orchestrator)

    first_claim = repository.claim_job(job.id, "worker-1")
    second_claim = repository.claim_job(job.id, "worker-2")

    assert first_claim is not None
    assert second_claim is None
    assert repository.get_job(job.id).attempt_count == 1


def test_lease_renewal_requires_current_owner(tmp_path):
    orchestrator, repository, config = build_test_orchestrator(tmp_path)
    config.worker.lease_seconds = 5
    job = create_job(orchestrator)

    claimed = repository.claim_job(job.id, "worker-1", lease_seconds=config.effective_lease_seconds())
    assert claimed is not None

    assert repository.renew_job_lease(job.id, "worker-2", lease_seconds=config.effective_lease_seconds()) is None
    assert repository.renew_job_lease(job.id, "worker-1", lease_seconds=config.effective_lease_seconds()) is not None
    assert repository.renew_lease(job.id, "worker-1", lease_seconds=config.effective_lease_seconds()) is True


def test_stream_event_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    job = create_job(orchestrator)

    repository.add_stream_event(
        job_id=job.id,
        provider=job.provider,
        backend=job.backend,
        event_type="stdout_line",
        phase="planning",
        message="Planning next step.",
        metadata={"source": "stdout"},
    )

    events = repository.get_stream_events(job.id)
    latest = repository.get_latest_stream_event(job.id)

    assert len(events) == 1
    assert events[0].phase == "planning"
    assert latest is not None
    assert latest.message == "Planning next step."


def test_migration_adds_provider_columns_with_safe_defaults(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript((Path(__file__).resolve().parents[1] / "claude_orchestrator" / "migrations" / "0001_initial.sql").read_text())
    connection.execute(
        """
        INSERT INTO jobs (
            id, created_at, updated_at, status, backend, task_type, priority, prompt, metadata_json,
            attempt_count, next_retry_at, last_error_code, last_error_message, max_attempts, idempotency_key,
            model, workspace_path, cancel_requested, lease_owner, lease_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-job",
            "2026-04-10T00:00:00Z",
            "2026-04-10T00:00:00Z",
            "queued",
            "messages_api",
            "general",
            0,
            "hello",
            "{}",
            0,
            None,
            None,
            None,
            5,
            None,
            None,
            None,
            0,
            None,
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO job_runs (
            id, job_id, started_at, ended_at, backend, request_payload_json, response_summary_json,
            usage_json, headers_json, error_json, exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-run",
            "legacy-job",
            "2026-04-10T00:00:00Z",
            None,
            "messages_api",
            "{}",
            "{}",
            "{}",
            "{}",
            "{}",
            None,
        ),
    )
    connection.commit()
    connection.close()

    db = Database(db_path)
    db.initialize()
    repository = JobRepository(db)

    job = repository.get_job("legacy-job")
    run = repository.get_job_runs("legacy-job")[0]

    assert job.provider == ProviderName.ANTHROPIC.value
    assert run.provider == ProviderName.ANTHROPIC.value

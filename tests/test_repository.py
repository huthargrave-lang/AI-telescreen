from __future__ import annotations

import sqlite3
from pathlib import Path

from claude_orchestrator.db import Database
from claude_orchestrator.integrations import IntegrationCapability, WorkspaceIntegrationSummary
from claude_orchestrator.models import JobStatus, ProviderName
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


def test_workspace_integration_summary_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    job = create_job(orchestrator)
    summary = WorkspaceIntegrationSummary(
        workspace_path=job.workspace_path or "",
        repo_path=None,
        capabilities=[
            IntegrationCapability(
                name="github",
                source="project_mcp",
                kind="github_tool",
                metadata={"command": "npx"},
            )
        ],
        config_paths=[str((tmp_path / ".claude" / "settings.json").resolve())],
        notes=["project config discovered"],
    )

    assert repository.save_workspace_integration_summary(summary, job_id=job.id) is True
    loaded = repository.get_workspace_integration_summary_for_job(job.id)

    assert loaded is not None
    assert loaded.status() == "integration-enabled"
    assert loaded.capabilities[0].name == "github"
    assert repository.save_workspace_integration_summary(summary, job_id=job.id) is False


def test_saved_project_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)

    project = orchestrator.create_saved_project(
        name="AI Telescreen Repo",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=True,
        notes="primary repo",
    )

    loaded = repository.get_saved_project(project.id)
    projects = repository.list_saved_projects()

    assert loaded.name == "AI Telescreen Repo"
    assert loaded.default_use_git_worktree is True
    assert projects[0].id == project.id


def test_saved_project_update_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Editable Repo",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="before",
    )

    updated = orchestrator.update_saved_project(
        project.id,
        name="Edited Repo",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="release",
        default_use_git_worktree=True,
        notes="after",
    )

    loaded = repository.get_saved_project(project.id)

    assert updated.name == "Edited Repo"
    assert loaded.default_base_branch == "release"
    assert loaded.default_use_git_worktree is True
    assert loaded.notes == "after"


def test_launch_job_from_project_and_duplicate_job(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Launch Repo",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )

    launched = orchestrator.launch_job_from_project(
        project.id,
        prompt="ship it",
        task_type="code",
        priority=7,
        max_attempts=3,
    )
    duplicate = orchestrator.duplicate_job(launched.id)

    assert launched.metadata["project_id"] == project.id
    assert launched.metadata["repo_path"] == str(tmp_path.resolve())
    assert launched.priority == 7
    assert launched.max_attempts == 3
    assert duplicate.id != launched.id
    assert duplicate.metadata["project_id"] == project.id
    assert duplicate.metadata["repo_path"] == str(tmp_path.resolve())
    assert repository.list_project_jobs(project.id, limit=10)[0].id == duplicate.id
    assert repository.get_job(duplicate.id).status == JobStatus.QUEUED


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

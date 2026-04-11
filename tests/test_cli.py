from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_orchestrator.bootstrap import bootstrap
from claude_orchestrator.cli import app
from claude_orchestrator.integrations import (
    IntegrationCapability,
    WorkspaceIntegrationSummary,
    build_integration_metadata_summary,
)
from claude_orchestrator.models import EnqueueJobRequest
from claude_orchestrator.services.orchestrator import OrchestratorService

typer_testing = pytest.importorskip("typer.testing")
CliRunner = typer_testing.CliRunner


def test_cli_inspect_shows_workspace_and_stream_metadata(tmp_path, monkeypatch):
    runner = CliRunner()
    config_path = tmp_path / "claude-orchestrator.toml"
    config_path.write_text(
        "\n".join(
            [
                'default_backend = "claude_code_cli"',
                "",
                "[storage]",
                'sqlite_path = "data/claude-orchestrator.db"',
                "",
                "[backends.claude_code_cli]",
                "enabled = true",
                f'executable = "{Path(sys.executable).name}"',
                'command_template = ["{executable}", "{prompt_file}"]',
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    context = bootstrap(tmp_path, config_path=config_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    job = orchestrator.enqueue(
        EnqueueJobRequest(
            backend="claude_code_cli",
            task_type="code",
            prompt="inspect cli",
            metadata={"cleanup_policy": "none"},
        )
    )
    summary = WorkspaceIntegrationSummary(
        workspace_path=job.workspace_path or "",
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
    metadata = dict(context.repository.get_job(job.id).metadata)
    metadata["integration_summary"] = build_integration_metadata_summary(summary)
    context.repository.set_job_metadata(job.id, metadata)
    context.repository.save_workspace_integration_summary(summary, job_id=job.id)
    context.repository.add_stream_event(
        job_id=job.id,
        provider=job.provider,
        backend=job.backend,
        event_type="phase_changed",
        phase="planning",
        message="Planning repository edits.",
        metadata={"source": "stdout"},
    )

    result = runner.invoke(app, ["inspect", job.id, "--config", str(config_path)])

    assert result.exit_code == 0
    assert "workspace_kind:" in result.output
    assert "latest_phase:" in result.output
    assert "integration_status:" in result.output
    assert "integration_capabilities:" in result.output
    assert "github [github_tool]" in result.output
    assert "recent stream events:" in result.output

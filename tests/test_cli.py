from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_orchestrator.bootstrap import bootstrap
from claude_orchestrator.cli import app
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
                'default_backend = "messages_api"',
                "",
                "[storage]",
                'sqlite_path = "data/claude-orchestrator.db"',
                "",
                "[backends.codex_cli]",
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
            backend="codex_cli",
            task_type="code",
            prompt="inspect cli",
            metadata={"cleanup_policy": "none"},
        )
    )
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
    assert "recent stream events:" in result.output

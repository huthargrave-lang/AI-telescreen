from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_orchestrator.bootstrap import bootstrap
from claude_orchestrator.models import EnqueueJobRequest
from claude_orchestrator.services.orchestrator import OrchestratorService
from claude_orchestrator.web.app import build_app

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")
TestClient = testclient.TestClient


def test_web_app_loads_templates_outside_repo_root(tmp_path, monkeypatch):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    app = build_app(root=tmp_path)
    client = TestClient(app)

    dashboard = client.get("/")
    status = client.get("/api/status")

    assert dashboard.status_code == 200
    assert "claude-orchestrator" in dashboard.text
    assert status.status_code == 200
    assert "counts" in status.json()


def test_web_app_renders_provider_and_backend_filters(tmp_path):
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
            prompt="inspect repo",
        )
    )
    context.repository.add_stream_event(
        job_id=job.id,
        provider=job.provider,
        backend=job.backend,
        event_type="phase_changed",
        phase="planning",
        message="Planning repository analysis.",
        metadata={"source": "stdout"},
    )

    app = build_app(root=tmp_path, config_path=config_path)
    client = TestClient(app)

    dashboard = client.get("/")
    filtered = client.get("/api/jobs", params={"provider": "openai"})
    detail = client.get(f"/api/jobs/{filtered.json()['jobs'][0]['id']}")
    stream = client.get(f"/api/jobs/{filtered.json()['jobs'][0]['id']}/stream-events")
    html_detail = client.get(f"/jobs/{filtered.json()['jobs'][0]['id']}")

    assert dashboard.status_code == 200
    assert "openai" in dashboard.text
    assert "codex_cli" in dashboard.text
    assert filtered.status_code == 200
    assert filtered.json()["jobs"][0]["provider"] == "openai"
    assert detail.status_code == 200
    assert "stream_events" in detail.json()
    assert stream.status_code == 200
    assert html_detail.status_code == 200
    assert "Live Activity" in html_detail.text
    assert "planning" in html_detail.text

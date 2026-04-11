from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from claude_orchestrator.bootstrap import bootstrap
from claude_orchestrator.diagnostics import BackendSmokeTestResult, DiagnosticCheck, DiagnosticsReport
from claude_orchestrator.models import JobStatus
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
    assert "AI Telescreen" in dashboard.text
    assert "Create Job" in dashboard.text
    assert "Doctor" in dashboard.text
    assert "Process Due Jobs Once" in dashboard.text
    assert status.status_code == 200
    assert "counts" in status.json()


def test_web_create_job_retry_cancel_and_duplicate_flow(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created = client.post(
        "/jobs",
        data={
            "prompt": "inspect repo",
            "backend": "messages_api",
            "provider": "",
            "task_type": "code",
            "priority": "2",
            "max_attempts": "4",
            "repo_path": str(tmp_path),
            "base_branch": "main",
        },
        follow_redirects=False,
    )
    job_id = Path(urlparse(created.headers["location"]).path).name
    job = context.repository.get_job(job_id)
    claimed = context.repository.claim_job(job.id, "worker-web", lease_seconds=30)
    assert claimed is not None
    context.repository.update_job_status(
        job.id,
        target_status=JobStatus.WAITING_RETRY,
        current_status=JobStatus.RUNNING,
    )

    duplicated = client.post(f"/jobs/{job.id}/duplicate", follow_redirects=False)
    duplicate_job_id = Path(urlparse(duplicated.headers["location"]).path).name
    cancel_response = client.post(f"/jobs/{duplicate_job_id}/cancel", follow_redirects=False)
    retry_response = client.post(f"/jobs/{job.id}/retry", follow_redirects=False)
    dashboard = client.get("/")

    assert created.status_code == 303
    assert job.metadata["repo_path"] == str(tmp_path.resolve())
    assert job.metadata.get("use_git_worktree") is not True
    assert cancel_response.status_code == 303
    assert retry_response.status_code == 303
    assert context.repository.get_job(duplicate_job_id).status == JobStatus.CANCELLED
    assert context.repository.get_job(job.id).status == JobStatus.QUEUED
    assert "Duplicate" in dashboard.text
    assert "Create Job" in dashboard.text


def test_web_projects_create_launch_and_prefill(tmp_path):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Main Repo",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "notes": "operator default",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name

    new_job_page = client.get(f"/jobs/new?project_id={project_id}")
    launch = client.post(
        f"/projects/{project_id}/launch",
        data={"prompt": "launch from project", "task_type": "code", "priority": "1", "max_attempts": "3"},
        follow_redirects=False,
    )
    launched_job_id = Path(urlparse(launch.headers["location"]).path).name
    launched_job = context.repository.get_job(launched_job_id)
    projects_page = client.get("/projects")
    project_detail = client.get(f"/projects/{project_id}")

    assert created_project.status_code == 303
    assert 'value="messages_api"' in new_job_page.text
    assert f'value="{tmp_path.resolve()}"' in new_job_page.text
    assert "Prefilled from project" in new_job_page.text
    assert launch.status_code == 303
    assert launched_job.metadata["project_id"] == project_id
    assert launched_job.metadata["repo_path"] == str(tmp_path.resolve())
    assert launched_job.metadata.get("use_git_worktree") is not True
    assert projects_page.status_code == 200
    assert "Main Repo" in projects_page.text
    assert project_detail.status_code == 200
    assert "Launch Job" in project_detail.text


def test_web_doctor_page_and_api(tmp_path, monkeypatch):
    report = DiagnosticsReport(
        config_path=str(tmp_path / "claude-orchestrator.toml"),
        default_backend="codex_cli",
        enabled_backends=["codex_cli", "messages_api"],
        workspace_root=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "data" / "claude-orchestrator.db"),
        checks=[
            DiagnosticCheck(
                name="codex_cli",
                ok=True,
                status="configured",
                summary="Codex CLI is configured.",
                details={"resolved_executable": "/usr/bin/codex"},
            )
        ],
        warnings=["No coding-oriented backend is currently enabled."],
        smoke_tests={
            "codex_cli": BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=True,
                status="ok",
                summary="Codex smoke passed.",
                details={"version": "codex 1.2.3"},
            )
        },
    )

    monkeypatch.setattr(
        OrchestratorService,
        "collect_diagnostics",
        lambda self, run_smoke_tests=True: report,
    )

    app = build_app(root=tmp_path)
    client = TestClient(app)

    doctor = client.get("/doctor")
    doctor_api = client.get("/api/doctor")

    assert doctor.status_code == 200
    assert "Codex Smoke Test" in doctor.text
    assert "Codex smoke passed." in doctor.text
    assert doctor_api.status_code == 200
    assert doctor_api.json()["smoke_tests"]["codex_cli"]["status"] == "ok"


def test_web_project_edit_and_recent_launch_history(tmp_path):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Main Repo",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "notes": "before edit",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name

    edited = client.post(
        f"/projects/{project_id}/edit",
        data={
            "name": "Main Repo Edited",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "release",
            "notes": "after edit",
        },
        follow_redirects=False,
    )
    launched = client.post(
        f"/projects/{project_id}/launch",
        data={"prompt": "launch from edited project", "task_type": "code", "priority": "1", "max_attempts": "3"},
        follow_redirects=False,
    )
    launched_job_id = Path(urlparse(launched.headers["location"]).path).name
    detail = client.get(f"/projects/{project_id}")
    project = context.repository.get_saved_project(project_id)

    assert edited.status_code == 303
    assert launched.status_code == 303
    assert project.name == "Main Repo Edited"
    assert project.default_base_branch == "release"
    assert project.default_use_git_worktree is False
    assert "Recent Launches" in detail.text
    assert launched_job_id in detail.text
    assert "Edit Project" in detail.text

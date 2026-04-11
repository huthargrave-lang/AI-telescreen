from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from claude_orchestrator.bootstrap import bootstrap
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

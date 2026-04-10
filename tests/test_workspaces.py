from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from claude_orchestrator.models import EnqueueJobRequest
from claude_orchestrator.workspaces import cleanup_job_workspace, prepare_job_workspace
from tests.helpers import build_test_orchestrator


def test_prepare_job_workspace_directory_mode(tmp_path):
    result = prepare_job_workspace(tmp_path / "workspaces", "job-1", {"cleanup_policy": "none"})

    assert result.workspace_kind == "directory"
    assert result.workspace_path.exists()
    assert result.metadata_updates()["workspace_kind"] == "directory"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for worktree tests")
def test_prepare_job_workspace_git_worktree_and_cleanup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    result = prepare_job_workspace(
        tmp_path / "workspaces",
        "job-2",
        {
            "use_git_worktree": True,
            "repo_path": str(repo),
            "base_branch": "main",
            "cleanup_policy": "on_success",
        },
    )

    assert result.workspace_kind == "git_worktree"
    assert result.used_git_worktree is True
    assert result.worktree_path is not None and result.worktree_path.exists()
    assert result.branch_name is not None
    assert (result.worktree_path / "README.md").exists()

    cleanup = cleanup_job_workspace(
        tmp_path / "workspaces",
        result.metadata_updates(),
        outcome="completed",
    )

    assert cleanup.cleaned is True
    assert cleanup.workspace_kind == "git_worktree"
    assert not result.worktree_path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for worktree tests")
def test_enqueue_persists_worktree_metadata(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    orchestrator, repository, _ = build_test_orchestrator(tmp_path, backends={"codex_cli": object()})
    job = orchestrator.enqueue(
        EnqueueJobRequest(
            backend="codex_cli",
            task_type="code",
            prompt="worktree test",
            metadata={
                "use_git_worktree": True,
                "repo_path": str(repo),
                "base_branch": "main",
                "cleanup_policy": "none",
            },
        )
    )
    loaded = repository.get_job(job.id)

    assert loaded.workspace_path == loaded.metadata["workspace_path"]
    assert loaded.metadata["workspace_kind"] == "git_worktree"
    assert loaded.metadata["repo_path"] == str(repo.resolve())
    assert loaded.metadata["worktree_path"].endswith("worktree")
    assert loaded.metadata["branch_name"].startswith("claude-orchestrator/")

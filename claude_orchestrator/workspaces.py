"""Workspace and artifact helpers with path-safety checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from .models import Job


def ensure_within_root(root: Path, candidate: Path) -> Path:
    """Resolve a path and ensure it stays inside the configured root."""

    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_root not in resolved_candidate.parents and resolved_candidate != resolved_root:
        raise ValueError(f"Path escapes workspace root: {resolved_candidate}")
    return resolved_candidate


def ensure_job_workspace(workspace_root: Path, job_id: str) -> Path:
    """Create and return a dedicated per-job workspace."""

    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = workspace_root / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    return ensure_within_root(workspace_root, workspace)


def prepare_job_workspace(workspace_root: Path, job_id: str, metadata: Optional[dict[str, Any]] = None) -> Path:
    """Prepare a job workspace, leaving room for future worktree-backed flows.

    The current implementation keeps the existing per-job directory behavior. Metadata keys such as
    ``use_git_worktree``, ``repo_path``, ``worktree_path``, ``branch_name``, and ``base_branch`` are
    intentionally reserved so a future pass can add git worktree provisioning without changing the
    orchestrator-facing contract.
    """

    _ = metadata or {}
    return ensure_job_workspace(workspace_root, job_id)


def store_private_prompt(workspace: Path, prompt: str, artifact_dir: str) -> Path:
    """Persist a prompt outside SQLite when privacy mode is enabled."""

    directory = workspace / artifact_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "prompt.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def store_response_artifact(workspace: Path, content: str, name: str = "response.txt") -> Path:
    """Persist a response payload in the per-job workspace."""

    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / name
    path.write_text(content, encoding="utf-8")
    return path


def resolve_job_prompt(job: Job) -> str:
    """Return the real prompt, loading from a private artifact if needed."""

    prompt_artifact_path = job.metadata.get("prompt_artifact_path")
    if prompt_artifact_path:
        return Path(prompt_artifact_path).read_text(encoding="utf-8")
    return job.prompt


def prompt_fingerprint(prompt: str) -> str:
    """Stable grouping hash used for batching-compatible jobs."""

    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

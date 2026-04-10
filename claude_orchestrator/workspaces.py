"""Workspace lifecycle helpers with path safety and optional git worktree support."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .models import Job

WORKSPACE_MARKER_FILENAME = ".claude-orchestrator-workspace.json"
SUPPORTED_CLEANUP_POLICIES = {"none", "on_success", "on_completion"}
IGNORED_WORKTREE_PATHS = {
    WORKSPACE_MARKER_FILENAME,
    "codex_prompt.txt",
    "cli_prompt.txt",
    "codex_stdout.txt",
    "claude_code_stdout.txt",
    "response.txt",
    "private/prompt.txt",
}


@dataclass
class WorkspacePreparationResult:
    workspace_path: Path
    repo_path: Optional[Path] = None
    worktree_path: Optional[Path] = None
    branch_name: Optional[str] = None
    base_branch: Optional[str] = None
    used_git_worktree: bool = False
    workspace_kind: str = "directory"
    cleanup_policy: str = "none"
    app_created: bool = True

    def metadata_updates(self) -> dict[str, Any]:
        return {
            "workspace_path": str(self.workspace_path),
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "worktree_path": str(self.worktree_path) if self.worktree_path else None,
            "branch_name": self.branch_name,
            "base_branch": self.base_branch,
            "use_git_worktree": self.used_git_worktree,
            "workspace_kind": self.workspace_kind,
            "cleanup_policy": self.cleanup_policy,
            "workspace_app_created": self.app_created,
        }


@dataclass
class WorkspaceCleanupResult:
    cleaned: bool
    reason: str
    workspace_kind: Optional[str] = None
    cleaned_path: Optional[Path] = None


def ensure_within_root(root: Path, candidate: Path) -> Path:
    """Resolve a path and ensure it stays inside the configured root."""

    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_root not in resolved_candidate.parents and resolved_candidate != resolved_root:
        raise ValueError(f"Path escapes workspace root: {resolved_candidate}")
    return resolved_candidate


def ensure_job_workspace(workspace_root: Path, job_id: str) -> Path:
    """Create and return a dedicated per-job workspace directory."""

    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = workspace_root / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    return ensure_within_root(workspace_root, workspace)


def prepare_job_workspace(
    workspace_root: Path,
    job_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> WorkspacePreparationResult:
    """Prepare a durable per-job workspace or git worktree for coding jobs.

    The implementation is intentionally conservative:
    - plain directories remain the default
    - git worktrees are created only when explicitly requested
    - the original repository working tree is never removed
    - all created paths stay rooted under ``workspace_root``
    """

    values = dict(metadata or {})
    cleanup_policy = _normalize_cleanup_policy(values.get("cleanup_policy"))
    use_git_worktree = bool(values.get("use_git_worktree"))

    if use_git_worktree:
        return _prepare_git_worktree(
            workspace_root=workspace_root,
            job_id=job_id,
            values=values,
            cleanup_policy=cleanup_policy,
        )

    workspace = _existing_workspace_path(workspace_root, values.get("workspace_path")) or ensure_job_workspace(
        workspace_root,
        job_id,
    )
    _write_workspace_marker(
        workspace,
        {
            "job_id": job_id,
            "workspace_kind": "directory",
            "cleanup_policy": cleanup_policy,
        },
    )
    return WorkspacePreparationResult(
        workspace_path=workspace,
        used_git_worktree=False,
        workspace_kind="directory",
        cleanup_policy=cleanup_policy,
        app_created=True,
    )


def cleanup_job_workspace(
    workspace_root: Path,
    metadata: Optional[dict[str, Any]],
    *,
    outcome: str,
) -> WorkspaceCleanupResult:
    """Clean an app-created workspace when the configured policy allows it."""

    values = dict(metadata or {})
    cleanup_policy = _normalize_cleanup_policy(values.get("cleanup_policy"))
    if cleanup_policy == "none":
        return WorkspaceCleanupResult(cleaned=False, reason="cleanup_disabled")
    if cleanup_policy == "on_success" and outcome != "completed":
        return WorkspaceCleanupResult(cleaned=False, reason="cleanup_skipped_for_outcome")
    if cleanup_policy == "on_completion" and outcome not in {"completed", "failed", "cancelled"}:
        return WorkspaceCleanupResult(cleaned=False, reason="cleanup_skipped_for_outcome")

    workspace_kind = str(values.get("workspace_kind") or "directory")
    workspace_path_value = values.get("workspace_path")
    if not workspace_path_value:
        return WorkspaceCleanupResult(cleaned=False, reason="missing_workspace_path", workspace_kind=workspace_kind)

    workspace_path = ensure_within_root(workspace_root, Path(str(workspace_path_value)))
    if not _is_app_created_workspace(workspace_path, values):
        return WorkspaceCleanupResult(cleaned=False, reason="workspace_not_app_created", workspace_kind=workspace_kind)

    if workspace_kind == "git_worktree":
        repo_path_value = values.get("repo_path")
        if not repo_path_value:
            return WorkspaceCleanupResult(cleaned=False, reason="missing_repo_path", workspace_kind=workspace_kind)
        repo_path = Path(str(repo_path_value)).expanduser().resolve()
        if workspace_path.exists() and _git_is_dirty(workspace_path):
            return WorkspaceCleanupResult(cleaned=False, reason="workspace_dirty", workspace_kind=workspace_kind)
        if workspace_path.exists():
            _git(["worktree", "remove", "--force", str(workspace_path)], cwd=repo_path)
        parent = workspace_path.parent
        if parent.exists() and parent != workspace_root.resolve():
            try:
                parent.rmdir()
            except OSError:
                pass
        return WorkspaceCleanupResult(
            cleaned=True,
            reason="worktree_removed",
            workspace_kind=workspace_kind,
            cleaned_path=workspace_path,
        )

    if workspace_path.exists():
        shutil.rmtree(workspace_path)
    return WorkspaceCleanupResult(
        cleaned=True,
        reason="workspace_removed",
        workspace_kind=workspace_kind,
        cleaned_path=workspace_path,
    )


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


def _prepare_git_worktree(
    *,
    workspace_root: Path,
    job_id: str,
    values: dict[str, Any],
    cleanup_policy: str,
) -> WorkspacePreparationResult:
    repo_path_value = values.get("repo_path")
    if not repo_path_value:
        raise ValueError("use_git_worktree=true requires repo_path metadata.")
    repo_path = _resolve_git_repo(Path(str(repo_path_value)))

    job_root = ensure_job_workspace(workspace_root, job_id)
    existing_worktree = _existing_workspace_path(workspace_root, values.get("worktree_path"))
    worktree_path = existing_worktree or ensure_within_root(workspace_root, job_root / "worktree")
    branch_name = str(values.get("branch_name") or f"claude-orchestrator/{job_id[:8]}")
    base_branch = str(values.get("base_branch") or _git_current_ref(repo_path))

    if worktree_path.exists() and (worktree_path / ".git").exists():
        _write_workspace_marker(
            worktree_path,
            {
                "job_id": job_id,
                "workspace_kind": "git_worktree",
                "repo_path": str(repo_path),
                "branch_name": branch_name,
                "base_branch": base_branch,
                "cleanup_policy": cleanup_policy,
            },
        )
        return WorkspacePreparationResult(
            workspace_path=worktree_path,
            repo_path=repo_path,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_branch=base_branch,
            used_git_worktree=True,
            workspace_kind="git_worktree",
            cleanup_policy=cleanup_policy,
            app_created=True,
        )

    if worktree_path.exists() and any(worktree_path.iterdir()):
        raise ValueError(f"Requested worktree path is not empty: {worktree_path}")

    _git(["worktree", "add", "-b", branch_name, str(worktree_path), base_branch], cwd=repo_path)
    _write_workspace_marker(
        worktree_path,
        {
            "job_id": job_id,
            "workspace_kind": "git_worktree",
            "repo_path": str(repo_path),
            "branch_name": branch_name,
            "base_branch": base_branch,
            "cleanup_policy": cleanup_policy,
        },
    )
    return WorkspacePreparationResult(
        workspace_path=worktree_path,
        repo_path=repo_path,
        worktree_path=worktree_path,
        branch_name=branch_name,
        base_branch=base_branch,
        used_git_worktree=True,
        workspace_kind="git_worktree",
        cleanup_policy=cleanup_policy,
        app_created=True,
    )


def _normalize_cleanup_policy(value: Any) -> str:
    policy = str(value or "none").strip().lower()
    if policy not in SUPPORTED_CLEANUP_POLICIES:
        raise ValueError(f"Unsupported cleanup_policy: {policy}")
    return policy


def _existing_workspace_path(workspace_root: Path, raw_path: Any) -> Optional[Path]:
    if not raw_path:
        return None
    path = Path(str(raw_path)).expanduser()
    return ensure_within_root(workspace_root, path)


def _resolve_git_repo(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"repo_path does not exist or is not a directory: {resolved}")
    _git(["rev-parse", "--is-inside-work-tree"], cwd=resolved)
    top_level = _git(["rev-parse", "--show-toplevel"], cwd=resolved).strip()
    return Path(top_level).resolve()


def _git_current_ref(repo_path: Path) -> str:
    ref = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).strip()
    if not ref or ref == "HEAD":
        raise ValueError(f"Could not determine base branch for repository {repo_path}")
    return ref


def _git_is_dirty(worktree_path: Path) -> bool:
    status = _git(["status", "--porcelain"], cwd=worktree_path)
    for line in status.splitlines():
        path = line[3:].strip()
        normalized = path.replace("\\", "/")
        if normalized in IGNORED_WORKTREE_PATHS:
            continue
        if any(normalized.startswith(prefix) for prefix in ("private/",)):
            continue
        return True
    return False


def _git(args: list[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - exercised by caller-facing tests.
        stderr = (exc.stderr or exc.stdout or "").strip()
        raise ValueError(f"git {' '.join(args)} failed: {stderr or exc}") from exc
    return completed.stdout


def _write_workspace_marker(path: Path, payload: dict[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    marker_path = path / WORKSPACE_MARKER_FILENAME
    marker_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _is_app_created_workspace(path: Path, values: dict[str, Any]) -> bool:
    if not values.get("workspace_app_created", True):
        return False
    return (path / WORKSPACE_MARKER_FILENAME).exists()

"""High-level orchestration operations shared by CLI, workers, and web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..backends.codex_cli import CodexCliBackend
from ..backends.factory import validate_backend_config
from ..config import AppConfig
from ..diagnostics import BackendSmokeTestResult, DiagnosticCheck, DiagnosticsReport
from ..integrations import (
    WorkspaceIntegrationSummary,
    build_integration_metadata_summary,
    discover_workspace_integrations,
)
from ..models import (
    ArtifactRecord,
    BackendResult,
    ConversationState,
    EnqueueJobRequest,
    Job,
    JobStreamEvent,
    JobStatus,
    ProjectManagerSessionStatus,
    RetryDisposition,
    SavedProject,
)
from ..providers import infer_provider, validate_provider_backend
from ..repository import JobRepository
from ..retry import ConfigurationError, RetryPolicy, RetryableBackendError, UserCancelledError, parse_retry_after
from ..timeutils import utcnow
from ..workspaces import (
    cleanup_job_workspace,
    delete_job_workspace,
    prepare_job_workspace,
    resolve_job_prompt,
    store_private_prompt,
    store_response_artifact,
)
from ..backends.base import BackendAdapter, BatchCapableBackend, BackendContext
from .project_manager import ProjectManagerService

logger = logging.getLogger(__name__)


@dataclass
class JobInspection:
    job: Job
    state: ConversationState
    runs: List[Any]
    events: List[Any]
    stream_events: List[JobStreamEvent]
    latest_stream_event: Optional[JobStreamEvent]
    latest_phase: Optional[str]
    latest_progress_message: Optional[str]
    artifacts: List[ArtifactRecord]
    integration_summary: Optional[WorkspaceIntegrationSummary]
    backend_integration_support: Dict[str, Any]


@dataclass
class JobDeletionResult:
    job_id: str
    deleted_status: str
    workspace_cleaned: bool
    workspace_cleanup_reason: str


@dataclass
class JobLifecycleSummary:
    status: str
    headline: str
    helper_text: str


@dataclass
class ManagerImmediateStartResult:
    started: bool
    current_status: str
    reason: Optional[str] = None
    claimed_job: Optional[Job] = None


class LeaseHeartbeat:
    """Background lease renewer for long-running jobs."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        heartbeat_interval_seconds: int,
    ) -> None:
        self.repository = repository
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self.lost_lease = False
        self._renewal_count = 0

    def start(self) -> None:
        if self._task is not None or self.heartbeat_interval_seconds <= 0:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        try:
            await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                renewed = self.repository.renew_lease(
                    self.job_id,
                    self.worker_id,
                    self.lease_seconds,
                )
                if not renewed:
                    self.lost_lease = True
                    self.repository.add_scheduler_event(
                        self.job_id,
                        "job.lease.heartbeat_lost",
                        {"worker_id": self.worker_id},
                    )
                    logger.warning(
                        "job.lease.heartbeat_lost",
                        extra={"context": {"job_id": self.job_id, "worker_id": self.worker_id}},
                    )
                    return
                self._renewal_count += 1
                if self._renewal_count == 1 or self._renewal_count % 10 == 0:
                    renewed_until = utcnow() + timedelta(seconds=self.lease_seconds)
                    self.repository.add_scheduler_event(
                        self.job_id,
                        "job.lease.heartbeat",
                        {
                            "worker_id": self.worker_id,
                            "lease_expires_at": renewed_until.isoformat(),
                            "renewal_count": self._renewal_count,
                        },
                    )
                logger.debug(
                    "job.lease.heartbeat_renewed",
                    extra={
                        "context": {
                            "job_id": self.job_id,
                            "worker_id": self.worker_id,
                            "renewal_count": self._renewal_count,
                        }
                    },
                )


class OrchestratorService:
    """Coordinates persistence, retries, recovery, and backend execution."""

    PROJECT_MANAGER_FULL_AUTONOMY_LIMIT = 10
    CLAUDE_CODING_BACKEND_PREFERENCE = ("claude_code_cli", "messages_api", "message_batches")

    def __init__(
        self,
        root: Path,
        config: AppConfig,
        repository: JobRepository,
        backends: Dict[str, object],
        config_path: Optional[Path] = None,
    ) -> None:
        self.root = root
        self.config = config
        self.repository = repository
        self.backends = backends
        self.config_path = config_path
        self.retry_policy = RetryPolicy(config.retry)
        self.project_manager = ProjectManagerService(repository)

    def preferred_claude_coding_backend(self) -> Optional[str]:
        for backend_name in self.CLAUDE_CODING_BACKEND_PREFERENCE:
            if backend_name in self.backends:
                return backend_name
        return None

    def preferred_auto_coding_backend(self, *, default_backend: Optional[str] = None) -> Optional[str]:
        if "codex_cli" in self.backends:
            return "codex_cli"
        if default_backend and default_backend in self.backends:
            return default_backend
        claude_backend = self.preferred_claude_coding_backend()
        if claude_backend:
            return claude_backend
        if self.config.default_backend in self.backends:
            return self.config.default_backend
        return next(iter(sorted(self.backends.keys())), None)

    def resolve_coding_agent_preference(
        self,
        preference: Optional[str],
        *,
        default_backend: Optional[str] = None,
    ) -> Optional[str]:
        value = str(preference or "").strip().lower()
        if not value or value == "auto":
            return self.preferred_auto_coding_backend(default_backend=default_backend)
        if value == "codex":
            if "codex_cli" not in self.backends:
                raise ValueError("Codex is not available as a coding agent in this AI Telescreen setup.")
            return "codex_cli"
        if value == "claude":
            claude_backend = self.preferred_claude_coding_backend()
            if claude_backend is None:
                raise ValueError("Claude is not available as a coding agent in this AI Telescreen setup.")
            return claude_backend
        if value in self.backends:
            return value
        raise ValueError("Coding Agent must be Auto, Codex, Claude, or a valid configured backend.")

    def coding_agent_alias_for_backend(self, backend_name: Optional[str]) -> str:
        value = str(backend_name or "").strip()
        if not value:
            return "auto"
        if value == "codex_cli":
            return "codex"
        if value in self.CLAUDE_CODING_BACKEND_PREFERENCE:
            return "claude"
        return value

    def describe_coding_agent_options(self) -> List[Dict[str, str]]:
        options: List[Dict[str, str]] = [
            {
                "value": "auto",
                "label": "Auto",
                "hint": "manager decides",
            }
        ]
        if self.preferred_claude_coding_backend() is not None:
            options.append(
                {
                    "value": "claude",
                    "label": "Claude",
                    "hint": "always use Claude",
                }
            )
        if "codex_cli" in self.backends:
            options.append(
                {
                    "value": "codex",
                    "label": "Codex",
                    "hint": "always use Codex",
                }
            )
        return options

    def enqueue(self, request: EnqueueJobRequest) -> Job:
        backend_name = request.backend or self.config.default_backend
        if backend_name not in self.backends:
            raise ValueError(f"Unknown backend: {backend_name}")
        provider_name = infer_provider(backend_name) if not request.provider else validate_provider_backend(
            request.provider,
            backend_name,
        )
        existing = (
            self.repository.get_job_by_idempotency_key(request.idempotency_key)
            if request.idempotency_key
            else None
        )
        if existing is not None:
            return existing
        privacy_mode = self.config.privacy.enabled if request.privacy_mode is None else request.privacy_mode
        prompt_to_store = request.prompt
        metadata = dict(request.metadata)
        if backend_name == "codex_cli" and "use_git_worktree" not in metadata:
            metadata["use_git_worktree"] = self.config.backends.codex_cli.use_git_worktree
        metadata.setdefault("cleanup_policy", "none")
        if privacy_mode:
            prompt_to_store = "[stored_in_private_artifact]"
        request.backend = backend_name
        request.provider = provider_name
        request.model = request.model or metadata.get("model") or self._default_model_for_backend(backend_name)
        job_id = str(uuid.uuid4())
        workspace_result = prepare_job_workspace(self.config.workspace_path(self.root), job_id, metadata)
        metadata.update(
            {
                key: value
                for key, value in workspace_result.metadata_updates().items()
                if value is not None
            }
        )
        integration_summary = self._discover_integration_summary(
            workspace_result.workspace_path,
            workspace_result.repo_path,
        )
        metadata["integration_summary"] = build_integration_metadata_summary(integration_summary)
        request.metadata = metadata
        created = self.repository.create_job(
            request,
            prompt_to_store=prompt_to_store,
            workspace_path=workspace_result.workspace_path,
            job_id=job_id,
        )
        self._persist_integration_summary(created.id, integration_summary)
        actual_workspace = workspace_result.workspace_path
        if privacy_mode:
            metadata = dict(created.metadata)
            prompt_artifact_path = store_private_prompt(
                actual_workspace,
                request.prompt,
                self.config.privacy.prompt_artifact_dir,
            )
            metadata["prompt_artifact_path"] = str(prompt_artifact_path)
            self.repository.set_job_metadata(created.id, metadata)
            self.repository.add_scheduler_event(
                created.id,
                "privacy_prompt_stored",
                {"prompt_artifact_path": str(prompt_artifact_path)},
            )
        self.repository.add_scheduler_event(
            created.id,
            "workspace_initialized",
            {
                "workspace_path": str(actual_workspace),
                "provider": provider_name,
                "backend": backend_name,
                "workspace_kind": workspace_result.workspace_kind,
                "repo_path": str(workspace_result.repo_path) if workspace_result.repo_path else None,
                "worktree_path": str(workspace_result.worktree_path) if workspace_result.worktree_path else None,
                "branch_name": workspace_result.branch_name,
                "base_branch": workspace_result.base_branch,
                "cleanup_policy": workspace_result.cleanup_policy,
            },
        )
        return self.repository.get_job(created.id)

    def status(self) -> Dict[str, int]:
        return self.repository.get_status_counts()

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        provider: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = 100,
    ) -> List[Job]:
        return self.repository.list_jobs(
            status=status,
            provider=provider,
            backend=backend,
            limit=limit,
            continuation_priority_bonus=self.config.worker.continuation_priority_bonus,
        )

    def list_saved_projects(self) -> List[SavedProject]:
        return self.repository.list_saved_projects()

    def get_saved_project(self, project_id: str) -> SavedProject:
        return self.repository.get_saved_project(project_id)

    def create_saved_project(
        self,
        *,
        name: str,
        repo_path: str,
        default_backend: Optional[str] = None,
        default_provider: Optional[str] = None,
        default_base_branch: Optional[str] = None,
        default_use_git_worktree: bool = False,
        notes: Optional[str] = None,
        autonomy_mode: str = "minimal",
        initial_context: Optional[str] = None,
        default_model: Optional[str] = None,
        auto_resume_on_quota: bool = False,
        allow_extra_usage: bool = False,
    ) -> SavedProject:
        resolved_repo_path = Path(repo_path).expanduser().resolve()
        if not resolved_repo_path.exists() or not resolved_repo_path.is_dir():
            raise ValueError(f"Repository path does not exist or is not a directory: {resolved_repo_path}")
        project = self.repository.create_saved_project(
            name=name,
            repo_path=str(resolved_repo_path),
            default_backend=default_backend,
            default_provider=default_provider,
            default_base_branch=default_base_branch,
            default_use_git_worktree=default_use_git_worktree,
            notes=notes,
            autonomy_mode=autonomy_mode,
            initial_context=initial_context,
            default_model=default_model,
            auto_resume_on_quota=auto_resume_on_quota,
            allow_extra_usage=allow_extra_usage,
        )
        self.project_manager.sync_project(project.id)
        if initial_context and initial_context.strip():
            self.project_manager._store_project_guidance(project.id, initial_context.strip())
        return project

    def update_saved_project(
        self,
        project_id: str,
        *,
        name: str,
        repo_path: str,
        default_backend: Optional[str] = None,
        default_provider: Optional[str] = None,
        default_base_branch: Optional[str] = None,
        default_use_git_worktree: bool = False,
        notes: Optional[str] = None,
        autonomy_mode: str = "minimal",
        initial_context: Optional[str] = None,
        default_model: Optional[str] = None,
        auto_resume_on_quota: bool = False,
        allow_extra_usage: bool = False,
    ) -> SavedProject:
        resolved_repo_path = Path(repo_path).expanduser().resolve()
        if not resolved_repo_path.exists() or not resolved_repo_path.is_dir():
            raise ValueError(f"Repository path does not exist or is not a directory: {resolved_repo_path}")
        project = self.repository.update_saved_project(
            project_id,
            name=name,
            repo_path=str(resolved_repo_path),
            default_backend=default_backend,
            default_provider=default_provider,
            default_base_branch=default_base_branch,
            default_use_git_worktree=default_use_git_worktree,
            notes=notes,
            autonomy_mode=autonomy_mode,
            initial_context=initial_context,
            default_model=default_model,
            auto_resume_on_quota=auto_resume_on_quota,
            allow_extra_usage=allow_extra_usage,
        )
        self.project_manager.sync_project(project.id)
        return project

    def set_project_autonomy_mode(self, project_id: str, autonomy_mode: str) -> SavedProject:
        project = self.repository.get_saved_project(project_id)
        return self.update_saved_project(
            project_id,
            name=project.name,
            repo_path=project.repo_path,
            default_backend=project.default_backend,
            default_provider=project.default_provider,
            default_base_branch=project.default_base_branch,
            default_use_git_worktree=project.default_use_git_worktree,
            notes=project.notes,
            autonomy_mode=autonomy_mode,
            default_model=project.default_model,
            auto_resume_on_quota=project.auto_resume_on_quota,
            allow_extra_usage=project.allow_extra_usage,
        )

    def set_project_default_model(self, project_id: str, default_model: Optional[str]) -> SavedProject:
        project = self.repository.get_saved_project(project_id)
        return self.update_saved_project(
            project_id,
            name=project.name,
            repo_path=project.repo_path,
            default_backend=project.default_backend,
            default_provider=project.default_provider,
            default_base_branch=project.default_base_branch,
            default_use_git_worktree=project.default_use_git_worktree,
            notes=project.notes,
            autonomy_mode=project.autonomy_mode,
            default_model=default_model,
            auto_resume_on_quota=project.auto_resume_on_quota,
            allow_extra_usage=project.allow_extra_usage,
        )

    def launch_job_from_project(
        self,
        project_id: str,
        *,
        prompt: str,
        task_type: str = "code",
        priority: int = 0,
        max_attempts: int = 5,
        backend: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        use_git_worktree: Optional[bool] = None,
        base_branch: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Job:
        project = self.repository.get_saved_project(project_id)
        metadata = self._sanitize_enqueue_metadata(
            {
                "project_id": project.id,
                "project_name": project.name,
                "repo_path": project.repo_path,
                "use_git_worktree": (
                    project.default_use_git_worktree if use_git_worktree is None else use_git_worktree
                ),
                "base_branch": base_branch or project.default_base_branch,
                **(extra_metadata or {}),
            }
        )
        return self.enqueue(
            EnqueueJobRequest(
                backend=backend or project.default_backend or self.config.default_backend,
                provider=provider or project.default_provider,
                task_type=task_type,
                prompt=prompt,
                priority=priority,
                metadata=metadata,
                max_attempts=max_attempts,
                model=model or project.default_model,
            )
        )

    def duplicate_job(
        self,
        job_id: str,
        *,
        prompt: Optional[str] = None,
    ) -> Job:
        original = self.repository.get_job(job_id)
        source_prompt = prompt if prompt is not None else resolve_job_prompt(original)
        metadata = self._sanitize_enqueue_metadata(dict(original.metadata))
        return self.enqueue(
            EnqueueJobRequest(
                backend=original.backend,
                provider=original.provider,
                task_type=original.task_type,
                prompt=source_prompt,
                priority=original.priority,
                metadata=metadata,
                max_attempts=original.max_attempts,
                system_prompt=self.repository.get_conversation_state(job_id).system_prompt,
                model=original.model,
                privacy_mode=bool(original.metadata.get("prompt_artifact_path")),
            )
        )

    def describe_job_actions(self, job: Job) -> Dict[str, Any]:
        status = job.status
        return {
            "open": True,
            "run_now": status == JobStatus.QUEUED,
            "cancel": status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING_RETRY},
            "retry": status == JobStatus.FAILED,
            "retry_now": status == JobStatus.WAITING_RETRY,
            "duplicate": status in {JobStatus.FAILED, JobStatus.COMPLETED, JobStatus.CANCELLED},
            "delete": status in {
                JobStatus.QUEUED,
                JobStatus.FAILED,
                JobStatus.WAITING_RETRY,
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
            },
            "delete_disabled_reason": (
                "Running jobs cannot be deleted while work is in progress."
                if status == JobStatus.RUNNING
                else None
            ),
        }

    def describe_job_lifecycle(self, job: Job) -> JobLifecycleSummary:
        if job.status == JobStatus.QUEUED:
            return JobLifecycleSummary(
                status=job.status.value,
                headline="Queued and waiting to start",
                helper_text=(
                    "This task has not started yet. Use Run Now to start it from the browser, "
                    "Cancel to remove it from the queue, or Delete to permanently remove accidental test jobs."
                ),
            )
        if job.status == JobStatus.RUNNING:
            return JobLifecycleSummary(
                status=job.status.value,
                headline="Running now",
                helper_text=(
                    "A worker is actively processing this task. Cancel asks the worker to stop and record a safe interruption."
                ),
            )
        if job.status == JobStatus.WAITING_RETRY:
            return JobLifecycleSummary(
                status=job.status.value,
                headline="Waiting for another attempt",
                helper_text=(
                    "This task hit a retryable problem. Retry Now starts the next attempt immediately, "
                    "or Cancel/Delete if you do not want it to run again."
                ),
            )
        if job.status == JobStatus.FAILED:
            return JobLifecycleSummary(
                status=job.status.value,
                headline="Stopped with an error",
                helper_text=(
                    "This task ended in a permanent failure or ran out of attempts. Retry re-queues it, "
                    "Duplicate copies it into a fresh task, and Delete removes this record."
                ),
            )
        if job.status == JobStatus.COMPLETED:
            return JobLifecycleSummary(
                status=job.status.value,
                headline="Completed successfully",
                helper_text=(
                    "This task finished. Duplicate creates a fresh queued copy, and Delete removes completed test or throwaway jobs."
                ),
            )
        return JobLifecycleSummary(
            status=job.status.value,
            headline="Cancelled",
            helper_text=(
                "This task was cancelled before or during execution. Delete removes it permanently, and Duplicate creates a new queued copy."
            ),
        )

    def claim_job_for_run_now(self, job_id: str, *, worker_id: str) -> Job:
        job = self.repository.get_job(job_id)
        if job.status != JobStatus.QUEUED:
            raise ValueError(f"Job {job_id} must be queued before it can run now; current status is {job.status.value}.")
        claimed = self.repository.claim_job(
            job_id,
            worker_id,
            lease_seconds=self.config.effective_lease_seconds(),
        )
        if claimed is None:
            current = self.repository.get_job(job_id)
            raise ValueError(f"Job {job_id} could not be started because it is now {current.status.value}.")
        self.repository.add_scheduler_event(
            job_id,
            "job_run_now_requested",
            {"worker_id": worker_id},
        )
        return claimed

    async def run_job_now(self, job_id: str, *, worker_id: str) -> Job:
        claimed = self.claim_job_for_run_now(job_id, worker_id=worker_id)
        return await self.process_claimed_job(claimed, worker_id)

    def retry_job_now(self, job_id: str, *, worker_id: str) -> Job:
        retried = self.retry_job(job_id)
        return self.claim_job_for_run_now(retried.id, worker_id=worker_id)

    def _manager_queue_reason_for_job(self, job: Job) -> str:
        if job.status == JobStatus.RUNNING:
            return "Task is already running."
        if job.status == JobStatus.COMPLETED:
            return "Task already completed before AI Telescreen could start it again."
        if job.status == JobStatus.FAILED:
            return "Task reached a failed state before AI Telescreen could start it immediately."
        if job.status == JobStatus.CANCELLED:
            return "Task was cancelled before AI Telescreen could start it immediately."
        if job.status == JobStatus.WAITING_RETRY:
            return "Task is waiting for a retry window, so AI Telescreen could not start it immediately."
        return "Task was created but could not start immediately because immediate execution is unavailable in this context."

    def try_start_manager_job_now(
        self,
        job_id: str,
        *,
        worker_id: str,
        launch_context: str,
    ) -> ManagerImmediateStartResult:
        job = self.repository.get_job(job_id)
        if job.status == JobStatus.RUNNING:
            return ManagerImmediateStartResult(started=False, current_status=job.status.value)
        if job.status != JobStatus.QUEUED:
            return ManagerImmediateStartResult(
                started=False,
                current_status=job.status.value,
                reason=self._manager_queue_reason_for_job(job),
            )
        try:
            claimed = self.claim_job_for_run_now(job_id, worker_id=worker_id)
        except Exception:
            current = self.repository.get_job(job_id)
            reason = self._manager_queue_reason_for_job(current)
            if current.status == JobStatus.QUEUED:
                self.repository.update_job_status(
                    job_id,
                    target_status=JobStatus.QUEUED,
                    current_status=JobStatus.QUEUED,
                    clear_lease=False,
                    event_type="project_manager.immediate_start_unavailable",
                    event_detail={"reason": reason, "launch_context": launch_context},
                    metadata_updates={"project_manager_queue_reason": reason},
                )
            return ManagerImmediateStartResult(
                started=False,
                current_status=current.status.value,
                reason=reason,
            )
        self.repository.update_job_status(
            job_id,
            target_status=JobStatus.RUNNING,
            current_status=JobStatus.RUNNING,
            clear_lease=False,
            event_type="project_manager.immediate_start_requested",
            event_detail={"worker_id": worker_id, "launch_context": launch_context},
            metadata_remove_keys=("project_manager_queue_reason",),
        )
        return ManagerImmediateStartResult(
            started=True,
            current_status=claimed.status.value,
            claimed_job=claimed,
        )

    def delete_job(self, job_id: str) -> JobDeletionResult:
        job = self.repository.get_job(job_id)
        if job.status == JobStatus.RUNNING:
            raise ValueError("Running jobs cannot be deleted while work is in progress.")
        cleanup_result = delete_job_workspace(
            self.config.workspace_path(self.root),
            job.metadata,
        )
        deleted = self.repository.delete_job(job_id)
        return JobDeletionResult(
            job_id=deleted.id,
            deleted_status=deleted.status.value,
            workspace_cleaned=cleanup_result.cleaned,
            workspace_cleanup_reason=cleanup_result.reason,
        )

    def list_project_jobs(self, project_id: str, *, limit: int = 20) -> List[Job]:
        project = self.repository.get_saved_project(project_id)
        explicit_jobs = self.repository.list_project_jobs(project_id, limit=limit)
        recent_jobs = self.repository.list_recent_jobs(limit=max(limit * 10, 50))
        merged: Dict[str, Job] = {job.id: job for job in explicit_jobs}
        for job in recent_jobs:
            if job.id in merged:
                continue
            if job.metadata.get("project_id") == project_id or job.metadata.get("repo_path") == project.repo_path:
                merged[job.id] = job
        return sorted(merged.values(), key=lambda job: job.created_at, reverse=True)[:limit]

    def get_project_integration_summary(self, project_id: str) -> WorkspaceIntegrationSummary:
        project = self.repository.get_saved_project(project_id)
        project_path = Path(project.repo_path).expanduser()
        repo_path = project_path if (project_path / ".git").exists() else None
        return self._discover_integration_summary(project_path, repo_path)

    def get_project_manager_snapshot(self, project_id: str):
        self.repository.get_saved_project(project_id)
        return self.project_manager.get_snapshot(project_id)

    def describe_project_manager_session(self, project_id: str) -> ProjectManagerSessionStatus:
        project = self.repository.get_saved_project(project_id)
        snapshot = self.get_project_manager_snapshot(project_id)
        workflow_state = snapshot.state.workflow_state
        autonomy_mode = self.project_manager._normalize_autonomy_mode(project.autonomy_mode)
        managed_jobs = [
            job
            for job in self.list_project_jobs(project_id, limit=40)
            if bool(job.metadata.get("project_manager_auto_launched"))
        ]
        session_id = str(snapshot.state.active_autonomy_session_id or "").strip() or None
        session_jobs = [
            job
            for job in managed_jobs
            if str(job.metadata.get("project_manager_autonomy_session_id") or "").strip() == session_id
        ]
        relevant_jobs = session_jobs or managed_jobs

        active_job: Optional[Job] = None
        if snapshot.state.active_job_id:
            try:
                candidate = self.repository.get_job(snapshot.state.active_job_id)
            except KeyError:
                candidate = None
            if candidate is not None and bool(candidate.metadata.get("project_manager_auto_launched")):
                active_job = candidate

        if active_job is None and workflow_state == "running_current_task":
            active_job = next(
                (
                    job
                    for job in relevant_jobs
                    if job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING_RETRY}
                ),
                None,
            )

        last_completed_job = next((job for job in relevant_jobs if job.status == JobStatus.COMPLETED), None)
        last_failed_job = next((job for job in relevant_jobs if job.status == JobStatus.FAILED), None)
        last_terminal_job = next(
            (job for job in relevant_jobs if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}),
            None,
        )
        displayed_job = active_job
        if displayed_job is None and workflow_state in {"awaiting_continue_decision", "blocked_on_manual_test"}:
            displayed_job = last_terminal_job

        active_job_status = displayed_job.status.value if displayed_job else None
        active_job_headline = self.describe_job_lifecycle(displayed_job).headline if displayed_job else None
        waiting_on_operator = workflow_state in {"awaiting_confirmation", "awaiting_continue_decision"}
        waiting_on_worker = workflow_state == "running_current_task" and displayed_job is not None and displayed_job.status == JobStatus.QUEUED
        needs_manual_testing = workflow_state == "blocked_on_manual_test" or snapshot.state.needs_manual_testing
        current_recommendation = (
            str(snapshot.state.display_snapshot.get("conversation_reply") or "").strip()
            or (snapshot.state.latest_response.reason if snapshot.state.latest_response else None)
        )
        next_prompt = str(snapshot.state.display_snapshot.get("reply_question") or "").strip() or None

        if workflow_state == "running_current_task":
            if displayed_job is None:
                session_headline = "Task launched"
                session_detail = "The manager started a task and is waiting for the queue state to settle."
            elif displayed_job.status == JobStatus.QUEUED:
                queued_reason = str(displayed_job.metadata.get("project_manager_queue_reason") or "").strip()
                session_headline = "Task queued, waiting for worker"
                session_detail = queued_reason or (
                    "The manager already launched a task, but it is still queued. "
                    "A worker must be active to pick it up."
                )
            elif displayed_job.status == JobStatus.RUNNING:
                session_headline = "Task running"
                session_detail = "A worker is actively processing the manager-launched task right now."
            elif displayed_job.status == JobStatus.WAITING_RETRY:
                session_headline = "Task waiting to retry"
                session_detail = (
                    "The manager-launched task hit a retryable problem and is waiting for its next attempt."
                )
            elif displayed_job.status == JobStatus.FAILED:
                session_headline = "Task failed"
                session_detail = "The last manager-launched task failed and the manager is refreshing what to do next."
            else:
                session_headline = "Task finished"
                session_detail = "The latest manager-launched task finished and the manager is refreshing the project state."
        elif workflow_state == "awaiting_continue_decision":
            session_headline = "Waiting on your decision"
            session_detail = (
                "The manager finished the current supervised step and is waiting to hear whether it should keep going."
            )
        elif workflow_state == "blocked_on_manual_test":
            session_headline = "Waiting for manual testing"
            session_detail = (
                "The manager is paused until manual verification or operator judgment clears the next step."
            )
        elif workflow_state == "awaiting_confirmation":
            session_headline = "Waiting on your approval"
            session_detail = (
                "Minimal autonomy keeps manager chat separate from coding jobs and asks before every task."
                if autonomy_mode == "minimal"
                else "The manager recommended the next step and is waiting for approval before it starts that task."
            )
        elif relevant_jobs:
            session_headline = "Session idle"
            session_detail = "No manager-launched task is active right now. The last supervised session has paused."
        else:
            session_headline = "No task in progress"
            session_detail = "The manager is idle. Chat updates the plan and Project Context; coding work starts only when you run it or autonomy allows it."

        activity_candidates = [
            candidate
            for candidate in [
                snapshot.state.last_guidance_saved_at,
                snapshot.state.last_job_ingested_at,
                *(message.created_at for message in snapshot.recent_messages if message.created_at),
                *(feedback.created_at for feedback in snapshot.recent_feedback if feedback.created_at),
                *(event.created_at for event in snapshot.recent_events if event.created_at),
                *(job.updated_at for job in relevant_jobs[:4]),
            ]
            if candidate is not None
        ]
        last_manager_activity_at = max(activity_candidates) if activity_candidates else None

        return ProjectManagerSessionStatus(
            autonomy_mode=autonomy_mode,
            workflow_state=workflow_state,
            session_headline=session_headline,
            session_detail=session_detail,
            auto_task_count=max(snapshot.state.auto_tasks_run_count, 0),
            waiting_on_operator=waiting_on_operator,
            waiting_on_worker=waiting_on_worker,
            needs_manual_testing=needs_manual_testing,
            active_autonomy_session_id=session_id,
            active_managed_job_id=displayed_job.id if displayed_job else None,
            active_managed_job_status=active_job_status,
            active_managed_job_headline=active_job_headline,
            last_manager_activity_at=last_manager_activity_at,
            last_completed_managed_job_id=last_completed_job.id if last_completed_job else None,
            last_completed_managed_job_status=last_completed_job.status.value if last_completed_job else None,
            last_failed_managed_job_id=last_failed_job.id if last_failed_job else None,
            last_failed_managed_job_status=last_failed_job.status.value if last_failed_job else None,
            current_recommendation=current_recommendation,
            next_prompt=next_prompt,
        )

    def compact_project_manager_state(self, project_id: str):
        self.repository.get_saved_project(project_id)
        return self.project_manager.compact_manager_state(project_id)

    def submit_project_manager_message(
        self,
        project_id: str,
        message: str,
        *,
        urgency: Optional[str] = None,
        backend_preference: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ):
        project = self.repository.get_saved_project(project_id)
        resolved_backend_preference = self.resolve_coding_agent_preference(
            backend_preference,
            default_backend=project.default_backend,
        )
        snapshot = self.project_manager.submit_project_manager_message(
            project_id,
            message,
            urgency=urgency,
            backend_preference=resolved_backend_preference,
            execution_mode=execution_mode,
        )
        return self._progress_project_manager_autonomy(
            project,
            snapshot,
            trigger="message",
        )

    def save_project_manager_advisory(self, project_id: str):
        self.repository.get_saved_project(project_id)
        return self.project_manager.save_project_manager_advisory(project_id)

    def continue_project_manager_autonomy(self, project_id: str):
        project = self.repository.get_saved_project(project_id)
        snapshot = self.get_project_manager_snapshot(project_id)
        return self._progress_project_manager_autonomy(
            project,
            snapshot,
            trigger="continue",
            require_draft=True,
        )

    def submit_project_operator_feedback(
        self,
        project_id: str,
        *,
        outcome: str,
        notes: str,
        severity: Optional[str] = None,
        area: Optional[str] = None,
        screenshot_reference: Optional[str] = None,
        requires_followup: bool = False,
    ):
        self.repository.get_saved_project(project_id)
        return self.project_manager.submit_operator_feedback(
            project_id,
            outcome=outcome,
            notes=notes,
            severity=severity,
            area=area,
            screenshot_reference=screenshot_reference,
            requires_followup=requires_followup,
        )

    def launch_job_from_project_manager_draft(
        self,
        project_id: str,
        *,
        max_attempts: int = 5,
    ) -> Job:
        project = self.repository.get_saved_project(project_id)
        snapshot = self.get_project_manager_snapshot(project_id)
        response = snapshot.state.latest_response
        if response is None or response.draft_task is None:
            raise ValueError("This project does not currently have a manager-generated task draft to launch.")
        draft_task = response.draft_task
        autonomy_mode = self.project_manager._normalize_autonomy_mode(project.autonomy_mode)
        active_session_id = snapshot.state.active_autonomy_session_id or str(uuid.uuid4())
        next_count = max(snapshot.state.auto_tasks_run_count, 0) + 1
        job = self.launch_job_from_project(
            project_id,
            prompt=draft_task.prompt,
            task_type=draft_task.task_type,
            priority=draft_task.priority,
            max_attempts=max_attempts,
            backend=draft_task.backend,
            provider=draft_task.provider,
            use_git_worktree=draft_task.use_git_worktree,
            base_branch=draft_task.base_branch,
            extra_metadata=self._sanitize_enqueue_metadata(
                {
                    "execution_mode": draft_task.execution_mode,
                    "project_manager_decision": response.decision,
                    "project_manager_reason": response.reason,
                    "project_manager_derived_from_operator_message": draft_task.derived_from_operator_message,
                    "project_manager_auto_launched": True,
                    "project_manager_autonomy_mode": autonomy_mode,
                    "project_manager_autonomy_session_id": active_session_id,
                    "project_manager_autonomy_trigger": "operator_confirmed",
                    "project_manager_launch_source": "operator_confirmed",
                }
            ),
        )
        self.project_manager.update_workflow_state(
            project_id,
            workflow_state="running_current_task",
            active_job_id=job.id,
            active_autonomy_session_id=active_session_id,
            auto_tasks_run_count=next_count,
        )
        return job

    def run_backend_smoke_test(self, backend_name: str) -> BackendSmokeTestResult:
        if backend_name != "codex_cli":
            raise ValueError(f"Smoke tests are not implemented for backend: {backend_name}")
        return self.run_codex_smoke_test()

    def run_codex_smoke_test(self) -> BackendSmokeTestResult:
        backend = self.backends.get("codex_cli")
        if backend is None:
            return BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=False,
                status="disabled",
                summary="codex_cli is disabled in the current AI Telescreen config.",
                details={"enabled": "false"},
            )

        codex_backend = backend if isinstance(backend, CodexCliBackend) else CodexCliBackend(self.config)
        try:
            resolved = codex_backend.resolve_executable()
        except ConfigurationError as exc:
            return BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=False,
                status="invalid_config",
                summary=str(exc),
                details={
                    "enabled": str(self.config.backends.codex_cli.enabled).lower(),
                    "configured_executable": self.config.backends.codex_cli.executable,
                },
            )

        warnings: List[str] = []
        if self.config.backends.codex_cli.args:
            warnings.append(
                "The smoke test ignores configured codex_cli.args so it can stay read-only and predictable."
            )
        runtime_warning = codex_backend.codex_runtime_warning()
        if runtime_warning:
            warnings.append(runtime_warning)
        if codex_backend._uses_legacy_command_template():
            warnings.append(
                "Doctor uses the direct Codex smoke-test path, so a passing smoke test does not validate legacy "
                "command_template runtime execution."
            )

        version_output = ""
        try:
            version_run = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.root),
            )
            version_output = (version_run.stdout or version_run.stderr or "").strip()
            if version_run.returncode != 0:
                warnings.append("`codex --version` returned a non-zero exit code during diagnostics.")
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"Unable to collect Codex version output: {exc}")

        smoke_prompt = "Reply with exactly: AI Telescreen codex smoke test OK"
        command = codex_backend.build_smoke_test_command(smoke_prompt)
        timeout_seconds = self.config.backends.codex_cli.smoke_test_timeout_seconds
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(self.root),
            )
        except FileNotFoundError as exc:
            return BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=False,
                status="missing_executable",
                summary=str(exc),
                details={"resolved_executable": resolved},
                warnings=warnings,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_preview = ((exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")).strip()[:500]
            stderr_preview = ((exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")).strip()[:500]
            return BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=False,
                status="timed_out",
                summary="Codex started but did not finish the smoke test before the timeout.",
                details={
                    "resolved_executable": resolved,
                    "timeout_seconds": str(timeout_seconds),
                    "version": version_output,
                    "runtime_mode": codex_backend.codex_runtime_mode(),
                    "stdout_preview": stdout_preview,
                    "stderr_preview": stderr_preview,
                },
                warnings=warnings,
            )

        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()
        if completed.returncode != 0:
            classified_error = codex_backend.classify_cli_failure(
                stderr_text=stderr_text,
                stdout_text=stdout_text,
            )
            status = "exec_failed"
            if isinstance(classified_error, RetryableBackendError):
                status = "transient_error"
            elif any(
                token in f"{stderr_text}\n{stdout_text}".lower()
                for token in ("auth", "login", "api key", "unauthorized", "forbidden")
            ):
                status = "auth_error"
            return BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=False,
                status=status,
                summary=str(classified_error),
                details={
                    "resolved_executable": resolved,
                    "timeout_seconds": str(timeout_seconds),
                    "version": version_output,
                    "runtime_mode": codex_backend.codex_runtime_mode(),
                    "returncode": str(completed.returncode),
                    "stdout_preview": stdout_text[:500],
                    "stderr_preview": stderr_text[:500],
                },
                warnings=warnings,
            )

        return BackendSmokeTestResult(
            backend="codex_cli",
            provider="openai",
            ok=True,
            status="ok",
            summary="Codex CLI completed a minimal read-only smoke test successfully.",
            details={
                "resolved_executable": resolved,
                "timeout_seconds": str(timeout_seconds),
                "version": version_output,
                "runtime_mode": codex_backend.codex_runtime_mode(),
                "stdout_preview": stdout_text[:500],
                "stderr_preview": stderr_text[:500],
            },
            warnings=warnings,
        )

    def collect_diagnostics(self, *, run_smoke_tests: bool = True) -> DiagnosticsReport:
        report = DiagnosticsReport(
            config_path=str(self.config_path) if self.config_path else None,
            default_backend=self.config.default_backend,
            enabled_backends=sorted(self.backends.keys()),
            workspace_root=str(self.config.workspace_path(self.root)),
            database_path=str(self.config.sqlite_path(self.root)),
        )

        try:
            validate_backend_config(self.config)
            report.checks.append(
                DiagnosticCheck(
                    name="configuration",
                    ok=True,
                    status="valid",
                    summary="Runtime configuration passed validation.",
                    details={
                        "default_backend": self.config.default_backend,
                        "enabled_backends": ", ".join(sorted(self.backends.keys())),
                    },
                )
            )
        except ValueError as exc:
            report.checks.append(
                DiagnosticCheck(
                    name="configuration",
                    ok=False,
                    status="invalid",
                    summary="Runtime configuration validation failed.",
                    details={"error": str(exc)},
                )
            )

        report.checks.append(
            DiagnosticCheck(
                name="database",
                ok=self.config.sqlite_path(self.root).exists(),
                status="present" if self.config.sqlite_path(self.root).exists() else "missing",
                summary=(
                    "SQLite database is initialized."
                    if self.config.sqlite_path(self.root).exists()
                    else "SQLite database file has not been created yet."
                ),
                details={"path": str(self.config.sqlite_path(self.root))},
            )
        )

        git_path = shutil.which("git")
        report.checks.append(
            DiagnosticCheck(
                name="git",
                ok=git_path is not None,
                status="available" if git_path else "missing",
                summary="Git is available for repo-aware workspace operations."
                if git_path
                else "Git is not available on PATH.",
                details={"path": git_path or ""},
            )
        )

        anthropic_env = self.config.backends.messages_api.api_key_env
        anthropic_key_present = bool(os.getenv(anthropic_env))
        report.checks.append(
            DiagnosticCheck(
                name="messages_api",
                ok=anthropic_key_present,
                status="configured" if anthropic_key_present else "missing_api_key",
                summary=(
                    "Anthropic API environment looks configured for messages_api."
                    if anthropic_key_present
                    else f"Environment variable {anthropic_env} is not set."
                ),
                details={"api_key_env": anthropic_env},
            )
        )

        report.checks.append(
            DiagnosticCheck(
                name="message_batches",
                ok=not self.config.backends.message_batches.enabled or anthropic_key_present,
                status=(
                    "disabled"
                    if not self.config.backends.message_batches.enabled
                    else ("configured" if anthropic_key_present else "missing_api_key")
                ),
                summary=(
                    "message_batches is disabled."
                    if not self.config.backends.message_batches.enabled
                    else (
                        "Message Batches can use the configured Anthropic key."
                        if anthropic_key_present
                        else f"message_batches needs {anthropic_env} to be set."
                    )
                ),
                details={"enabled": str(self.config.backends.message_batches.enabled).lower()},
            )
        )

        agent_env = self.config.backends.agent_sdk.api_key_env
        agent_key_present = bool(os.getenv(agent_env))
        report.checks.append(
            DiagnosticCheck(
                name="agent_sdk",
                ok=not self.config.backends.agent_sdk.enabled or agent_key_present,
                status=(
                    "disabled"
                    if not self.config.backends.agent_sdk.enabled
                    else ("configured" if agent_key_present else "missing_api_key")
                ),
                summary=(
                    "agent_sdk is disabled."
                    if not self.config.backends.agent_sdk.enabled
                    else (
                        "agent_sdk has the required Anthropic API key environment."
                        if agent_key_present
                        else f"agent_sdk needs {agent_env} to be set."
                    )
                ),
                details={
                    "enabled": str(self.config.backends.agent_sdk.enabled).lower(),
                    "api_key_env": agent_env,
                },
            )
        )

        claude_executable = shutil.which(self.config.backends.claude_code_cli.executable)
        report.checks.append(
            DiagnosticCheck(
                name="claude_code_cli",
                ok=(
                    not self.config.backends.claude_code_cli.enabled
                    or (
                        bool(self.config.backends.claude_code_cli.command_template)
                        and claude_executable is not None
                    )
                ),
                status=(
                    "disabled"
                    if not self.config.backends.claude_code_cli.enabled
                    else (
                        "configured"
                        if self.config.backends.claude_code_cli.command_template and claude_executable is not None
                        else "invalid_config"
                    )
                ),
                summary=(
                    "claude_code_cli is disabled."
                    if not self.config.backends.claude_code_cli.enabled
                    else (
                        "Claude Code CLI executable and command template are configured."
                        if self.config.backends.claude_code_cli.command_template and claude_executable is not None
                        else "Claude Code CLI needs an executable on PATH and a non-empty command_template."
                    )
                ),
                details={
                    "enabled": str(self.config.backends.claude_code_cli.enabled).lower(),
                    "configured_executable": self.config.backends.claude_code_cli.executable,
                    "resolved_executable": claude_executable or "",
                },
            )
        )

        codex_backend = self.backends.get("codex_cli")
        if not isinstance(codex_backend, CodexCliBackend):
            codex_backend = CodexCliBackend(self.config)
        codex_executable = shutil.which(self.config.backends.codex_cli.executable)
        codex_config_valid = (
            codex_executable is not None
            and self.config.backends.codex_cli.timeout_seconds > 0
            and self.config.backends.codex_cli.smoke_test_timeout_seconds > 0
            and (
                not self.config.backends.codex_cli.use_legacy_command_template
                or codex_backend.has_legacy_command_template_configured()
            )
        )
        report.checks.append(
            DiagnosticCheck(
                name="codex_cli",
                ok=(
                    not self.config.backends.codex_cli.enabled
                    or codex_config_valid
                ),
                status=(
                    "disabled"
                    if not self.config.backends.codex_cli.enabled
                    else ("configured" if codex_config_valid else "invalid_config")
                ),
                summary=(
                    "codex_cli is disabled."
                    if not self.config.backends.codex_cli.enabled
                    else (
                        "Codex CLI executable and timeout settings look valid."
                        if codex_config_valid
                        else (
                            "Codex CLI legacy mode needs a non-empty command_template in addition to an executable "
                            "on PATH and positive timeout settings."
                            if self.config.backends.codex_cli.use_legacy_command_template
                            else "Codex CLI needs an executable on PATH and positive timeout settings."
                        )
                    )
                ),
                details={
                    "enabled": str(self.config.backends.codex_cli.enabled).lower(),
                    "configured_executable": self.config.backends.codex_cli.executable,
                    "resolved_executable": codex_executable or "",
                    "runtime_mode": codex_backend.codex_runtime_mode(),
                    "use_legacy_command_template": str(
                        self.config.backends.codex_cli.use_legacy_command_template
                    ).lower(),
                    "legacy_command_template_configured": str(codex_backend.has_legacy_command_template_configured()).lower(),
                    "timeout_seconds": str(self.config.backends.codex_cli.timeout_seconds),
                    "smoke_test_timeout_seconds": str(self.config.backends.codex_cli.smoke_test_timeout_seconds),
                },
            )
        )
        runtime_warning = codex_backend.codex_runtime_warning()
        if runtime_warning and not (run_smoke_tests and self.config.backends.codex_cli.enabled):
            report.warnings.append(runtime_warning)
        if codex_backend._uses_legacy_command_template():
            report.warnings.append(
                "codex_cli runtime is explicitly using legacy command_template mode. Doctor smoke tests still run "
                "through the newer direct read-only path."
            )

        current_repo_path = self.root if (self.root / ".git").exists() else None
        integration_summary = self._discover_integration_summary(self.root, current_repo_path)
        report.checks.append(
            DiagnosticCheck(
                name="integration_discovery",
                ok=True,
                status=integration_summary.status(),
                summary=(
                    "Integration discovery completed for the current workspace."
                    if integration_summary.capabilities or integration_summary.config_paths
                    else "No project or user integration configuration was discovered for the current workspace."
                ),
                details={
                    "workspace_path": integration_summary.workspace_path,
                    "repo_path": integration_summary.repo_path or "",
                    "config_paths": ", ".join(integration_summary.config_paths),
                    "capability_count": str(len(integration_summary.capabilities)),
                },
            )
        )

        if run_smoke_tests and self.config.backends.codex_cli.enabled:
            smoke_test = self.run_codex_smoke_test()
            report.smoke_tests["codex_cli"] = smoke_test
            report.warnings.extend(smoke_test.warnings)

        enabled_coding_backends = {
            name for name in self.backends.keys() if name in {"agent_sdk", "claude_code_cli", "codex_cli"}
        }
        if not enabled_coding_backends:
            report.warnings.append("No coding-oriented backend is currently enabled.")
        if git_path is None and (
            self.config.backends.codex_cli.use_git_worktree
            or any(project.default_use_git_worktree for project in self.repository.list_saved_projects())
        ):
            report.warnings.append("Git worktree defaults are configured, but git is not available on PATH.")

        return report

    def inspect(self, job_id: str) -> JobInspection:
        job = self.repository.get_job(job_id)
        integration_summary = self.repository.get_workspace_integration_summary_for_job(job_id)
        stream_events = self.repository.get_stream_events(job_id, limit=200)
        latest_stream_event = self.repository.get_latest_stream_event(job_id)
        latest_phase, latest_progress_message = self._summarize_stream_activity(stream_events)
        return JobInspection(
            job=job,
            state=self.repository.get_conversation_state(job_id),
            runs=self.repository.get_job_runs(job_id),
            events=self.repository.get_scheduler_events(job_id),
            stream_events=stream_events,
            latest_stream_event=latest_stream_event,
            latest_phase=latest_phase,
            latest_progress_message=latest_progress_message,
            artifacts=self.repository.get_artifacts(job_id),
            integration_summary=integration_summary,
            backend_integration_support=self.describe_backend_integrations(
                job.backend,
                integration_summary=integration_summary,
            ),
        )

    def retry_due(self) -> int:
        return self.repository.requeue_due_retries()

    def cancel(self, job_id: str) -> Job:
        job = self.repository.request_cancel(job_id)
        if job.status == JobStatus.CANCELLED:
            self._maybe_cleanup_workspace(job, outcome="cancelled")
            return self.repository.get_job(job_id)
        return job

    def retry_job(self, job_id: str) -> Job:
        return self.repository.manual_retry(job_id)

    def purge_completed(self, older_than_days: int = 7) -> int:
        return self.repository.purge_completed(older_than_days=older_than_days)

    def export_logs(self, destination: Path) -> int:
        payload = self.repository.export_logs()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return len(payload)

    def recover_stale_jobs(self) -> List[str]:
        stale_before = utcnow()
        recovered: List[str] = []
        for job in self.repository.recover_stale_running_jobs(stale_before):
            if job.backend not in self.backends:
                self.repository.update_job_status(
                    job.id,
                    target_status=JobStatus.FAILED,
                    current_status=JobStatus.RUNNING,
                    last_error_code="backend_disabled",
                    last_error_message=f"Backend {job.backend} is not currently enabled.",
                    clear_cancel=True,
                    event_type="job.recovery.failed",
                    event_detail={"reason": "backend_disabled"},
                )
                self._ingest_project_manager_outcome(job.id, error_reason="backend_disabled")
                recovered.append(job.id)
                continue
            state = self.repository.get_conversation_state(job.id)
            backend = self._get_backend(job.backend)
            if backend.can_resume(job, state):
                self.repository.update_job_status(
                    job.id,
                    target_status=JobStatus.QUEUED,
                    current_status=JobStatus.RUNNING,
                    clear_cancel=True,
                    event_type="job.recovery.requeued",
                    event_detail={"action": "resume", "reason": "startup_recovery"},
                    metadata_updates={"resume_hint": "recoverable_resume"},
                )
            else:
                decision = self.retry_policy.schedule_retry(
                    max(job.attempt_count, 1),
                    now=utcnow(),
                    reason="startup_recovery_retry",
                    error_code="startup_recovery",
                    error_message="Recovered stale running job after restart.",
                    headers={},
                )
                self.repository.update_job_status(
                    job.id,
                    target_status=JobStatus.WAITING_RETRY,
                    current_status=JobStatus.RUNNING,
                    next_retry_at=decision.retry_at,
                    last_error_code=decision.error_code,
                    last_error_message=decision.error_message,
                    clear_cancel=True,
                    event_type="job.recovery.retry_scheduled",
                    event_detail={
                        "action": "retry",
                        "reason": decision.reason,
                        "retry_at": decision.retry_at.isoformat() if decision.retry_at else None,
                    },
                    metadata_remove_keys=("resume_hint",),
                )
            recovered.append(job.id)
        return recovered

    async def process_claimed_job(self, job: Job, worker_id: str) -> Job:
        backend = self._get_backend(job.backend)
        job, integration_summary = self._refresh_job_integration_summary(job)
        state = self.repository.get_conversation_state(job.id)
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
            integration_summary=integration_summary,
            emit_stream_event=lambda event_type, phase, message, metadata: self._emit_stream_event(
                job,
                event_type=event_type,
                phase=phase,
                message=message,
                metadata=metadata,
            ),
        )
        heartbeat = LeaseHeartbeat(
            self.repository,
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=self.config.effective_lease_seconds(),
            heartbeat_interval_seconds=max(
                0.01,
                min(
                    self.config.worker.heartbeat_interval_seconds,
                    self.config.effective_lease_seconds() / 3,
                ),
            ),
        )
        run_id: Optional[str] = None
        request_payload: Dict[str, Any] = {}
        try:
            run_id = self.repository.record_run_start(job, request_payload={"status": "starting"})
            if job.cancel_requested:
                raise UserCancelledError("Cancellation requested before execution started.")
            heartbeat.start()
            logger.info(
                "job.execution.started",
                extra={
                    "context": {
                        "job_id": job.id,
                        "provider": job.provider,
                        "backend": job.backend,
                        "worker_id": worker_id,
                    }
                },
            )
            if self._should_continue_job(job, state, backend):
                result = await backend.continue_job(job, state, context)
            else:
                result = await backend.submit(job, state, context)
            if heartbeat.lost_lease:
                raise RetryableBackendError("Lost job lease during execution; reschedule for safe recovery.")
            request_payload = result.request_payload
            self._persist_success(job, result)
            self.repository.finish_run(
                run_id,
                request_payload=result.request_payload,
                response_summary=result.response_summary,
                usage=result.usage,
                headers=result.headers,
                error=result.error,
                exit_reason=result.exit_reason,
            )
            logger.info(
                "job.execution.completed",
                extra={
                    "context": {
                        "job_id": job.id,
                        "provider": job.provider,
                        "backend": job.backend,
                        "status": result.status.value,
                    }
                },
            )
            persisted_job = self.repository.get_job(job.id)
            await self._continue_manager_autonomy_now(persisted_job, worker_id)
            return persisted_job
        except asyncio.CancelledError:
            await heartbeat.stop()
            self._persist_interrupted(job, reason="worker_shutdown_interrupted")
            if run_id is not None:
                self.repository.finish_run(
                    run_id,
                    request_payload=request_payload,
                    response_summary={},
                    usage={},
                    headers={},
                    error={
                        "type": "CancelledError",
                        "message": "Job execution interrupted during worker shutdown.",
                        "reason": "worker_shutdown_interrupted",
                    },
                    exit_reason="worker_shutdown_interrupted",
                )
            logger.warning(
                "job.execution.interrupted",
                extra={
                    "context": {
                        "job_id": job.id,
                        "provider": job.provider,
                        "backend": job.backend,
                        "worker_id": worker_id,
                    }
                },
            )
            raise
        except Exception as error:
            await heartbeat.stop()
            setattr(error, "_job_attempt_count", job.attempt_count)
            setattr(error, "_job_max_attempts", job.max_attempts)
            decision = backend.classify_error(error)
            self._persist_failure(job, error, decision)
            if run_id is not None:
                self.repository.finish_run(
                    run_id,
                    request_payload=request_payload,
                    response_summary={},
                    usage={},
                    headers=decision.headers,
                    error={
                        "type": error.__class__.__name__,
                        "message": str(error),
                        "reason": decision.reason,
                    },
                    exit_reason=decision.reason,
                )
            persisted_job = self.repository.get_job(job.id)
            await self._continue_manager_autonomy_now(persisted_job, worker_id)
            return persisted_job
        finally:
            await heartbeat.stop()

    async def _continue_manager_autonomy_now(self, job: Job, worker_id: str) -> None:
        if not bool(job.metadata.get("project_manager_auto_launched")):
            return
        project_id = str(job.metadata.get("project_id") or "").strip()
        if not project_id:
            return
        try:
            project = self.repository.get_saved_project(project_id)
        except KeyError:
            return
        if self.project_manager._normalize_autonomy_mode(project.autonomy_mode) != "full":
            return
        if not project.allow_extra_usage and self._last_run_used_extra_usage(job):
            return
        session = self.describe_project_manager_session(project_id)
        next_job_id = session.active_managed_job_id
        if not next_job_id or next_job_id == job.id:
            return
        try:
            next_job = self.repository.get_job(next_job_id)
        except KeyError:
            return
        if next_job.status != JobStatus.QUEUED:
            return
        start_result = self.try_start_manager_job_now(
            next_job_id,
            worker_id=worker_id,
            launch_context="manager_autonomy",
        )
        if start_result.claimed_job is None:
            return
        await self.process_claimed_job(start_result.claimed_job, worker_id)

    async def submit_message_batch(self, jobs: Sequence[Job], worker_id: str) -> List[Job]:
        if not jobs:
            return []
        if "message_batches" not in self.backends:
            raise KeyError("message_batches backend is not enabled.")
        backend = self._get_backend("message_batches")
        if not isinstance(backend, BatchCapableBackend):
            raise TypeError("message_batches backend does not implement batch submission.")
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
        )
        states = [(job, self.repository.get_conversation_state(job.id)) for job in jobs]
        try:
            submission = await backend.submit_batch(states, context)
        except asyncio.CancelledError:
            for job in jobs:
                self._persist_interrupted(job, reason="worker_shutdown_interrupted", expected_status=JobStatus.RUNNING)
            raise
        batch_record_id = self.repository.create_batch_record(
            backend="message_batches",
            upstream_batch_id=submission.upstream_batch_id,
            status="submitted",
            model=jobs[0].model or self.config.backends.messages_api.model,
            request_signature=submission.batch_id,
            custom_id_map=submission.custom_id_map,
            request_payload=submission.request_payload,
        )
        due_at = utcnow() + timedelta(seconds=self.config.backends.message_batches.poll_interval_seconds)
        for custom_id, job_id in submission.custom_id_map.items():
            job = self.repository.get_job(job_id)
            self.repository.update_job_status(
                job_id,
                target_status=JobStatus.WAITING_RETRY,
                current_status=JobStatus.RUNNING,
                next_retry_at=due_at,
                clear_cancel=True,
                event_type="batch_submitted",
                event_detail={
                    "batch_record_id": batch_record_id,
                    "upstream_batch_id": submission.upstream_batch_id,
                    "custom_id": custom_id,
                    "next_poll_at": due_at.isoformat(),
                },
                metadata_updates={
                    "batch_record_id": batch_record_id,
                    "upstream_batch_id": submission.upstream_batch_id,
                    "batch_custom_id": custom_id,
                },
            )
            run_id = self.repository.record_run_start(job, request_payload=submission.request_payload)
            self.repository.finish_run(
                run_id,
                request_payload=submission.request_payload,
                response_summary={
                    "batch_record_id": batch_record_id,
                    "upstream_batch_id": submission.upstream_batch_id,
                },
                usage={},
                headers={},
                error={},
                exit_reason="batch_submitted",
            )
        return [self.repository.get_job(job.id) for job in jobs]

    async def poll_open_batches(self, worker_id: str) -> int:
        if "message_batches" not in self.backends:
            return 0
        backend = self._get_backend("message_batches")
        if not isinstance(backend, BatchCapableBackend):
            return 0
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
        )
        processed = 0
        for record in self.repository.list_open_batches():
            poll_result = await backend.poll_batch(
                record["upstream_batch_id"],
                record["custom_id_map"],
                context,
            )
            self.repository.update_batch_record(
                record["id"],
                status=poll_result.status,
                response_payload={
                    "completed": poll_result.completed_custom_ids,
                    "failed": poll_result.failed_custom_ids,
                    "pending": poll_result.still_pending_custom_ids,
                },
                error={},
            )
            for custom_id, payload in poll_result.completed_custom_ids.items():
                job_id = record["custom_id_map"][custom_id]
                job = self.repository.get_job(job_id)
                state = self.repository.get_conversation_state(job_id)
                result = backend.parse_completed_result(job, state, payload)
                self._persist_success(job, result, expected_status=JobStatus.WAITING_RETRY)
                run_id = self.repository.record_run_start(job, request_payload={"batch_poll": record["upstream_batch_id"]})
                self.repository.finish_run(
                    run_id,
                    request_payload={"batch_poll": record["upstream_batch_id"]},
                    response_summary=result.response_summary,
                    usage=result.usage,
                    headers=poll_result.headers,
                    error={},
                    exit_reason=result.exit_reason,
                )
                processed += 1
            for custom_id, payload in poll_result.failed_custom_ids.items():
                job_id = record["custom_id_map"][custom_id]
                job = self.repository.get_job(job_id)
                error = RuntimeError(payload.get("error", payload.get("message", "Batch item failed.")))
                setattr(error, "_job_attempt_count", job.attempt_count)
                setattr(error, "_job_max_attempts", job.max_attempts)
                decision = backend.classify_error(error)
                self._persist_failure(job, error, decision, expected_status=JobStatus.WAITING_RETRY)
                processed += 1
            if poll_result.still_pending_custom_ids:
                due_at = utcnow() + timedelta(seconds=self.config.backends.message_batches.poll_interval_seconds)
                for custom_id in poll_result.still_pending_custom_ids:
                    job_id = record["custom_id_map"][custom_id]
                    job = self.repository.get_job(job_id)
                    if job.status != JobStatus.WAITING_RETRY:
                        continue
                    self.repository.update_job_status(
                        job_id,
                        target_status=JobStatus.WAITING_RETRY,
                        current_status=JobStatus.WAITING_RETRY,
                        next_retry_at=due_at,
                        clear_lease=False,
                        event_type="batch_poll_pending",
                        event_detail={
                            "upstream_batch_id": record["upstream_batch_id"],
                            "next_poll_at": due_at.isoformat(),
                        },
                    )
            if poll_result.status in {"expired", "failed"}:
                for custom_id in poll_result.still_pending_custom_ids:
                    job_id = record["custom_id_map"][custom_id]
                    job = self.repository.get_job(job_id)
                    if job.status != JobStatus.WAITING_RETRY:
                        continue
                    self.repository.update_job_status(
                        job_id,
                        target_status=JobStatus.FAILED,
                        current_status=JobStatus.WAITING_RETRY,
                        last_error_code="batch_incomplete",
                        last_error_message=f"Batch {record['upstream_batch_id']} ended as {poll_result.status}.",
                        clear_lease=True,
                        event_type="batch_incomplete",
                        event_detail={"status": poll_result.status},
                    )
                    self._ingest_project_manager_outcome(job_id, error_reason="batch_incomplete")
                    processed += 1
        return processed

    def describe_backend_integrations(
        self,
        backend_name: str,
        *,
        integration_summary: Optional[WorkspaceIntegrationSummary] = None,
    ) -> Dict[str, Any]:
        backend = self.backends.get(backend_name)
        supports_workspace_integrations = bool(getattr(backend, "supports_workspace_integrations", False))
        supports_project_mcp = bool(getattr(backend, "supports_project_mcp", False))
        supports_user_mcp = bool(getattr(backend, "supports_user_mcp", False))
        if not supports_workspace_integrations:
            effective_mode = "local-only backend"
        elif integration_summary is None:
            effective_mode = "integration-capable"
        elif integration_summary.external_capabilities():
            effective_mode = "integration-aware"
        else:
            effective_mode = "integration-capable, no configured external integrations discovered"
        return {
            "supports_workspace_integrations": supports_workspace_integrations,
            "supports_project_mcp": supports_project_mcp,
            "supports_user_mcp": supports_user_mcp,
            "effective_mode": effective_mode,
        }

    def _ingest_project_manager_outcome(
        self,
        job_id: str,
        *,
        response_summary: Optional[Dict[str, Any]] = None,
        needs_followup: bool = False,
        followup_reason: Optional[str] = None,
        error_reason: Optional[str] = None,
    ) -> None:
        try:
            snapshot = self.project_manager.ingest_job_outcome(
                job_id,
                response_summary=response_summary,
                needs_followup=needs_followup,
                followup_reason=followup_reason,
                error_reason=error_reason,
            )
            if snapshot is None:
                return
            job = self.repository.get_job(job_id)
            project_id = str(job.metadata.get("project_id") or "").strip()
            if not project_id:
                return
            project = self.repository.get_saved_project(project_id)
            autonomy_mode = self.project_manager._normalize_autonomy_mode(project.autonomy_mode)
            session_id = str(job.metadata.get("project_manager_autonomy_session_id") or "").strip() or snapshot.state.active_autonomy_session_id
            if not bool(job.metadata.get("project_manager_auto_launched")):
                return
            if autonomy_mode == "partial":
                self.project_manager.update_workflow_state(
                    project_id,
                    workflow_state=(
                        "blocked_on_manual_test"
                        if snapshot.state.needs_manual_testing
                        else "awaiting_continue_decision"
                    ),
                    active_job_id=None,
                    active_autonomy_session_id=session_id or None,
                    auto_tasks_run_count=max(snapshot.state.auto_tasks_run_count, 1),
                )
                return
            if autonomy_mode == "full":
                refreshed = self.project_manager.update_workflow_state(
                    project_id,
                    workflow_state="idle",
                    active_job_id=None,
                    active_autonomy_session_id=session_id or None,
                    auto_tasks_run_count=max(snapshot.state.auto_tasks_run_count, 1),
                )
                self._progress_project_manager_autonomy(
                    project,
                    refreshed,
                    trigger="outcome",
                    session_id=session_id or None,
                )
        except Exception:
            logger.exception(
                "project_manager.ingest_failed",
                extra={"context": {"job_id": job_id}},
            )

    def _progress_project_manager_autonomy(
        self,
        project: SavedProject,
        snapshot,
        *,
        trigger: str,
        require_draft: bool = False,
        session_id: Optional[str] = None,
    ):
        response = snapshot.state.latest_response
        autonomy_mode = self.project_manager._normalize_autonomy_mode(project.autonomy_mode)
        if response is None:
            return snapshot
        if require_draft and (response.decision != "launch_followup_job" or response.draft_task is None):
            raise ValueError("This project does not currently have a recommended task ready to continue.")
        if trigger == "message":
            if snapshot.state.workflow_state == "running_current_task" and snapshot.state.active_job_id:
                return self.project_manager.update_workflow_state(
                    project.id,
                    workflow_state="running_current_task",
                    active_job_id=snapshot.state.active_job_id,
                    active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                    auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
                )
            if snapshot.state.workflow_state == "awaiting_continue_decision":
                return self.project_manager.update_workflow_state(
                    project.id,
                    workflow_state="awaiting_continue_decision",
                    active_job_id=None,
                    active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                    auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
                )
            if snapshot.state.workflow_state == "blocked_on_manual_test":
                return self.project_manager.update_workflow_state(
                    project.id,
                    workflow_state="blocked_on_manual_test",
                    active_job_id=None,
                    active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                    auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
                )
        if response.needs_manual_testing or response.decision == "request_manual_test":
            if autonomy_mode == "full" and response.decision != "request_manual_test":
                pass
            else:
                return self.project_manager.update_workflow_state(
                    project.id,
                    workflow_state="blocked_on_manual_test",
                    active_job_id=None,
                    active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                    auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
                )
        if response.decision != "launch_followup_job" or response.draft_task is None:
            fallback_state = "awaiting_continue_decision" if autonomy_mode == "partial" and trigger == "outcome" else "idle"
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state=fallback_state,
                active_job_id=None,
                active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
            )
        if autonomy_mode == "minimal":
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state="awaiting_confirmation",
                active_job_id=None,
                active_autonomy_session_id=None,
                auto_tasks_run_count=0,
            )
        if autonomy_mode == "partial" and trigger == "message":
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state="awaiting_confirmation",
                active_job_id=None,
                active_autonomy_session_id=None,
                auto_tasks_run_count=0,
            )
        if response.phase == "blocked":
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state="awaiting_confirmation",
                active_job_id=None,
                active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
            )
        if response.confidence == "low" and (autonomy_mode == "full" or trigger == "message"):
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state="awaiting_confirmation",
                active_job_id=None,
                active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
            )
        if autonomy_mode == "partial" and trigger not in {"message", "continue"}:
            return self.project_manager.update_workflow_state(
                project.id,
                workflow_state="awaiting_continue_decision",
                active_job_id=None,
                active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                auto_tasks_run_count=max(snapshot.state.auto_tasks_run_count, 1),
            )
        if autonomy_mode == "full":
            recent_failed = sum(1 for event in snapshot.recent_events[:2] if event.outcome_status == JobStatus.FAILED.value)
            if recent_failed >= 2 or snapshot.state.auto_tasks_run_count >= self.PROJECT_MANAGER_FULL_AUTONOMY_LIMIT:
                return self.project_manager.update_workflow_state(
                    project.id,
                    workflow_state="awaiting_confirmation",
                    active_job_id=None,
                active_autonomy_session_id=session_id or snapshot.state.active_autonomy_session_id,
                auto_tasks_run_count=snapshot.state.auto_tasks_run_count,
            )
        if trigger == "message":
            active_session_id = str(uuid.uuid4())
            next_count = 1
        else:
            active_session_id = session_id or snapshot.state.active_autonomy_session_id or str(uuid.uuid4())
            next_count = max(snapshot.state.auto_tasks_run_count, 0) + 1
        draft = response.draft_task
        job = self.launch_job_from_project(
            project.id,
            prompt=draft.prompt,
            task_type=draft.task_type,
            priority=draft.priority,
            backend=draft.backend,
            provider=draft.provider,
            use_git_worktree=draft.use_git_worktree,
            base_branch=draft.base_branch,
            extra_metadata=self._sanitize_enqueue_metadata(
                {
                    "execution_mode": draft.execution_mode,
                    "project_manager_decision": response.decision,
                    "project_manager_reason": response.reason,
                    "project_manager_derived_from_operator_message": draft.derived_from_operator_message,
                    "project_manager_auto_launched": True,
                    "project_manager_autonomy_mode": autonomy_mode,
                    "project_manager_autonomy_session_id": active_session_id,
                    "project_manager_autonomy_trigger": trigger,
                }
            ),
        )
        return self.project_manager.update_workflow_state(
            project.id,
            workflow_state="running_current_task",
            active_job_id=job.id,
            active_autonomy_session_id=active_session_id,
            auto_tasks_run_count=next_count,
        )

    def _persist_success(
        self,
        job: Job,
        result: BackendResult,
        *,
        expected_status: JobStatus = JobStatus.RUNNING,
    ) -> None:
        if result.updated_state is not None:
            self.repository.save_conversation_state(result.updated_state)
        workspace = prepare_job_workspace(self.config.workspace_path(self.root), job.id, job.metadata).workspace_path
        if result.output and self.config.privacy.store_response_artifacts:
            response_path = store_response_artifact(workspace, result.output)
            result.artifacts.append(
                ArtifactRecord(
                    id=None,
                    job_id=job.id,
                    created_at=utcnow(),
                    kind="response_text",
                    path=str(response_path),
                    metadata={"source": job.backend, "provider": job.provider},
                )
            )
        for artifact in result.artifacts:
            self.repository.add_artifact(artifact)
        latest_job = self.repository.get_job(job.id)
        if latest_job.cancel_requested:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.CANCELLED,
                current_status=latest_job.status,
                clear_cancel=True,
                event_type="job.execution.cancelled",
                event_detail={"reason": "cancel_requested_while_running"},
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
            return
        if result.status == JobStatus.COMPLETED:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.COMPLETED,
                current_status=expected_status,
                clear_cancel=True,
                last_error_code=None,
                last_error_message=None,
                event_type="job.execution.completed",
                event_detail={
                    "needs_followup": result.needs_followup,
                    "followup_reason": result.followup_reason,
                },
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
            self._maybe_cleanup_workspace(job, outcome="completed")
            self._ingest_project_manager_outcome(
                job.id,
                response_summary=result.response_summary,
                needs_followup=result.needs_followup,
                followup_reason=result.followup_reason,
            )
        elif result.status == JobStatus.QUEUED:
            metadata_updates, metadata_remove_keys = self._followup_metadata(job, result)
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.QUEUED,
                current_status=expected_status,
                clear_cancel=True,
                last_error_code=None,
                last_error_message=None,
                event_type="job.followup.scheduled",
                event_detail={"followup_reason": result.followup_reason},
                metadata_updates=metadata_updates,
                metadata_remove_keys=metadata_remove_keys,
            )
        elif result.status == JobStatus.WAITING_RETRY:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.WAITING_RETRY,
                current_status=expected_status,
                next_retry_at=utcnow() + timedelta(seconds=self.config.worker.poll_interval_seconds),
                clear_cancel=True,
                event_type="job.retry.waiting",
                event_detail={"followup_reason": result.followup_reason},
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
        else:
            self.repository.update_job_status(
                job.id,
                target_status=result.status,
                current_status=expected_status,
                clear_cancel=True,
                event_type="job.state.changed",
                event_detail={"target_status": result.status.value},
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
            if result.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                self._maybe_cleanup_workspace(job, outcome=result.status.value)
            if result.status == JobStatus.FAILED:
                self._ingest_project_manager_outcome(
                    job.id,
                    response_summary=result.response_summary,
                    error_reason=result.exit_reason,
                )

    def _persist_failure(
        self,
        job: Job,
        error: Exception,
        decision: Any,
        *,
        expected_status: JobStatus = JobStatus.RUNNING,
    ) -> None:
        logger.error(
            "job.execution.failed",
            extra={
                "context": {
                    "job_id": job.id,
                    "provider": job.provider,
                    "backend": job.backend,
                    "decision": getattr(decision, "reason", None),
                    "error": str(error),
                }
            },
        )
        if decision.disposition == RetryDisposition.RETRY:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.WAITING_RETRY,
                current_status=expected_status,
                next_retry_at=decision.retry_at,
                last_error_code=decision.error_code,
                last_error_message=decision.error_message,
                clear_cancel=True,
                event_type="job.retry.scheduled",
                event_detail={
                    "reason": decision.reason,
                    "retry_at": decision.retry_at.isoformat() if decision.retry_at else None,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "headers": decision.headers,
                },
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
        elif decision.disposition == RetryDisposition.CANCEL:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.CANCELLED,
                current_status=expected_status,
                last_error_code=decision.error_code,
                last_error_message=decision.error_message,
                clear_cancel=True,
                event_type="job.execution.cancelled",
                event_detail={"reason": decision.reason},
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
            self._maybe_cleanup_workspace(job, outcome="cancelled")
        else:
            resume_at = self._check_auto_resume_on_quota(job, decision)
            if resume_at is not None:
                self.repository.update_job_status(
                    job.id,
                    target_status=JobStatus.WAITING_RETRY,
                    current_status=expected_status,
                    next_retry_at=resume_at,
                    last_error_code=decision.error_code,
                    last_error_message=f"Rate limited — auto-resume scheduled at {resume_at.isoformat()}",
                    clear_cancel=True,
                    event_type="job.auto_resume.scheduled",
                    event_detail={
                        "reason": "auto_resume_on_quota",
                        "resume_at": resume_at.isoformat(),
                        "original_reason": decision.reason,
                    },
                    metadata_updates={"auto_resume": True, "resume_hint": "quota_reset"},
                )
                self._emit_stream_event(
                    job,
                    event_type="info",
                    phase="waiting",
                    message=f"Rate limited. Auto-resume scheduled for quota reset.",
                    metadata={"resume_at": resume_at.isoformat()},
                )
                return
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.FAILED,
                current_status=expected_status,
                last_error_code=decision.error_code,
                last_error_message=decision.error_message,
                clear_cancel=True,
                event_type="job.execution.failed_final",
                event_detail={"reason": decision.reason, "headers": decision.headers},
                metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
            )
            self._maybe_cleanup_workspace(job, outcome="failed")
            self._ingest_project_manager_outcome(
                job.id,
                error_reason=decision.reason,
            )

    def _persist_interrupted(
        self,
        job: Job,
        *,
        reason: str,
        expected_status: JobStatus = JobStatus.RUNNING,
    ) -> None:
        decision = self.retry_policy.schedule_retry(
            max(job.attempt_count, 1),
            now=utcnow(),
            reason=reason,
            error_code="worker_shutdown",
            error_message="Worker shutdown interrupted job execution.",
            headers={},
        )
        self.repository.update_job_status(
            job.id,
            target_status=JobStatus.WAITING_RETRY,
            current_status=expected_status,
            next_retry_at=decision.retry_at,
            last_error_code=decision.error_code,
            last_error_message=decision.error_message,
            clear_cancel=True,
            event_type="job.execution.interrupted",
            event_detail={
                "reason": reason,
                "retry_at": decision.retry_at.isoformat() if decision.retry_at else None,
            },
            metadata_remove_keys=("followup_type", "followup_reason", "followup_requested_at", "resume_hint"),
        )

    def _last_run_used_extra_usage(self, job: Job) -> bool:
        """Check if the latest run for this job reported extra usage spending."""
        try:
            runs = self.repository.get_job_runs(job.id)
        except Exception:
            return False
        if not runs:
            return False
        latest_run = runs[-1]
        usage = latest_run.usage or {}
        return bool(usage.get("is_extra_usage"))

    def _check_auto_resume_on_quota(self, job: Job, decision: Any) -> Optional[datetime]:
        """If the project has auto-resume enabled and this was a rate limit failure,
        return a datetime to schedule the retry at based on rate limit reset headers."""
        project_id = job.metadata.get("project_id")
        if not project_id:
            return None
        reason = getattr(decision, "reason", "")
        error_code = getattr(decision, "error_code", "") or ""
        headers = getattr(decision, "headers", {}) or {}
        is_rate_limited = (
            "rate_limit" in reason
            or "429" in error_code
            or "rate_limited" in error_code.lower()
        )
        if not is_rate_limited:
            return None
        try:
            project = self.repository.get_saved_project(project_id)
        except KeyError:
            return None
        if not project.auto_resume_on_quota:
            return None
        now = utcnow()
        reset_at = self._parse_quota_reset_time(headers, now)
        if reset_at is not None:
            return reset_at
        # Default: retry in 60 seconds if no reset header found
        return now + timedelta(seconds=60)

    def _parse_quota_reset_time(self, headers: Dict[str, Any], now: datetime) -> Optional[datetime]:
        """Extract reset time from Anthropic rate limit headers."""
        for key, value in headers.items():
            lower = key.lower()
            if "ratelimit" in lower and "reset" in lower:
                parsed = parse_retry_after(str(value), now)
                if parsed is not None:
                    return now + timedelta(seconds=parsed)
        retry_after = None
        for key, value in headers.items():
            if key.lower() == "retry-after":
                retry_after = str(value)
                break
        if retry_after:
            parsed = parse_retry_after(retry_after, now)
            if parsed is not None:
                return now + timedelta(seconds=parsed)
        return None

    def _followup_metadata(self, job: Job, result: BackendResult) -> tuple[Dict[str, Any], Sequence[str]]:
        if result.followup_reason == "max_tokens":
            return (
                {
                    "followup_type": "model_continuation",
                    "followup_reason": result.followup_reason,
                    "followup_requested_at": utcnow().isoformat(),
                },
                ("resume_hint",),
            )
        if result.followup_reason:
            return (
                {"resume_hint": result.followup_reason},
                ("followup_type", "followup_reason", "followup_requested_at"),
            )
        return ({}, ("followup_type", "followup_reason", "followup_requested_at", "resume_hint"))

    def _default_model_for_backend(self, backend_name: str) -> Optional[str]:
        if backend_name in {"messages_api", "message_batches", "agent_sdk"}:
            return self.config.backends.messages_api.model or self.config.model
        return None

    def _sanitize_enqueue_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        sanitized = dict(metadata)
        for key in (
            "workspace_path",
            "worktree_path",
            "workspace_kind",
            "workspace_app_created",
            "branch_name",
            "prompt_artifact_path",
            "integration_summary",
            "followup_type",
            "followup_reason",
            "followup_requested_at",
            "resume_hint",
            "batch_record_id",
            "upstream_batch_id",
            "batch_custom_id",
        ):
            sanitized.pop(key, None)
        return {
            key: value
            for key, value in sanitized.items()
            if value not in (None, "")
        }

    def _discover_integration_summary(
        self,
        workspace_path: Path,
        repo_path: Optional[Path],
    ) -> WorkspaceIntegrationSummary:
        return discover_workspace_integrations(
            workspace_path,
            repo_path=repo_path,
        )

    def _refresh_job_integration_summary(
        self,
        job: Job,
    ) -> tuple[Job, Optional[WorkspaceIntegrationSummary]]:
        workspace_path_value = job.workspace_path or job.metadata.get("workspace_path")
        if not workspace_path_value:
            return job, None
        repo_path_value = job.metadata.get("repo_path")
        repo_path = Path(str(repo_path_value)).expanduser() if repo_path_value else None
        integration_summary = self._discover_integration_summary(
            Path(str(workspace_path_value)).expanduser(),
            repo_path,
        )
        integration_snapshot = build_integration_metadata_summary(integration_summary)
        if job.metadata.get("integration_summary") != integration_snapshot:
            updated_metadata = dict(job.metadata)
            updated_metadata["integration_summary"] = integration_snapshot
            self.repository.set_job_metadata(job.id, updated_metadata)
            job = self.repository.get_job(job.id)
        self._persist_integration_summary(job.id, integration_summary)
        return job, integration_summary

    def _persist_integration_summary(
        self,
        job_id: str,
        integration_summary: WorkspaceIntegrationSummary,
    ) -> None:
        saved = self.repository.save_workspace_integration_summary(integration_summary, job_id=job_id)
        if not saved:
            return
        self.repository.add_scheduler_event(
            job_id,
            "workspace_integrations_discovered",
            {
                "status": integration_summary.status(),
                "labels": integration_summary.status_labels(),
                "config_paths": integration_summary.config_paths,
                "capability_names": [
                    capability.name
                    for capability in integration_summary.external_capabilities()
                ],
            },
        )

    def _emit_stream_event(
        self,
        job: Job,
        *,
        event_type: str,
        phase: Optional[str],
        message: str,
        metadata: Dict[str, object],
    ) -> None:
        self.repository.add_stream_event(
            job_id=job.id,
            provider=job.provider,
            backend=job.backend,
            event_type=event_type,
            phase=phase,
            message=message,
            metadata={str(key): value for key, value in metadata.items()},
        )

    def _summarize_stream_activity(self, stream_events: Sequence[JobStreamEvent]) -> tuple[Optional[str], Optional[str]]:
        latest_phase = next((event.phase for event in reversed(stream_events) if event.phase), None)
        latest_progress_message = next((event.message for event in reversed(stream_events) if event.message), None)
        return latest_phase, latest_progress_message

    def _maybe_cleanup_workspace(self, job: Job, *, outcome: str) -> None:
        latest_job = self.repository.get_job(job.id)
        result = cleanup_job_workspace(
            self.config.workspace_path(self.root),
            latest_job.metadata,
            outcome=outcome,
        )
        if result.cleaned:
            self.repository.add_scheduler_event(
                job.id,
                "workspace.cleanup.completed",
                {
                    "workspace_kind": result.workspace_kind,
                    "reason": result.reason,
                    "cleaned_path": str(result.cleaned_path) if result.cleaned_path else None,
                },
            )
        elif result.reason not in {"cleanup_disabled", "cleanup_skipped_for_outcome"}:
            self.repository.add_scheduler_event(
                job.id,
                "workspace.cleanup.skipped",
                {
                    "workspace_kind": result.workspace_kind,
                    "reason": result.reason,
                },
            )

    def _should_continue_job(
        self,
        job: Job,
        state: ConversationState,
        backend: BackendAdapter,
    ) -> bool:
        if not backend.can_resume(job, state):
            return False
        followup_type = job.metadata.get("followup_type")
        resume_hint = job.metadata.get("resume_hint")
        if followup_type == "model_continuation":
            return True
        if resume_hint in {"recoverable_resume", "checkpoint_available"}:
            return True
        return bool(state.tool_context.get("pending_followup"))

    def _get_backend(self, backend_name: str) -> BackendAdapter:
        backend = self.backends.get(backend_name)
        if backend is None:
            raise KeyError(f"Unknown backend: {backend_name}")
        return backend  # type: ignore[return-value]

"""SQLite-backed repositories for jobs, runs, state, and scheduler events."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .db import Database
from .integrations import WorkspaceIntegrationSummary
from .models import (
    ArtifactRecord,
    ConversationState,
    EnqueueJobRequest,
    Job,
    JobRun,
    JobStreamEvent,
    JobStatus,
    ProviderName,
    SavedProject,
    SchedulerEventRecord,
)
from .state_machine import ensure_transition
from .timeutils import from_iso8601, to_iso8601, utcnow


def _dumps(value: Dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)


def _loads(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


def _loads_list(value: Optional[str]) -> List[Dict[str, Any]]:
    if not value:
        return []
    return json.loads(value)


def _followup_priority(metadata: Dict[str, Any], continuation_priority_bonus: int) -> int:
    if metadata.get("followup_type") == "model_continuation":
        return continuation_priority_bonus
    return 0


class JobRepository:
    """Repository with the durable operations needed by the orchestrator."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create_job(
        self,
        request: EnqueueJobRequest,
        *,
        prompt_to_store: str,
        workspace_path: Optional[Path],
        job_id: Optional[str] = None,
    ) -> Job:
        now = utcnow()
        existing = self.get_job_by_idempotency_key(request.idempotency_key) if request.idempotency_key else None
        if existing:
            return existing

        job_id = job_id or str(uuid.uuid4())
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs (
                    id,
                    created_at,
                    updated_at,
                    status,
                    provider,
                    backend,
                    task_type,
                    priority,
                    prompt,
                    metadata_json,
                    attempt_count,
                    next_retry_at,
                    last_error_code,
                    last_error_message,
                    max_attempts,
                    idempotency_key,
                    model,
                    workspace_path,
                    cancel_requested,
                    lease_owner,
                    lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    to_iso8601(now),
                    to_iso8601(now),
                    JobStatus.QUEUED.value,
                    request.provider or ProviderName.ANTHROPIC.value,
                    request.backend,
                    request.task_type,
                    request.priority,
                    prompt_to_store,
                    _dumps(request.metadata),
                    0,
                    None,
                    None,
                    None,
                    request.max_attempts,
                    request.idempotency_key,
                    request.model,
                    str(workspace_path) if workspace_path else None,
                    0,
                    None,
                    None,
                ),
            )
            connection.execute(
                """
                INSERT INTO conversation_state (
                    job_id,
                    system_prompt,
                    message_history_json,
                    compact_summary,
                    tool_context_json,
                    last_checkpoint_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.system_prompt,
                    "[]",
                    None,
                    "{}",
                    None,
                ),
            )
            connection.execute(
                """
                INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    to_iso8601(now),
                    "job_enqueued",
                    _dumps(
                        {
                            "backend": request.backend,
                            "provider": request.provider or ProviderName.ANTHROPIC.value,
                            "task_type": request.task_type,
                            "priority": request.priority,
                        }
                    ),
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        return self._row_to_job(row)

    def get_job_by_idempotency_key(self, idempotency_key: Optional[str]) -> Optional[Job]:
        if not idempotency_key:
            return None
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        provider: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = 100,
        continuation_priority_bonus: int = 0,
    ) -> List[Job]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if backend:
            clauses.append("backend = ?")
            params.append(backend)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                {where}
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0
                        WHEN 'queued' THEN 1
                        WHEN 'waiting_retry' THEN 2
                        WHEN 'failed' THEN 3
                        WHEN 'completed' THEN 4
                        WHEN 'cancelled' THEN 5
                    END,
                    priority DESC,
                    created_at ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        jobs = [self._row_to_job(row) for row in rows]
        return sorted(
            jobs,
            key=lambda job: (
                0 if job.status == JobStatus.RUNNING else 1,
                -(
                    job.priority
                    + _followup_priority(job.metadata, continuation_priority_bonus)
                ),
                to_iso8601(job.created_at) or "",
            ),
        )[:limit]

    def get_status_counts(self) -> Dict[str, int]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in JobStatus}
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts

    def get_conversation_state(self, job_id: str) -> ConversationState:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_state WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Missing conversation state for job {job_id}")
        return ConversationState(
            job_id=row["job_id"],
            system_prompt=row["system_prompt"],
            message_history=_loads_list(row["message_history_json"]),
            compact_summary=row["compact_summary"],
            tool_context=_loads(row["tool_context_json"]),
            last_checkpoint_at=from_iso8601(row["last_checkpoint_at"]),
        )

    def save_conversation_state(self, state: ConversationState) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE conversation_state
                SET
                    system_prompt = ?,
                    message_history_json = ?,
                    compact_summary = ?,
                    tool_context_json = ?,
                    last_checkpoint_at = ?
                WHERE job_id = ?
                """,
                (
                    state.system_prompt,
                    json.dumps(state.message_history),
                    state.compact_summary,
                    _dumps(state.tool_context),
                    to_iso8601(state.last_checkpoint_at or now),
                    state.job_id,
                ),
            )

    def create_saved_project(
        self,
        *,
        name: str,
        repo_path: str,
        default_backend: Optional[str],
        default_provider: Optional[str],
        default_base_branch: Optional[str],
        default_use_git_worktree: bool,
        notes: Optional[str],
    ) -> SavedProject:
        project_id = str(uuid.uuid4())
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_projects (
                    id,
                    name,
                    repo_path,
                    default_backend,
                    default_provider,
                    default_base_branch,
                    default_use_git_worktree,
                    notes,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    repo_path,
                    default_backend,
                    default_provider,
                    default_base_branch,
                    1 if default_use_git_worktree else 0,
                    notes,
                    to_iso8601(now),
                    to_iso8601(now),
                ),
            )
        return self.get_saved_project(project_id)

    def list_saved_projects(self) -> List[SavedProject]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM saved_projects
                ORDER BY updated_at DESC, name ASC
                """
            ).fetchall()
        return [self._row_to_saved_project(row) for row in rows]

    def get_saved_project(self, project_id: str) -> SavedProject:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM saved_projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return self._row_to_saved_project(row)

    def claim_job(self, job_id: str, worker_id: str, lease_seconds: float = 300) -> Optional[Job]:
        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status, attempt_count FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != JobStatus.QUEUED.value:
                return None
            ensure_transition(JobStatus(row["status"]), JobStatus.RUNNING)
            updated = connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    updated_at = ?,
                    attempt_count = attempt_count + 1,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    cancel_requested = 0
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.RUNNING.value,
                    to_iso8601(now),
                    worker_id,
                    to_iso8601(expires_at),
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                """
                INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    to_iso8601(now),
                    "job_claimed",
                    _dumps({"worker_id": worker_id, "lease_expires_at": to_iso8601(expires_at)}),
                ),
            )
        return self.get_job(job_id)

    def renew_lease(self, job_id: str, worker_id: str, lease_seconds: float) -> bool:
        """Renew a running-job lease only if the current worker still owns it."""

        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE jobs
                SET
                    updated_at = ?,
                    lease_expires_at = ?
                WHERE id = ?
                  AND status = ?
                  AND lease_owner = ?
                """,
                (
                    to_iso8601(now),
                    to_iso8601(expires_at),
                    job_id,
                    JobStatus.RUNNING.value,
                    worker_id,
                ),
            )
        return updated.rowcount == 1

    def renew_job_lease(self, job_id: str, worker_id: str, lease_seconds: float) -> Optional[datetime]:
        """Compatibility wrapper that returns the new expiry on success."""

        expires_at = utcnow() + timedelta(seconds=lease_seconds)
        if not self.renew_lease(job_id, worker_id, lease_seconds):
            return None
        return expires_at

    def list_runnable_jobs(self, limit: int = 100, continuation_priority_bonus: int = 0) -> List[Job]:
        now = utcnow()
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (JobStatus.QUEUED.value, to_iso8601(now), max(limit * 10, 100)),
            ).fetchall()
        jobs = [self._row_to_job(row) for row in rows]
        return sorted(
            jobs,
            key=lambda job: (
                -(
                    job.priority
                    + _followup_priority(job.metadata, continuation_priority_bonus)
                ),
                to_iso8601(job.created_at) or "",
            ),
        )[:limit]

    def record_run_start(self, job: Job, request_payload: Dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_runs (
                    id,
                    job_id,
                    started_at,
                    ended_at,
                    provider,
                    backend,
                    request_payload_json,
                    response_summary_json,
                    usage_json,
                    headers_json,
                    error_json,
                    exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job.id,
                    to_iso8601(now),
                    None,
                    job.provider,
                    job.backend,
                    _dumps(request_payload),
                    "{}",
                    "{}",
                    "{}",
                    "{}",
                    None,
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        request_payload: Dict[str, Any],
        response_summary: Dict[str, Any],
        usage: Dict[str, Any],
        headers: Dict[str, Any],
        error: Dict[str, Any],
        exit_reason: Optional[str],
    ) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE job_runs
                SET
                    ended_at = ?,
                    request_payload_json = ?,
                    response_summary_json = ?,
                    usage_json = ?,
                    headers_json = ?,
                    error_json = ?,
                    exit_reason = ?
                WHERE id = ?
                """,
                (
                    to_iso8601(now),
                    _dumps(request_payload),
                    _dumps(response_summary),
                    _dumps(usage),
                    _dumps(headers),
                    _dumps(error),
                    exit_reason,
                    run_id,
                ),
            )

    def update_job_status(
        self,
        job_id: str,
        *,
        target_status: JobStatus,
        current_status: Optional[JobStatus] = None,
        next_retry_at: Optional[datetime] = None,
        last_error_code: Optional[str] = None,
        last_error_message: Optional[str] = None,
        clear_lease: bool = True,
        clear_cancel: bool = False,
        event_type: Optional[str] = None,
        event_detail: Optional[Dict[str, Any]] = None,
        metadata_updates: Optional[Dict[str, Any]] = None,
        metadata_remove_keys: Optional[Sequence[str]] = None,
    ) -> None:
        job = self.get_job(job_id)
        if current_status is not None and job.status != current_status:
            raise ValueError(f"Expected {current_status.value}, got {job.status.value}")
        ensure_transition(job.status, target_status)
        now = utcnow()
        updated_metadata = dict(job.metadata)
        for key in metadata_remove_keys or ():
            updated_metadata.pop(key, None)
        for key, value in (metadata_updates or {}).items():
            updated_metadata[key] = value
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    updated_at = ?,
                    next_retry_at = ?,
                    last_error_code = ?,
                    last_error_message = ?,
                    metadata_json = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    cancel_requested = CASE WHEN ? THEN 0 ELSE cancel_requested END
                WHERE id = ?
                """,
                (
                    target_status.value,
                    to_iso8601(now),
                    to_iso8601(next_retry_at),
                    last_error_code,
                    last_error_message,
                    _dumps(updated_metadata),
                    None if clear_lease else job.lease_owner,
                    None if clear_lease else to_iso8601(job.lease_expires_at),
                    1 if clear_cancel else 0,
                    job_id,
                ),
            )
            if event_type:
                connection.execute(
                    """
                    INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        to_iso8601(now),
                        event_type,
                        _dumps(event_detail or {}),
                    ),
                )

    def request_cancel(self, job_id: str, reason: str = "cancelled_by_operator") -> Job:
        job = self.get_job(job_id)
        now = utcnow()
        updated_metadata = dict(job.metadata)
        for key in ("followup_type", "followup_reason", "followup_requested_at", "resume_hint"):
            updated_metadata.pop(key, None)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if job.status == JobStatus.RUNNING:
                connection.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested = 1, updated_at = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (to_iso8601(now), _dumps(updated_metadata), job_id),
                )
                connection.execute(
                    """
                    INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        to_iso8601(now),
                        "cancel_requested",
                        _dumps({"reason": reason}),
                    ),
                )
            elif job.status in {JobStatus.QUEUED, JobStatus.WAITING_RETRY, JobStatus.FAILED}:
                ensure_transition(job.status, JobStatus.CANCELLED)
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, next_retry_at = NULL, metadata_json = ?
                    WHERE id = ?
                    """,
                    (JobStatus.CANCELLED.value, to_iso8601(now), _dumps(updated_metadata), job_id),
                )
                connection.execute(
                    """
                    INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        job_id,
                        to_iso8601(now),
                        "job_cancelled",
                        _dumps({"reason": reason}),
                    ),
                )
        return self.get_job(job_id)

    def manual_retry(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.status not in {JobStatus.FAILED, JobStatus.WAITING_RETRY}:
            raise ValueError(f"Job {job_id} cannot be retried from status {job.status.value}")
        now = utcnow()
        updated_metadata = dict(job.metadata)
        for key in ("followup_type", "followup_reason", "followup_requested_at", "resume_hint"):
            updated_metadata.pop(key, None)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_transition(job.status, JobStatus.QUEUED)
            connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    updated_at = ?,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    last_error_message = NULL,
                    metadata_json = ?,
                    cancel_requested = 0
                WHERE id = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    to_iso8601(now),
                    _dumps(updated_metadata),
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    to_iso8601(now),
                    "manual_retry",
                    "{}",
                ),
            )
        return self.get_job(job_id)

    def requeue_due_retries(self, now: Optional[datetime] = None) -> int:
        current = now or utcnow()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            due_rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = ? AND next_retry_at IS NOT NULL AND next_retry_at <= ?
                """,
                (JobStatus.WAITING_RETRY.value, to_iso8601(current)),
            ).fetchall()
            for row in due_rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, next_retry_at = NULL
                    WHERE id = ?
                    """,
                    (JobStatus.QUEUED.value, to_iso8601(current), row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        row["id"],
                        to_iso8601(current),
                        "retry_requeued",
                        "{}",
                    ),
                )
        return len(due_rows)

    def recover_stale_running_jobs(self, stale_before: datetime) -> List[Job]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (JobStatus.RUNNING.value, to_iso8601(stale_before)),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def add_scheduler_event(self, job_id: str, event_type: str, detail: Dict[str, Any]) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_events (id, job_id, timestamp, event_type, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    to_iso8601(now),
                    event_type,
                    _dumps(detail),
                ),
            )

    def add_stream_event(
        self,
        *,
        job_id: str,
        provider: str,
        backend: str,
        event_type: str,
        message: str = "",
        phase: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> JobStreamEvent:
        now = utcnow()
        record = JobStreamEvent(
            id=str(uuid.uuid4()),
            job_id=job_id,
            provider=provider,
            backend=backend,
            timestamp=now,
            event_type=event_type,
            phase=phase,
            message=message,
            metadata=dict(metadata or {}),
        )
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO job_stream_events (
                    id,
                    job_id,
                    provider,
                    backend,
                    timestamp,
                    event_type,
                    phase,
                    message,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.job_id,
                    record.provider,
                    record.backend,
                    to_iso8601(record.timestamp),
                    record.event_type,
                    record.phase,
                    record.message,
                    _dumps(record.metadata),
                ),
            )
        return record

    def add_artifact(self, record: ArtifactRecord) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (id, job_id, created_at, kind, path, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id or str(uuid.uuid4()),
                    record.job_id,
                    to_iso8601(record.created_at),
                    record.kind,
                    record.path,
                    _dumps(record.metadata),
                ),
            )

    def get_job_runs(self, job_id: str) -> List[JobRun]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE job_id = ?
                ORDER BY started_at ASC
                """,
                (job_id,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_scheduler_events(self, job_id: str) -> List[SchedulerEventRecord]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduler_events
                WHERE job_id = ?
                ORDER BY timestamp ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            SchedulerEventRecord(
                id=row["id"],
                job_id=row["job_id"],
                timestamp=from_iso8601(row["timestamp"]) or utcnow(),
                event_type=row["event_type"],
                detail=_loads(row["detail_json"]),
            )
            for row in rows
        ]

    def get_artifacts(self, job_id: str) -> List[ArtifactRecord]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE job_id = ?
                ORDER BY created_at ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            ArtifactRecord(
                id=row["id"],
                job_id=row["job_id"],
                created_at=from_iso8601(row["created_at"]) or utcnow(),
                kind=row["kind"],
                path=row["path"],
                metadata=_loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def save_workspace_integration_summary(
        self,
        summary: WorkspaceIntegrationSummary,
        *,
        job_id: Optional[str] = None,
        source_kind: str = "workspace_scan",
    ) -> bool:
        discovered_at = utcnow()
        summary_json = json.dumps(summary.to_dict(), sort_keys=True)
        with self.db.connect() as connection:
            if job_id:
                row = connection.execute(
                    """
                    SELECT summary_json
                    FROM workspace_integrations
                    WHERE job_id = ?
                    ORDER BY discovered_at DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()
                if row is not None and row["summary_json"] == summary_json:
                    return False
            connection.execute(
                """
                INSERT INTO workspace_integrations (
                    id,
                    job_id,
                    workspace_path,
                    repo_path,
                    discovered_at,
                    source_kind,
                    summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    summary.workspace_path,
                    summary.repo_path,
                    to_iso8601(discovered_at),
                    source_kind,
                    summary_json,
                ),
            )
        return True

    def get_workspace_integration_summary_for_job(self, job_id: str) -> Optional[WorkspaceIntegrationSummary]:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT summary_json
                FROM workspace_integrations
                WHERE job_id = ?
                ORDER BY discovered_at DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceIntegrationSummary.from_dict(json.loads(row["summary_json"]))

    def get_stream_events(self, job_id: str, limit: int = 200) -> List[JobStreamEvent]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_stream_events
                WHERE job_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        events = [self._row_to_stream_event(row) for row in rows]
        return list(reversed(events))

    def get_latest_stream_event(self, job_id: str) -> Optional[JobStreamEvent]:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM job_stream_events
                WHERE job_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_stream_event(row) if row else None

    def export_logs(self) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id AS job_id, j.status, j.provider, j.backend, e.timestamp, e.event_type, e.detail_json
                FROM jobs j
                LEFT JOIN scheduler_events e ON e.job_id = j.id
                ORDER BY e.timestamp ASC
                """
            ).fetchall()
        return [
            {
                "job_id": row["job_id"],
                "status": row["status"],
                "provider": row["provider"] if "provider" in row.keys() else ProviderName.ANTHROPIC.value,
                "backend": row["backend"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "detail": _loads(row["detail_json"]),
            }
            for row in rows
        ]

    def purge_completed(self, older_than_days: int = 7) -> int:
        threshold = utcnow() - timedelta(days=older_than_days)
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status IN (?, ?) AND updated_at <= ?
                """,
                (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, to_iso8601(threshold)),
            ).fetchall()
            for row in rows:
                connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
        return len(rows)

    def list_batch_candidates(self, anchor_job: Job, limit: int) -> List[Job]:
        model = anchor_job.model or anchor_job.metadata.get("model")
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE backend = ?
                  AND provider = ?
                  AND status = ?
                  AND task_type = ?
                  AND COALESCE(model, '') = COALESCE(?, '')
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (
                    anchor_job.backend,
                    anchor_job.provider,
                    JobStatus.QUEUED.value,
                    anchor_job.task_type,
                    model,
                    limit,
                ),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def create_batch_record(
        self,
        *,
        backend: str,
        upstream_batch_id: str,
        status: str,
        model: Optional[str],
        request_signature: str,
        custom_id_map: Dict[str, str],
        request_payload: Dict[str, Any],
    ) -> str:
        batch_id = str(uuid.uuid4())
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO message_batches (
                    id,
                    created_at,
                    updated_at,
                    backend,
                    upstream_batch_id,
                    status,
                    model,
                    request_signature,
                    custom_id_map_json,
                    request_payload_json,
                    response_payload_json,
                    error_json,
                    last_polled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    to_iso8601(now),
                    to_iso8601(now),
                    backend,
                    upstream_batch_id,
                    status,
                    model,
                    request_signature,
                    _dumps(custom_id_map),
                    _dumps(request_payload),
                    "{}",
                    "{}",
                    None,
                ),
            )
        return batch_id

    def update_batch_record(
        self,
        batch_id: str,
        *,
        status: str,
        response_payload: Dict[str, Any],
        error: Dict[str, Any],
    ) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE message_batches
                SET
                    updated_at = ?,
                    status = ?,
                    response_payload_json = ?,
                    error_json = ?,
                    last_polled_at = ?
                WHERE id = ?
                """,
                (
                    to_iso8601(now),
                    status,
                    _dumps(response_payload),
                    _dumps(error),
                    to_iso8601(now),
                    batch_id,
                ),
            )

    def list_open_batches(self) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM message_batches
                WHERE status NOT IN ('completed', 'failed', 'expired')
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "backend": row["backend"],
                "upstream_batch_id": row["upstream_batch_id"],
                "status": row["status"],
                "model": row["model"],
                "request_signature": row["request_signature"],
                "custom_id_map": _loads(row["custom_id_map_json"]),
                "request_payload": _loads(row["request_payload_json"]),
                "response_payload": _loads(row["response_payload_json"]),
                "error": _loads(row["error_json"]),
                "last_polled_at": from_iso8601(row["last_polled_at"]),
            }
            for row in rows
        ]

    def set_job_metadata(self, job_id: str, metadata: Dict[str, Any]) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dumps(metadata), to_iso8601(now), job_id),
            )

    def set_workspace_path(self, job_id: str, workspace_path: Path) -> None:
        now = utcnow()
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET workspace_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(workspace_path), to_iso8601(now), job_id),
            )

    def _row_to_job(self, row: Any) -> Job:
        return Job(
            id=row["id"],
            created_at=from_iso8601(row["created_at"]) or utcnow(),
            updated_at=from_iso8601(row["updated_at"]) or utcnow(),
            status=JobStatus(row["status"]),
            provider=row["provider"] if "provider" in row.keys() else ProviderName.ANTHROPIC.value,
            backend=row["backend"],
            task_type=row["task_type"],
            priority=row["priority"],
            prompt=row["prompt"],
            metadata=_loads(row["metadata_json"]),
            attempt_count=row["attempt_count"],
            next_retry_at=from_iso8601(row["next_retry_at"]),
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
            max_attempts=row["max_attempts"],
            idempotency_key=row["idempotency_key"],
            model=row["model"],
            workspace_path=row["workspace_path"],
            cancel_requested=bool(row["cancel_requested"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=from_iso8601(row["lease_expires_at"]),
        )

    def _row_to_run(self, row: Any) -> JobRun:
        return JobRun(
            id=row["id"],
            job_id=row["job_id"],
            started_at=from_iso8601(row["started_at"]) or utcnow(),
            ended_at=from_iso8601(row["ended_at"]),
            provider=row["provider"] if "provider" in row.keys() else ProviderName.ANTHROPIC.value,
            backend=row["backend"],
            request_payload=_loads(row["request_payload_json"]),
            response_summary=_loads(row["response_summary_json"]),
            usage=_loads(row["usage_json"]),
            headers=_loads(row["headers_json"]),
            error=_loads(row["error_json"]),
            exit_reason=row["exit_reason"],
        )

    def _row_to_saved_project(self, row: Any) -> SavedProject:
        return SavedProject(
            id=row["id"],
            name=row["name"],
            repo_path=row["repo_path"],
            default_backend=row["default_backend"],
            default_provider=row["default_provider"],
            default_base_branch=row["default_base_branch"],
            default_use_git_worktree=bool(row["default_use_git_worktree"]),
            notes=row["notes"],
            created_at=from_iso8601(row["created_at"]) or utcnow(),
            updated_at=from_iso8601(row["updated_at"]) or utcnow(),
        )

    def _row_to_stream_event(self, row: Any) -> JobStreamEvent:
        return JobStreamEvent(
            id=row["id"],
            job_id=row["job_id"],
            provider=row["provider"],
            backend=row["backend"],
            timestamp=from_iso8601(row["timestamp"]) or utcnow(),
            event_type=row["event_type"],
            phase=row["phase"],
            message=row["message"],
            metadata=_loads(row["metadata_json"]),
        )

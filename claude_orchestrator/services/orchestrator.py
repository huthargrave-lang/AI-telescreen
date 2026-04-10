"""High-level orchestration operations shared by CLI, workers, and web UI."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..config import AppConfig
from ..models import (
    ArtifactRecord,
    BackendResult,
    ConversationState,
    EnqueueJobRequest,
    Job,
    JobStatus,
    RetryDisposition,
)
from ..repository import JobRepository
from ..retry import RetryPolicy, UserCancelledError
from ..timeutils import utcnow
from ..workspaces import ensure_job_workspace, store_private_prompt, store_response_artifact
from ..backends.base import BackendAdapter, BatchCapableBackend, BackendContext

logger = logging.getLogger(__name__)


@dataclass
class JobInspection:
    job: Job
    state: ConversationState
    runs: List[Any]
    events: List[Any]
    artifacts: List[ArtifactRecord]


class OrchestratorService:
    """Coordinates persistence, retries, recovery, and backend execution."""

    def __init__(
        self,
        root: Path,
        config: AppConfig,
        repository: JobRepository,
        backends: Dict[str, object],
    ) -> None:
        self.root = root
        self.config = config
        self.repository = repository
        self.backends = backends
        self.retry_policy = RetryPolicy(config.retry)

    def enqueue(self, request: EnqueueJobRequest) -> Job:
        backend_name = request.backend or self.config.default_backend
        if backend_name not in self.backends:
            raise ValueError(f"Unknown backend: {backend_name}")
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
        if privacy_mode:
            prompt_to_store = "[stored_in_private_artifact]"
        request.backend = backend_name
        request.model = request.model or metadata.get("model") or self.config.model
        request.metadata = metadata
        created = self.repository.create_job(
            request,
            prompt_to_store=prompt_to_store,
            workspace_path=None,
        )
        actual_workspace = ensure_job_workspace(self.config.workspace_path(self.root), created.id)
        self.repository.set_workspace_path(created.id, actual_workspace)
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
            {"workspace_path": str(actual_workspace)},
        )
        return self.repository.get_job(created.id)

    def status(self) -> Dict[str, int]:
        return self.repository.get_status_counts()

    def list_jobs(self, *, status: Optional[str] = None, backend: Optional[str] = None, limit: int = 100) -> List[Job]:
        return self.repository.list_jobs(status=status, backend=backend, limit=limit)

    def inspect(self, job_id: str) -> JobInspection:
        return JobInspection(
            job=self.repository.get_job(job_id),
            state=self.repository.get_conversation_state(job_id),
            runs=self.repository.get_job_runs(job_id),
            events=self.repository.get_scheduler_events(job_id),
            artifacts=self.repository.get_artifacts(job_id),
        )

    def retry_due(self) -> int:
        return self.repository.requeue_due_retries()

    def cancel(self, job_id: str) -> Job:
        return self.repository.request_cancel(job_id)

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
        stale_before = utcnow() - timedelta(seconds=self.config.worker.stale_after_seconds)
        recovered: List[str] = []
        for job in self.repository.recover_stale_running_jobs(stale_before):
            state = self.repository.get_conversation_state(job.id)
            backend = self._get_backend(job.backend)
            if backend.can_resume(job, state):
                self.repository.update_job_status(
                    job.id,
                    target_status=JobStatus.QUEUED,
                    current_status=JobStatus.RUNNING,
                    clear_cancel=True,
                    event_type="stale_job_recovered",
                    event_detail={"action": "resume", "reason": "startup_recovery"},
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
                    event_type="stale_job_recovered",
                    event_detail={
                        "action": "retry",
                        "reason": decision.reason,
                        "retry_at": decision.retry_at.isoformat() if decision.retry_at else None,
                    },
                )
            recovered.append(job.id)
        return recovered

    async def process_claimed_job(self, job: Job, worker_id: str) -> Job:
        backend = self._get_backend(job.backend)
        state = self.repository.get_conversation_state(job.id)
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
        )
        run_id: Optional[str] = None
        request_payload: Dict[str, Any] = {}
        try:
            run_id = self.repository.record_run_start(job, request_payload={"status": "starting"})
            if job.cancel_requested:
                raise UserCancelledError("Cancellation requested before execution started.")
            if backend.can_resume(job, state) and state.tool_context.get("pending_followup"):
                result = await backend.continue_job(job, state, context)
            elif backend.can_resume(job, state) and job.attempt_count > 1:
                result = await backend.continue_job(job, state, context)
            else:
                result = await backend.submit(job, state, context)
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
            return self.repository.get_job(job.id)
        except Exception as error:
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
            return self.repository.get_job(job.id)

    async def submit_message_batch(self, jobs: Sequence[Job], worker_id: str) -> List[Job]:
        if not jobs:
            return []
        backend = self._get_backend("message_batches")
        if not isinstance(backend, BatchCapableBackend):
            raise TypeError("message_batches backend does not implement batch submission.")
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
        )
        states = [(job, self.repository.get_conversation_state(job.id)) for job in jobs]
        submission = await backend.submit_batch(states, context)
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
            metadata = dict(job.metadata)
            metadata.update(
                {
                    "batch_record_id": batch_record_id,
                    "upstream_batch_id": submission.upstream_batch_id,
                    "batch_custom_id": custom_id,
                }
            )
            self.repository.set_job_metadata(job_id, metadata)
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
                    processed += 1
        return processed

    def _persist_success(
        self,
        job: Job,
        result: BackendResult,
        *,
        expected_status: JobStatus = JobStatus.RUNNING,
    ) -> None:
        if result.updated_state is not None:
            self.repository.save_conversation_state(result.updated_state)
        workspace = ensure_job_workspace(self.config.workspace_path(self.root), job.id)
        if result.output and self.config.privacy.store_response_artifacts:
            response_path = store_response_artifact(workspace, result.output)
            result.artifacts.append(
                ArtifactRecord(
                    id=None,
                    job_id=job.id,
                    created_at=utcnow(),
                    kind="response_text",
                    path=str(response_path),
                    metadata={"source": job.backend},
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
                event_type="job_cancelled",
                event_detail={"reason": "cancel_requested_while_running"},
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
                event_type="job_completed",
                event_detail={
                    "needs_followup": result.needs_followup,
                    "followup_reason": result.followup_reason,
                },
            )
        elif result.status == JobStatus.QUEUED:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.QUEUED,
                current_status=expected_status,
                clear_cancel=True,
                last_error_code=None,
                last_error_message=None,
                event_type="job_followup_scheduled",
                event_detail={"followup_reason": result.followup_reason},
            )
        elif result.status == JobStatus.WAITING_RETRY:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.WAITING_RETRY,
                current_status=expected_status,
                next_retry_at=utcnow() + timedelta(seconds=self.config.worker.poll_interval_seconds),
                clear_cancel=True,
                event_type="job_waiting_retry",
                event_detail={"followup_reason": result.followup_reason},
            )
        else:
            self.repository.update_job_status(
                job.id,
                target_status=result.status,
                current_status=expected_status,
                clear_cancel=True,
                event_type="job_state_changed",
                event_detail={"target_status": result.status.value},
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
            "job_execution_failed",
            extra={
                "context": {
                    "job_id": job.id,
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
                event_type="job_retry_scheduled",
                event_detail={
                    "reason": decision.reason,
                    "retry_at": decision.retry_at.isoformat() if decision.retry_at else None,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "headers": decision.headers,
                },
            )
        elif decision.disposition == RetryDisposition.CANCEL:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.CANCELLED,
                current_status=expected_status,
                last_error_code=decision.error_code,
                last_error_message=decision.error_message,
                clear_cancel=True,
                event_type="job_cancelled",
                event_detail={"reason": decision.reason},
            )
        else:
            self.repository.update_job_status(
                job.id,
                target_status=JobStatus.FAILED,
                current_status=expected_status,
                last_error_code=decision.error_code,
                last_error_message=decision.error_message,
                clear_cancel=True,
                event_type="job_failed",
                event_detail={"reason": decision.reason, "headers": decision.headers},
            )

    def _get_backend(self, backend_name: str) -> BackendAdapter:
        backend = self.backends.get(backend_name)
        if backend is None:
            raise KeyError(f"Unknown backend: {backend_name}")
        return backend  # type: ignore[return-value]

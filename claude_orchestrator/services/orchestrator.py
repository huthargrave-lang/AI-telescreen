"""High-level orchestration operations shared by CLI, workers, and web UI."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
from ..providers import infer_provider, validate_provider_backend
from ..repository import JobRepository
from ..retry import RetryPolicy, RetryableBackendError, UserCancelledError
from ..timeutils import utcnow
from ..workspaces import prepare_job_workspace, store_private_prompt, store_response_artifact
from ..backends.base import BackendAdapter, BatchCapableBackend, BackendContext

logger = logging.getLogger(__name__)


@dataclass
class JobInspection:
    job: Job
    state: ConversationState
    runs: List[Any]
    events: List[Any]
    artifacts: List[ArtifactRecord]


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
                renewed_until = self.repository.renew_job_lease(
                    self.job_id,
                    self.worker_id,
                    self.lease_seconds,
                )
                if renewed_until is None:
                    self.lost_lease = True
                    logger.warning(
                        "job.lease.heartbeat_lost",
                        extra={"context": {"job_id": self.job_id, "worker_id": self.worker_id}},
                    )
                    return
                logger.debug(
                    "job.lease.heartbeat_renewed",
                    extra={
                        "context": {
                            "job_id": self.job_id,
                            "worker_id": self.worker_id,
                            "lease_expires_at": renewed_until.isoformat(),
                        }
                    },
                )


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
        if privacy_mode:
            prompt_to_store = "[stored_in_private_artifact]"
        request.backend = backend_name
        request.provider = provider_name
        request.model = request.model or metadata.get("model") or self._default_model_for_backend(backend_name)
        request.metadata = metadata
        created = self.repository.create_job(
            request,
            prompt_to_store=prompt_to_store,
            workspace_path=None,
        )
        actual_workspace = prepare_job_workspace(self.config.workspace_path(self.root), created.id, metadata)
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
            {"workspace_path": str(actual_workspace), "provider": provider_name, "backend": backend_name},
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
        state = self.repository.get_conversation_state(job.id)
        context = BackendContext(
            config=self.config,
            workspace_root=self.config.workspace_path(self.root),
            worker_id=worker_id,
        )
        heartbeat = LeaseHeartbeat(
            self.repository,
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=self.config.effective_lease_seconds(),
            heartbeat_interval_seconds=self.config.worker.heartbeat_interval_seconds,
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
            return self.repository.get_job(job.id)
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
            return self.repository.get_job(job.id)
        finally:
            await heartbeat.stop()

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
        workspace = prepare_job_workspace(self.config.workspace_path(self.root), job.id, job.metadata)
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
        else:
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

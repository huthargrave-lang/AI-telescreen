from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from claude_orchestrator.config import AppConfig
from claude_orchestrator.db import Database
from claude_orchestrator.models import BackendResult, ConversationState, EnqueueJobRequest, JobStatus, RetryDecision, RetryDisposition
from claude_orchestrator.repository import JobRepository
from claude_orchestrator.services.orchestrator import OrchestratorService
from claude_orchestrator.timeutils import utcnow


class CompletedBackend:
    name = "messages_api"

    async def submit(self, job, state, context):
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history + [{"role": "assistant", "content": "done"}],
            compact_summary="done",
            tool_context=dict(state.tool_context),
            last_checkpoint_at=utcnow(),
        )
        return BackendResult(
            status=JobStatus.COMPLETED,
            output="done",
            updated_state=updated_state,
            request_payload={"job_id": job.id},
            response_summary={"summary": "done"},
            exit_reason="completed",
        )

    async def continue_job(self, job, state, context):
        return await self.submit(job, state, context)

    def classify_error(self, error, headers=None):
        return RetryDecision(
            disposition=RetryDisposition.FAIL,
            reason="fake_failure",
            error_code="fake_failure",
            error_message=str(error),
        )

    def can_resume(self, job, state):
        return False


def build_test_orchestrator(
    tmp_path: Path,
    *,
    backends: Optional[Dict[str, object]] = None,
) -> tuple[OrchestratorService, JobRepository, AppConfig]:
    config = AppConfig()
    config.storage.sqlite_path = str(tmp_path / "claude-orchestrator.db")
    config.logging.json_log_path = str(tmp_path / "logs" / "orchestrator.jsonl")
    config.workspace_root = str(tmp_path / "workspaces")
    config.privacy.store_response_artifacts = True
    db = Database(Path(config.storage.sqlite_path))
    db.initialize()
    repository = JobRepository(db)
    orchestrator = OrchestratorService(
        root=tmp_path,
        config=config,
        repository=repository,
        backends=backends or {"messages_api": CompletedBackend()},
    )
    return orchestrator, repository, config


def create_job(orchestrator: OrchestratorService, **kwargs):
    request = EnqueueJobRequest(
        backend=kwargs.get("backend", "messages_api"),
        task_type=kwargs.get("task_type", "test"),
        prompt=kwargs.get("prompt", "hello"),
        priority=kwargs.get("priority", 0),
        metadata=kwargs.get("metadata", {}),
        max_attempts=kwargs.get("max_attempts", 5),
        idempotency_key=kwargs.get("idempotency_key"),
        system_prompt=kwargs.get("system_prompt"),
        model=kwargs.get("model"),
        privacy_mode=kwargs.get("privacy_mode"),
    )
    return orchestrator.enqueue(request)

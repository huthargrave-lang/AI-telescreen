"""Optional Anthropic Agent SDK backend scaffold with checkpoint support."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AppConfig
from ..models import ArtifactRecord, BackendResult, ConversationState, Job, JobStatus
from ..retry import ConfigurationError, PermanentBackendError, RetryPolicy
from ..timeutils import utcnow
from ..workspaces import ensure_job_workspace, resolve_job_prompt
from .base import BackendAdapter, BackendContext


class AgentSdkBackend(BackendAdapter):
    """Checkpoint-friendly backend intended for bounded tool-use workflows."""

    name = "agent_sdk"

    def __init__(self, config: AppConfig, runner_factory: Optional[Any] = None) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)
        self._runner_factory = runner_factory

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        runner = self._build_runner()
        workspace = ensure_job_workspace(context.workspace_root, job.id)
        prompt = resolve_job_prompt(job)
        result = await runner.run(
            prompt=prompt,
            workspace=str(workspace),
            allowed_tools=job.metadata.get("allowed_tools", self.config.backends.agent_sdk.allowed_tools),
            turn_limit=job.metadata.get("turn_limit", self.config.backends.agent_sdk.turn_limit),
            timeout_seconds=job.metadata.get("timeout_seconds", self.config.backends.agent_sdk.timeout_seconds),
        )
        return self._normalize_result(job, state, workspace, result)

    async def continue_job(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        runner = self._build_runner()
        checkpoint = self._agent_state(state).get("checkpoint")
        if not checkpoint:
            return await self.submit(job, state, context)
        workspace = ensure_job_workspace(context.workspace_root, job.id)
        result = await runner.resume(
            checkpoint=checkpoint,
            workspace=str(workspace),
            allowed_tools=job.metadata.get("allowed_tools", self.config.backends.agent_sdk.allowed_tools),
            turn_limit=job.metadata.get("turn_limit", self.config.backends.agent_sdk.turn_limit),
            timeout_seconds=job.metadata.get("timeout_seconds", self.config.backends.agent_sdk.timeout_seconds),
        )
        return self._normalize_result(job, state, workspace, result)

    def classify_error(self, error: Exception, headers: Optional[Dict[str, str]] = None):
        return self.retry_policy.classify_failure(
            error,
            attempt_count=getattr(error, "_job_attempt_count", 1),
            max_attempts=getattr(error, "_job_max_attempts", self.config.retry.max_attempts),
            now=utcnow(),
            headers=headers,
        )

    def can_resume(self, job: Job, state: ConversationState) -> bool:
        return bool(self._agent_state(state).get("checkpoint"))

    def _build_runner(self) -> Any:
        if not self.config.backends.agent_sdk.enabled and self._runner_factory is None:
            raise ConfigurationError(
                "agent_sdk backend is disabled. Enable it in config and provide a documented Agent SDK runner."
            )
        if self._runner_factory is None:
            raise ConfigurationError(
                "No Agent SDK runner is configured. Inject a documented runner implementation before use."
            )
        return self._runner_factory()

    def _normalize_result(
        self,
        job: Job,
        state: ConversationState,
        workspace: Path,
        result: Any,
    ) -> BackendResult:
        payload = result if isinstance(result, dict) else getattr(result, "__dict__", {})
        if payload.get("error"):
            raise PermanentBackendError(str(payload["error"]))
        output = payload.get("output", "")
        checkpoint = payload.get("checkpoint")
        completed = bool(payload.get("completed", checkpoint is None))
        tool_context = dict(state.tool_context)
        agent_context = dict(self._agent_state(state))
        if checkpoint:
            agent_context["checkpoint"] = checkpoint
        else:
            agent_context.pop("checkpoint", None)
        agent_context["events"] = list(payload.get("events", []))
        agent_context["workspace"] = str(workspace)
        agent_context["allowed_tools"] = job.metadata.get("allowed_tools", self.config.backends.agent_sdk.allowed_tools)
        agent_context["last_run"] = {
            "completed": completed,
            "output_preview": output[:280],
            "event_count": len(payload.get("events", [])),
            "updated_at": utcnow().isoformat(),
        }
        tool_context["agent_sdk"] = agent_context
        # Preserve the legacy flat key for older persisted state while favoring the namespaced schema.
        if checkpoint:
            tool_context["checkpoint"] = checkpoint
        else:
            tool_context.pop("checkpoint", None)
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history
            + ([{"role": "assistant", "content": output}] if output else []),
            compact_summary=(output[:280] + "...") if len(output) > 280 else output,
            tool_context=tool_context,
            last_checkpoint_at=utcnow(),
        )
        artifacts: List[ArtifactRecord] = []
        for artifact_path in payload.get("artifacts", []):
            absolute_path = (
                str(Path(workspace) / artifact_path)
                if not str(artifact_path).startswith("/")
                else str(artifact_path)
            )
            artifacts.append(
                ArtifactRecord(
                    id=None,
                    job_id=job.id,
                    created_at=utcnow(),
                    kind="workspace_file",
                    path=absolute_path,
                    metadata={
                        "source": "agent_sdk",
                        "relative_path": str(artifact_path),
                        "workspace": str(workspace),
                    },
                )
            )
        return BackendResult(
            status=JobStatus.COMPLETED if completed else JobStatus.QUEUED,
            output=output,
            updated_state=updated_state,
            usage=payload.get("usage", {}),
            headers=payload.get("headers", {}),
            artifacts=artifacts,
            needs_followup=not completed,
            followup_reason="checkpoint_available" if checkpoint else None,
            request_payload={
                "workspace": str(workspace),
                "allowed_tools": job.metadata.get("allowed_tools", self.config.backends.agent_sdk.allowed_tools),
            },
            response_summary={
                "event_count": len(payload.get("events", [])),
                "completed": completed,
                "checkpoint_present": checkpoint is not None,
            },
            exit_reason="completed" if completed else "checkpoint",
        )

    def _agent_state(self, state: ConversationState) -> Dict[str, Any]:
        value = state.tool_context.get("agent_sdk", {})
        if isinstance(value, dict):
            return value
        return {}

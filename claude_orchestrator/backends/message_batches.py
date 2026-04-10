"""Optional Message Batches backend for bulk, non-urgent jobs."""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..config import AppConfig
from ..models import (
    BackendResult,
    BatchPollResult,
    BatchSubmissionResult,
    ConversationState,
    Job,
    JobStatus,
)
from ..retry import ConfigurationError, HttpFailure, RetryPolicy
from ..timeutils import utcnow
from ..workspaces import prompt_fingerprint, resolve_job_prompt
from .base import BatchCapableBackend, BackendContext
from .messages_api import MessagesApiBackend

try:  # pragma: no cover - optional import in the local sandbox.
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - exercised when dependency absent.
    AsyncAnthropic = None  # type: ignore[assignment]


class MessageBatchesBackend(BatchCapableBackend):
    """Anthropic Message Batches backend with job fan-out/fan-in support."""

    name = "message_batches"

    def __init__(self, config: AppConfig, client_factory: Optional[Any] = None) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)
        self._client_factory = client_factory
        self._messages_helper = MessagesApiBackend(config, client_factory=client_factory)

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        batch_result = await self.submit_batch(((job, state),), context)
        return BackendResult(
            status=JobStatus.WAITING_RETRY,
            output=None,
            updated_state=state,
            request_payload=batch_result.request_payload,
            response_summary={
                "batch_id": batch_result.batch_id,
                "upstream_batch_id": batch_result.upstream_batch_id,
                "custom_id_map": batch_result.custom_id_map,
            },
            exit_reason="batch_submitted",
        )

    async def continue_job(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        return await self.submit(job, state, context)

    async def submit_batch(
        self,
        jobs: Sequence[Tuple[Job, ConversationState]],
        context: BackendContext,
    ) -> BatchSubmissionResult:
        client = self._build_client()
        requests = []
        custom_id_map: Dict[str, str] = {}
        for index, (job, state) in enumerate(jobs):
            request_payload = self._messages_helper._build_request_payload(job, state, continuation=False)
            custom_id = f"{job.id}:{index}"
            custom_id_map[custom_id] = job.id
            requests.append({"custom_id": custom_id, "params": request_payload})
        messages_api = getattr(client, "messages", client)
        batches_api = getattr(messages_api, "batches", None)
        if batches_api is None or not hasattr(batches_api, "create"):
            raise ConfigurationError("Anthropic SDK does not expose messages.batches.create in this environment.")
        response = await batches_api.create(requests=requests)
        upstream_batch_id = getattr(response, "id", None) or response["id"]
        model = jobs[0][0].model or jobs[0][0].metadata.get("model") or self.config.backends.messages_api.model
        return BatchSubmissionResult(
            batch_id=prompt_fingerprint(upstream_batch_id),
            upstream_batch_id=upstream_batch_id,
            custom_id_map=custom_id_map,
            request_payload={"requests": requests, "model": model},
        )

    async def poll_batch(
        self,
        upstream_batch_id: str,
        custom_id_map: Dict[str, str],
        context: BackendContext,
    ) -> BatchPollResult:
        client = self._build_client()
        messages_api = getattr(client, "messages", client)
        batches_api = getattr(messages_api, "batches", None)
        if batches_api is None or not hasattr(batches_api, "retrieve"):
            raise ConfigurationError("Anthropic SDK does not expose messages.batches.retrieve in this environment.")
        batch = await batches_api.retrieve(upstream_batch_id)
        if isinstance(batch, Mapping):
            status = batch.get("processing_status") or batch.get("status") or "unknown"
            headers = dict(batch.get("headers", {}) or {})
            result_items = batch.get("results")
        else:
            status = getattr(batch, "processing_status", None) or getattr(batch, "status", None) or "unknown"
            headers = dict(getattr(batch, "headers", {}) or {})
            result_items = getattr(batch, "results", None)
        if result_items is None and hasattr(batches_api, "results"):
            result_items = await batches_api.results(upstream_batch_id)
        completed: Dict[str, Dict[str, Any]] = {}
        failed: Dict[str, Dict[str, Any]] = {}
        seen = set()
        for item in result_items or []:
            if isinstance(item, dict):
                custom_id = item.get("custom_id")
                result_payload = item.get("result", {})
            else:
                custom_id = getattr(item, "custom_id", None)
                result_payload = getattr(item, "result", {})
                if not isinstance(result_payload, dict) and hasattr(result_payload, "__dict__"):
                    result_payload = dict(result_payload.__dict__)
            if not custom_id:
                continue
            seen.add(custom_id)
            if self._item_failed(result_payload):
                failed[custom_id] = result_payload
            else:
                completed[custom_id] = self._normalize_success(result_payload)
        pending = [custom_id for custom_id in custom_id_map if custom_id not in seen]
        return BatchPollResult(
            upstream_batch_id=upstream_batch_id,
            status=status,
            completed_custom_ids=completed,
            failed_custom_ids=failed,
            still_pending_custom_ids=pending,
            headers=headers,
        )

    def parse_completed_result(
        self,
        job: Job,
        state: ConversationState,
        payload: Dict[str, Any],
    ) -> BackendResult:
        text_output = payload.get("output", "")
        stop_reason = payload.get("stop_reason")
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history + [{"role": "assistant", "content": text_output}],
            compact_summary=(text_output[:280] + "...") if len(text_output) > 280 else text_output,
            tool_context=dict(state.tool_context),
            last_checkpoint_at=utcnow(),
        )
        return BackendResult(
            status=JobStatus.COMPLETED,
            output=text_output,
            updated_state=updated_state,
            usage=payload.get("usage", {}),
            headers=payload.get("headers", {}),
            response_summary={
                "stop_reason": stop_reason,
                "content_preview": text_output[:500],
            },
            exit_reason=stop_reason or "batch_completed",
        )

    def classify_error(self, error: Exception, headers: Optional[Dict[str, str]] = None):
        failure = self._translate_error(error, headers=headers)
        return self.retry_policy.classify_failure(
            failure,
            attempt_count=getattr(error, "_job_attempt_count", 1),
            max_attempts=getattr(error, "_job_max_attempts", self.config.retry.max_attempts),
            now=utcnow(),
            headers=headers,
        )

    def can_resume(self, job: Job, state: ConversationState) -> bool:
        return bool(job.metadata.get("upstream_batch_id"))

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        api_key = os.getenv(self.config.backends.messages_api.api_key_env)
        if not api_key:
            raise ConfigurationError(
                f"Missing API key in environment variable {self.config.backends.messages_api.api_key_env}"
            )
        if AsyncAnthropic is None:
            raise ConfigurationError(
                "anthropic package is not installed; install project dependencies before using message_batches."
            )
        return AsyncAnthropic(
            api_key=api_key,
            max_retries=0,
            timeout=self.config.backends.messages_api.timeout_seconds,
        )

    def _translate_error(self, error: Exception, headers: Optional[Dict[str, str]] = None) -> Exception:
        status_code = getattr(error, "status_code", None)
        body = getattr(error, "body", None) or {"message": str(error)}
        if status_code:
            return HttpFailure(
                status_code=int(status_code),
                body=body if isinstance(body, dict) else {"message": str(error)},
                headers=headers or getattr(error, "headers", None) or {},
                message=str(error),
            )
        return error

    def _item_failed(self, payload: Mapping[str, Any]) -> bool:
        return bool(payload.get("error")) or payload.get("type") in {"error", "failed"}

    def _normalize_success(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        response = payload.get("message") or payload.get("response") or payload
        if not isinstance(response, Mapping) and hasattr(response, "__dict__"):
            response = dict(response.__dict__)
        output = ""
        for item in response.get("content", []):
            if not isinstance(item, Mapping) and hasattr(item, "__dict__"):
                item = dict(item.__dict__)
            if isinstance(item, Mapping) and item.get("type") == "text":
                output += str(item.get("text", ""))
        return {
            "output": output.strip(),
            "usage": response.get("usage", {}),
            "stop_reason": response.get("stop_reason"),
            "headers": payload.get("headers", {}),
        }

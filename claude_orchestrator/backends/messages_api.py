"""Messages API backend built on officially supported Anthropic SDK flows."""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Dict, List, Mapping, Optional

from ..config import AppConfig
from ..models import BackendResult, ConversationState, Job, JobStatus
from ..retry import ConfigurationError, HttpFailure, RetryPolicy
from ..timeutils import utcnow
from ..workspaces import resolve_job_prompt
from .base import BackendAdapter, BackendContext

try:  # pragma: no cover - optional import in the local sandbox.
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - exercised when dependency absent.
    AsyncAnthropic = None  # type: ignore[assignment]


class MessagesApiBackend(BackendAdapter):
    """Default stateless backend that persists conversation state locally."""

    name = "messages_api"

    def __init__(self, config: AppConfig, client_factory: Optional[Any] = None) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)
        self._client_factory = client_factory

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        return await self._execute(job, state, context, continuation=False)

    async def continue_job(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        return await self._execute(job, state, context, continuation=True)

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
        return bool(state.message_history or state.tool_context.get("pending_followup"))

    async def _execute(
        self,
        job: Job,
        state: ConversationState,
        context: BackendContext,
        *,
        continuation: bool,
    ) -> BackendResult:
        client = self._build_client()
        request_payload = self._build_request_payload(job, state, continuation=continuation)
        raw_response = await self._create_message(client, request_payload)
        parsed = raw_response.parse() if hasattr(raw_response, "parse") else raw_response
        headers = dict(getattr(raw_response, "headers", {}) or {})
        text_output = self._extract_text(parsed)
        stop_reason = getattr(parsed, "stop_reason", None) or request_payload.get("stop_reason")
        updated_state = self._update_state(job, state, request_payload["messages"], text_output, stop_reason)
        usage = self._extract_usage(parsed)
        response_summary = {
            "model": getattr(parsed, "model", request_payload["model"]),
            "stop_reason": stop_reason,
            "content_preview": text_output[:500],
        }
        result_status = JobStatus.COMPLETED
        needs_followup = False
        followup_reason = None
        if stop_reason == "max_tokens":
            result_status = JobStatus.QUEUED
            needs_followup = True
            followup_reason = "max_tokens"
            updated_state.tool_context["pending_followup"] = True
        else:
            updated_state.tool_context["pending_followup"] = False
        if stop_reason == "tool_use":
            raise ValueError(
                "Messages API backend received tool_use output; choose agent_sdk or another tool-capable backend."
            )
        return BackendResult(
            status=result_status,
            output=text_output,
            updated_state=updated_state,
            usage=usage,
            headers=headers,
            needs_followup=needs_followup,
            followup_reason=followup_reason,
            request_payload=request_payload,
            response_summary=response_summary,
            exit_reason=stop_reason,
        )

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
                "anthropic package is not installed; install project dependencies before using messages_api."
            )
        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": self.config.backends.messages_api.timeout_seconds,
        }
        if self.config.backends.messages_api.base_url:
            client_kwargs["base_url"] = self.config.backends.messages_api.base_url
        return AsyncAnthropic(**client_kwargs)

    async def _create_message(self, client: Any, request_payload: Dict[str, Any]) -> Any:
        messages_api = getattr(client, "messages", client)
        raw_api = getattr(messages_api, "with_raw_response", messages_api)
        create = getattr(raw_api, "create")
        return await create(**request_payload)

    def _build_request_payload(
        self,
        job: Job,
        state: ConversationState,
        *,
        continuation: bool,
    ) -> Dict[str, Any]:
        prompt = resolve_job_prompt(job)
        system_prompt = self._effective_system_prompt(state, job)
        messages = deepcopy(state.message_history)
        if not messages:
            messages.append({"role": "user", "content": prompt})
        elif continuation and messages[-1]["role"] != "user":
            messages.append(
                {
                    "role": "user",
                    "content": "Continue from the previous checkpoint and finish the task without repeating unnecessary context.",
                }
            )
        _, messages = self._compact_messages(state.compact_summary, messages)
        max_tokens = int(job.metadata.get("max_tokens", self.config.backends.messages_api.max_tokens))
        request_payload: Dict[str, Any] = {
            "model": job.model or job.metadata.get("model") or self.config.backends.messages_api.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            request_payload["system"] = system_prompt
        return request_payload

    def _extract_text(self, parsed: Any) -> str:
        chunks: List[str] = []
        for item in getattr(parsed, "content", []) or []:
            item_type = getattr(item, "type", None)
            if item_type is None and isinstance(item, Mapping):
                item_type = item.get("type")
            if item_type == "text":
                if hasattr(item, "text"):
                    chunks.append(getattr(item, "text"))
                elif isinstance(item, Mapping):
                    chunks.append(str(item.get("text", "")))
        return "".join(chunks).strip()

    def _extract_usage(self, parsed: Any) -> Dict[str, Any]:
        usage = getattr(parsed, "usage", None)
        if usage is None:
            return {}
        if isinstance(usage, Mapping):
            return dict(usage)
        values = {}
        for attribute in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            if hasattr(usage, attribute):
                values[attribute] = getattr(usage, attribute)
        return values

    def _update_state(
        self,
        job: Job,
        state: ConversationState,
        request_messages: List[Dict[str, Any]],
        text_output: str,
        stop_reason: Optional[str],
    ) -> ConversationState:
        updated_history = deepcopy(request_messages) + ([{"role": "assistant", "content": text_output}] if text_output else [])
        compact_summary, compacted_history = self._compact_messages(state.compact_summary, updated_history)
        updated = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=compacted_history,
            compact_summary=compact_summary,
            tool_context=deepcopy(state.tool_context),
            last_checkpoint_at=utcnow(),
        )
        updated.tool_context["last_stop_reason"] = stop_reason
        updated.tool_context["pending_followup"] = stop_reason == "max_tokens"
        updated.tool_context["compaction_applied"] = compacted_history != updated_history
        return updated

    def _effective_system_prompt(self, state: ConversationState, job: Job) -> Optional[str]:
        base_system_prompt = state.system_prompt or job.metadata.get("system_prompt")
        compact_summary = state.compact_summary
        if not compact_summary:
            return base_system_prompt
        summary_block = (
            "Conversation summary from earlier turns. Treat it as durable context for continuation.\n"
            f"{compact_summary}"
        )
        if base_system_prompt:
            return f"{base_system_prompt}\n\n{summary_block}"
        return summary_block

    def _compact_messages(
        self,
        existing_summary: Optional[str],
        messages: List[Dict[str, Any]],
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        threshold = self.config.backends.messages_api.compaction_message_threshold
        char_threshold = self.config.backends.messages_api.compaction_char_threshold
        if len(messages) <= threshold and self._message_char_count(messages) <= char_threshold:
            return existing_summary, messages
        keep_recent = min(
            max(self.config.backends.messages_api.compaction_keep_recent_messages, 1),
            max(len(messages), 1),
        )
        older_messages = messages[:-keep_recent]
        recent_messages = messages[-keep_recent:]
        summary = self._summarize_messages(existing_summary, older_messages)
        return summary, recent_messages

    def _summarize_messages(
        self,
        existing_summary: Optional[str],
        messages: List[Dict[str, Any]],
    ) -> Optional[str]:
        if not messages and existing_summary:
            return existing_summary
        lines: List[str] = []
        if existing_summary:
            lines.append(existing_summary.strip())
        for message in messages:
            role = str(message.get("role", "unknown")).upper()
            content = self._stringify_content(message.get("content", "")).replace("\n", " ").strip()
            if content:
                lines.append(f"{role}: {content[:400]}")
        if not lines:
            return existing_summary
        combined = "\n".join(lines)
        limit = self.config.backends.messages_api.compaction_summary_char_limit
        if len(combined) <= limit:
            return combined
        return f"...\n{combined[-limit:]}"

    def _message_char_count(self, messages: List[Dict[str, Any]]) -> int:
        return sum(len(self._stringify_content(message.get("content", ""))) for message in messages)

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, Mapping):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content)

    def _translate_error(self, error: Exception, headers: Optional[Dict[str, str]] = None) -> Exception:
        status_code = getattr(error, "status_code", None)
        body = getattr(error, "body", None) or getattr(error, "response", None)
        response_headers = headers or getattr(error, "headers", None) or {}
        if status_code:
            if hasattr(body, "json"):
                try:
                    body = body.json()
                except Exception:  # pragma: no cover - defensive branch.
                    body = {"message": str(error)}
            if not isinstance(body, dict):
                body = {"message": str(error)}
            return HttpFailure(
                status_code=int(status_code),
                body=body,
                headers=response_headers,
                message=str(error),
            )
        return error

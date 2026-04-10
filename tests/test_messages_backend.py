from __future__ import annotations

import asyncio
from dataclasses import dataclass

from claude_orchestrator.backends.messages_api import MessagesApiBackend
from claude_orchestrator.config import AppConfig
from claude_orchestrator.models import ConversationState, Job, JobStatus, RetryDisposition
from claude_orchestrator.timeutils import utcnow
from claude_orchestrator.workspaces import ensure_job_workspace


@dataclass
class FakeContent:
    type: str
    text: str


@dataclass
class FakeParsedResponse:
    content: list
    stop_reason: str
    model: str
    usage: dict


class FakeRawResponse:
    def __init__(self, parsed, headers=None):
        self._parsed = parsed
        self.headers = headers or {}

    def parse(self):
        return self._parsed


class FakeMessagesApi:
    def __init__(self, raw_response):
        self.with_raw_response = self
        self._raw_response = raw_response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._raw_response


class FakeClient:
    def __init__(self, raw_response):
        self.messages = FakeMessagesApi(raw_response)


class FakeApiError(Exception):
    def __init__(self, status_code, body, headers):
        super().__init__(body.get("error", {}).get("message", "error"))
        self.status_code = status_code
        self.body = body
        self.headers = headers


def _job() -> Job:
    now = utcnow()
    return Job(
        id="job-1",
        created_at=now,
        updated_at=now,
        status=JobStatus.QUEUED,
        backend="messages_api",
        task_type="test",
        priority=0,
        prompt="hello",
        metadata={},
        attempt_count=0,
        next_retry_at=None,
        last_error_code=None,
        last_error_message=None,
        max_attempts=5,
        idempotency_key=None,
    )


def test_messages_backend_requests_followup_after_max_tokens(tmp_path):
    parsed = FakeParsedResponse(
        content=[FakeContent(type="text", text="partial output")],
        stop_reason="max_tokens",
        model="test-model",
        usage={"input_tokens": 10, "output_tokens": 20},
    )
    fake_client = FakeClient(FakeRawResponse(parsed, headers={"x-request-id": "abc"}))
    config = AppConfig()
    backend = MessagesApiBackend(config, client_factory=lambda: fake_client)
    state = ConversationState(job_id="job-1", system_prompt=None)
    context = type(
        "Context",
        (),
        {"config": config, "workspace_root": ensure_job_workspace(tmp_path, "job-1").parent, "worker_id": "worker-1"},
    )()

    result = asyncio.run(backend.submit(_job(), state, context))

    assert result.status == JobStatus.QUEUED
    assert result.needs_followup is True
    assert result.updated_state.tool_context["pending_followup"] is True
    assert fake_client.messages.calls[0]["messages"][0]["content"] == "hello"


def test_messages_backend_compacts_history_into_system_summary(tmp_path):
    parsed = FakeParsedResponse(
        content=[FakeContent(type="text", text="done")],
        stop_reason="end_turn",
        model="test-model",
        usage={"input_tokens": 10, "output_tokens": 20},
    )
    fake_client = FakeClient(FakeRawResponse(parsed))
    config = AppConfig()
    config.backends.messages_api.compaction_message_threshold = 4
    config.backends.messages_api.compaction_keep_recent_messages = 2
    backend = MessagesApiBackend(config, client_factory=lambda: fake_client)
    state = ConversationState(
        job_id="job-1",
        system_prompt="Be helpful.",
        compact_summary="Older summary",
        message_history=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
            {"role": "user", "content": "five"},
            {"role": "assistant", "content": "six"},
        ],
    )
    context = type(
        "Context",
        (),
        {"config": config, "workspace_root": ensure_job_workspace(tmp_path, "job-1").parent, "worker_id": "worker-1"},
    )()

    result = asyncio.run(backend.continue_job(_job(), state, context))

    request = fake_client.messages.calls[0]
    assert "Conversation summary from earlier turns" in request["system"]
    assert len(request["messages"]) == 2
    assert request["messages"][-1]["content"].startswith("Continue from the previous checkpoint")
    assert len(result.updated_state.message_history) <= 3
    assert "Older summary" in (result.updated_state.compact_summary or "")


def test_messages_backend_classifies_429_retry_after():
    backend = MessagesApiBackend(AppConfig(), client_factory=lambda: object())
    error = FakeApiError(
        429,
        {"error": {"type": "rate_limit_error", "message": "slow down"}},
        {"retry-after": "17"},
    )

    decision = backend.classify_error(error)

    assert decision.disposition == RetryDisposition.RETRY
    assert decision.retry_after_seconds == 17


def test_messages_backend_classifies_invalid_key_as_permanent():
    backend = MessagesApiBackend(AppConfig(), client_factory=lambda: object())
    error = FakeApiError(
        401,
        {"error": {"type": "authentication_error", "message": "bad key"}},
        {},
    )

    decision = backend.classify_error(error)

    assert decision.disposition == RetryDisposition.FAIL

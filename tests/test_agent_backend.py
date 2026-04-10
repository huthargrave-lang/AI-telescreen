from __future__ import annotations

import asyncio

from claude_orchestrator.backends.agent_sdk import AgentSdkBackend
from claude_orchestrator.config import AppConfig
from claude_orchestrator.models import ConversationState, Job, JobStatus
from claude_orchestrator.timeutils import utcnow


class FakeRunner:
    async def run(self, **kwargs):
        return {
            "output": "checkpointed",
            "events": ["read"],
            "checkpoint": "cp-1",
            "completed": False,
        }

    async def resume(self, **kwargs):
        return {
            "output": "done",
            "events": ["write"],
            "artifacts": ["result.txt"],
            "completed": True,
        }


def _job() -> Job:
    now = utcnow()
    return Job(
        id="agent-job",
        created_at=now,
        updated_at=now,
        status=JobStatus.QUEUED,
        backend="agent_sdk",
        task_type="code",
        priority=0,
        prompt="analyze repo",
        metadata={},
        attempt_count=0,
        next_retry_at=None,
        last_error_code=None,
        last_error_message=None,
        max_attempts=5,
        idempotency_key=None,
    )


def test_agent_backend_checkpoint_resume(tmp_path):
    config = AppConfig()
    config.backends.agent_sdk.enabled = True
    backend = AgentSdkBackend(config, runner_factory=lambda: FakeRunner())
    state = ConversationState(job_id="agent-job", system_prompt=None)
    context = type(
        "Context",
        (),
        {"config": config, "workspace_root": tmp_path / "workspaces", "worker_id": "worker-1"},
    )()

    first = asyncio.run(backend.submit(_job(), state, context))

    assert first.status == JobStatus.QUEUED
    assert backend.can_resume(_job(), first.updated_state) is True
    assert first.updated_state.tool_context["agent_sdk"]["checkpoint"] == "cp-1"

    second = asyncio.run(backend.continue_job(_job(), first.updated_state, context))

    assert second.status == JobStatus.COMPLETED
    assert len(second.artifacts) == 1
    assert second.updated_state.tool_context["agent_sdk"]["last_run"]["completed"] is True

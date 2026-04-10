from __future__ import annotations

from claude_orchestrator.backends.agent_sdk import AgentSdkBackend
from claude_orchestrator.backends.base import BackendAdapter
from claude_orchestrator.backends.claude_code_cli import ClaudeCodeCliBackend
from claude_orchestrator.backends.codex_cli import CodexCliBackend
from claude_orchestrator.backends.message_batches import MessageBatchesBackend
from claude_orchestrator.backends.messages_api import MessagesApiBackend
from claude_orchestrator.config import AppConfig


class DummyRunner:
    async def run(self, **kwargs):  # pragma: no cover - contract only.
        return {"output": "ok", "completed": True}

    async def resume(self, **kwargs):  # pragma: no cover - contract only.
        return {"output": "ok", "completed": True}


def test_backends_implement_shared_contract():
    config = AppConfig()
    config.backends.agent_sdk.enabled = True
    config.backends.codex_cli.enabled = True
    backends = [
        MessagesApiBackend(config, client_factory=lambda: object()),
        MessageBatchesBackend(config, client_factory=lambda: object()),
        AgentSdkBackend(config, runner_factory=lambda: DummyRunner()),
        ClaudeCodeCliBackend(config),
        CodexCliBackend(config),
    ]

    for backend in backends:
        assert isinstance(backend, BackendAdapter)

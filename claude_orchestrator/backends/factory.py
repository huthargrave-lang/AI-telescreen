"""Backend registry construction."""

from __future__ import annotations

from typing import Dict

from ..config import AppConfig
from .agent_sdk import AgentSdkBackend
from .claude_code_cli import ClaudeCodeCliBackend
from .message_batches import MessageBatchesBackend
from .messages_api import MessagesApiBackend


def build_backend_registry(config: AppConfig) -> Dict[str, object]:
    """Build all configured backends."""

    return {
        "messages_api": MessagesApiBackend(config),
        "message_batches": MessageBatchesBackend(config),
        "agent_sdk": AgentSdkBackend(config),
        "claude_code_cli": ClaudeCodeCliBackend(config),
    }

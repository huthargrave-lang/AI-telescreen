"""Backend registry construction."""

from __future__ import annotations

import logging
import shutil
from typing import Dict

from ..config import AppConfig
from .agent_sdk import AgentSdkBackend
from .claude_code_cli import ClaudeCodeCliBackend
from .codex_cli import CodexCliBackend
from .message_batches import MessageBatchesBackend
from .messages_api import MessagesApiBackend

logger = logging.getLogger(__name__)


def build_backend_registry(config: AppConfig) -> Dict[str, object]:
    """Build all configured backends."""

    validate_backend_config(config)
    registry: Dict[str, object] = {"messages_api": MessagesApiBackend(config)}
    if config.backends.message_batches.enabled:
        registry["message_batches"] = MessageBatchesBackend(config)
    if config.backends.agent_sdk.enabled:
        registry["agent_sdk"] = AgentSdkBackend(config)
    if config.backends.claude_code_cli.enabled:
        registry["claude_code_cli"] = ClaudeCodeCliBackend(config)
    if config.backends.codex_cli.enabled:
        registry["codex_cli"] = CodexCliBackend(config)
    return registry


def validate_backend_config(config: AppConfig) -> None:
    """Raise with clear operator-facing messages for invalid runtime configuration."""

    errors = []
    enabled_backends = {"messages_api"}
    if config.backends.message_batches.enabled:
        enabled_backends.add("message_batches")
    if config.backends.agent_sdk.enabled:
        enabled_backends.add("agent_sdk")
    if config.backends.claude_code_cli.enabled:
        enabled_backends.add("claude_code_cli")
    if config.backends.codex_cli.enabled:
        enabled_backends.add("codex_cli")

    if config.default_backend not in enabled_backends:
        errors.append(
            f"default_backend={config.default_backend!r} is not enabled. Enabled backends: {sorted(enabled_backends)}"
        )

    effective_lease_seconds = config.effective_lease_seconds()
    if effective_lease_seconds <= 0:
        errors.append("worker lease must be greater than zero seconds.")
    if config.worker.heartbeat_interval_seconds <= 0:
        errors.append("worker heartbeat interval must be greater than zero seconds.")
    if config.worker.heartbeat_interval_seconds >= effective_lease_seconds:
        errors.append("worker heartbeat interval must be shorter than the effective worker lease.")

    if config.backends.messages_api.compaction_keep_recent_messages <= 0:
        errors.append("messages_api compaction_keep_recent_messages must be at least 1.")
    if (
        config.backends.messages_api.compaction_keep_recent_messages
        > config.backends.messages_api.compaction_message_threshold
    ):
        errors.append("messages_api keep_recent_messages cannot exceed compaction_message_threshold.")

    if config.backends.claude_code_cli.enabled:
        executable = config.backends.claude_code_cli.executable
        if not config.backends.claude_code_cli.command_template:
            errors.append("claude_code_cli is enabled but command_template is empty.")
        if shutil.which(executable) is None:
            logger.warning(
                "claude_code_cli executable %r is not on PATH. "
                "Install it with: npm install -g @anthropic-ai/claude-code",
                executable,
            )
        if config.backends.claude_code_cli.hook_commands and not config.backends.claude_code_cli.allow_hooks:
            errors.append("claude_code_cli hook_commands are configured but allow_hooks is false.")
        if (
            config.backends.claude_code_cli.allow_hooks
            and config.backends.claude_code_cli.hook_commands
            and not config.backends.claude_code_cli.allowed_hook_executables
        ):
            errors.append("claude_code_cli hooks require allowed_hook_executables when allow_hooks is true.")

    if config.backends.codex_cli.enabled:
        executable = config.backends.codex_cli.executable
        if shutil.which(executable) is None:
            errors.append(f"codex_cli executable {executable!r} is not on PATH.")
        normalized_args = [item for item in (str(entry).strip() for entry in config.backends.codex_cli.args) if item]
        if any(item in CodexCliBackend.MANAGED_ARGUMENTS for item in normalized_args):
            errors.append(
                "codex_cli args must not include workspace or prompt file flags; workspace is supplied via subprocess cwd."
            )
        normalized_template = [
            item for item in (str(entry).strip() for entry in config.backends.codex_cli.command_template) if item
        ]
        if config.backends.codex_cli.use_legacy_command_template and not normalized_template:
            errors.append(
                "codex_cli use_legacy_command_template is true, but command_template is empty."
            )
        if config.backends.codex_cli.timeout_seconds <= 0:
            errors.append("codex_cli timeout_seconds must be greater than zero.")
        if config.backends.codex_cli.smoke_test_timeout_seconds <= 0:
            errors.append("codex_cli smoke_test_timeout_seconds must be greater than zero.")
        if config.backends.codex_cli.max_output_bytes <= 0:
            errors.append("codex_cli max_output_bytes must be greater than zero.")

    if errors:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(errors))

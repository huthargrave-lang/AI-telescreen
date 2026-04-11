"""Optional documented Claude Code CLI backend wrapper."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import AppConfig
from ..models import ArtifactRecord, BackendResult, ConversationState, Job, JobStatus
from ..retry import ConfigurationError, PermanentBackendError, RetryPolicy, RetryableBackendError
from ..timeutils import utcnow
from ..workspaces import ensure_job_workspace, resolve_job_prompt, store_response_artifact
from .base import BackendAdapter, BackendContext


class ClaudeCodeCliBackend(BackendAdapter):
    """Structured subprocess wrapper for explicitly configured CLI flows."""

    name = "claude_code_cli"
    SHELL_EXECUTABLES = {"bash", "sh", "zsh", "fish", "pwsh", "powershell"}
    supports_workspace_integrations = True
    supports_project_mcp = True
    supports_user_mcp = True

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        self._emit_integration_context(context)
        workspace = ensure_job_workspace(context.workspace_root, job.id)
        prompt = resolve_job_prompt(job)
        prompt_file = workspace / "cli_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        await self._run_hooks("before", workspace, prompt_file)
        command = self._build_command(workspace, prompt_file)
        returncode, stdout_text, stderr_text, stdout_truncated, stderr_truncated = await self._run_subprocess(
            command,
            cwd=workspace,
            timeout_seconds=self.config.backends.claude_code_cli.timeout_seconds,
        )
        await self._run_hooks("after", workspace, prompt_file)
        if returncode != 0:
            lowered = stderr_text.lower()
            if "auth" in lowered or "login" in lowered or "token" in lowered:
                raise PermanentBackendError(stderr_text or "Claude Code CLI authentication failed.")
            if "timeout" in lowered or "rate limit" in lowered or "temporarily unavailable" in lowered:
                raise RetryableBackendError(stderr_text or "Transient Claude Code CLI failure.")
            if "network" in lowered or "connection" in lowered or "unavailable" in lowered:
                raise RetryableBackendError(stderr_text or "Claude Code CLI transport failure.")
            raise PermanentBackendError(stderr_text or "Claude Code CLI exited with a non-zero code.")
        response_artifact = store_response_artifact(workspace, stdout_text, name="claude_code_stdout.txt")
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history + [{"role": "assistant", "content": stdout_text}],
            compact_summary=(stdout_text[:280] + "...") if len(stdout_text) > 280 else stdout_text,
            tool_context=dict(state.tool_context),
            last_checkpoint_at=utcnow(),
        )
        return BackendResult(
            status=JobStatus.COMPLETED,
            output=stdout_text,
            updated_state=updated_state,
            headers={},
            artifacts=[
                ArtifactRecord(
                    id=None,
                    job_id=job.id,
                    created_at=utcnow(),
                    kind="cli_output",
                    path=str(response_artifact),
                    metadata={
                        "stderr_preview": stderr_text[: self.config.backends.claude_code_cli.preview_characters],
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                        "returncode": returncode,
                    },
                )
            ],
            request_payload={"command": command, "prompt_file": str(prompt_file)},
            response_summary={
                "stdout_preview": stdout_text[: self.config.backends.claude_code_cli.preview_characters],
                "stderr_preview": stderr_text[: self.config.backends.claude_code_cli.preview_characters],
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
            exit_reason="completed",
        )

    async def continue_job(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        return await self.submit(job, state, context)

    def classify_error(self, error: Exception, headers: Optional[Dict[str, str]] = None):
        return self.retry_policy.classify_failure(
            error,
            attempt_count=getattr(error, "_job_attempt_count", 1),
            max_attempts=getattr(error, "_job_max_attempts", self.config.retry.max_attempts),
            now=utcnow(),
            headers=headers,
        )

    def can_resume(self, job: Job, state: ConversationState) -> bool:
        return False

    def _build_command(self, workspace: Path, prompt_file: Path) -> List[str]:
        backend_config = self.config.backends.claude_code_cli
        if not backend_config.enabled:
            raise ConfigurationError("claude_code_cli backend is disabled in config.")
        if not backend_config.command_template:
            raise ConfigurationError(
                "claude_code_cli.command_template is empty. Provide a documented CLI invocation."
            )
        executable = backend_config.executable
        resolved = shutil.which(executable)
        if resolved is None:
            raise ConfigurationError(f"CLI executable not found on PATH: {executable}")
        first_token = backend_config.command_template[0]
        allowed_first_tokens = {backend_config.executable, "{executable}", resolved, Path(resolved).name}
        if first_token not in allowed_first_tokens:
            raise ConfigurationError(
                "claude_code_cli.command_template must begin with the configured CLI executable or {executable}."
            )
        if Path(resolved).name in self.SHELL_EXECUTABLES:
            raise ConfigurationError("Shell wrappers are not allowed for claude_code_cli command execution.")
        command = []
        for item in backend_config.command_template:
            command.append(
                item.format(
                    executable=resolved,
                    workspace=str(workspace),
                    prompt_file=str(prompt_file),
                )
            )
        command[0] = resolved
        return command

    async def _run_hooks(self, phase: str, workspace: Path, prompt_file: Path) -> None:
        backend_config = self.config.backends.claude_code_cli
        commands = backend_config.hook_commands
        if not commands:
            return
        if not backend_config.allow_hooks:
            raise ConfigurationError("claude_code_cli hooks are configured but allow_hooks is false.")
        for command_text in commands:
            command = self._build_hook_command(command_text, workspace, prompt_file, phase)
            returncode, stdout_text, stderr_text, _, _ = await self._run_subprocess(
                command,
                cwd=workspace,
                timeout_seconds=min(backend_config.hook_timeout_seconds, backend_config.timeout_seconds),
            )
            if returncode != 0:
                raise ConfigurationError(
                    f"claude_code_cli hook failed during {phase}: {stderr_text or stdout_text or 'non-zero exit'}"
                )

    def _build_hook_command(self, command_text: str, workspace: Path, prompt_file: Path, phase: str) -> List[str]:
        tokens = [
            part.format(workspace=str(workspace), prompt_file=str(prompt_file), phase=phase)
            for part in shlex.split(command_text)
        ]
        if not tokens:
            raise ConfigurationError("claude_code_cli hook command is empty.")
        executable = tokens[0]
        resolved = shutil.which(executable)
        if resolved is None:
            raise ConfigurationError(f"Hook executable not found on PATH: {executable}")
        executable_name = Path(resolved).name
        if executable_name in self.SHELL_EXECUTABLES:
            raise ConfigurationError("Shell wrappers are not allowed for claude_code_cli hooks.")
        if executable_name not in set(self.config.backends.claude_code_cli.allowed_hook_executables):
            raise ConfigurationError(
                f"Hook executable {executable_name!r} is not in allowed_hook_executables."
            )
        tokens[0] = resolved
        return tokens

    async def _run_subprocess(
        self,
        command: List[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> Tuple[int, str, str, bool, bool]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(str(exc)) from exc
        stdout_task = asyncio.create_task(
            self._read_stream_limited(process.stdout, self.config.backends.claude_code_cli.max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            self._read_stream_limited(process.stderr, self.config.backends.claude_code_cli.max_output_bytes)
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise RetryableBackendError("Claude Code CLI timed out.") from exc
        stdout_text, stdout_truncated = await stdout_task
        stderr_text, stderr_truncated = await stderr_task
        return returncode, stdout_text.strip(), stderr_text.strip(), stdout_truncated, stderr_truncated

    async def _read_stream_limited(
        self,
        stream: Optional[asyncio.StreamReader],
        byte_limit: int,
    ) -> Tuple[str, bool]:
        if stream is None:
            return "", False
        chunks: List[bytes] = []
        kept = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remaining = max(byte_limit - kept, 0)
            if remaining > 0:
                chunks.append(chunk[:remaining])
                kept += len(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return b"".join(chunks).decode("utf-8", errors="replace"), truncated

    def _emit_integration_context(self, context: BackendContext) -> None:
        emit_stream_event = getattr(context, "emit_stream_event", None)
        integration_summary = getattr(context, "integration_summary", None)
        if emit_stream_event is None or integration_summary is None:
            return
        capability_names = [capability.name for capability in integration_summary.external_capabilities()]
        if not capability_names:
            return
        emit_stream_event(
            "integration_context_loaded",
            "setup",
            "Loaded workspace integration context for Claude Code CLI.",
            {
                "capability_names": capability_names,
                "config_paths": integration_summary.config_paths,
                "status": integration_summary.status(),
            },
        )

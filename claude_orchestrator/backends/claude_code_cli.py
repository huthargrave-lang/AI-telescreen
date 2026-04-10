"""Optional documented Claude Code CLI backend wrapper."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AppConfig
from ..models import ArtifactRecord, BackendResult, ConversationState, Job, JobStatus
from ..retry import ConfigurationError, PermanentBackendError, RetryPolicy, RetryableBackendError
from ..timeutils import utcnow
from ..workspaces import ensure_job_workspace, resolve_job_prompt, store_response_artifact
from .base import BackendAdapter, BackendContext


class ClaudeCodeCliBackend(BackendAdapter):
    """Structured subprocess wrapper for explicitly configured CLI flows."""

    name = "claude_code_cli"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        workspace = ensure_job_workspace(context.workspace_root, job.id)
        prompt = resolve_job_prompt(job)
        prompt_file = workspace / "cli_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        await self._run_hooks("before", workspace, prompt_file)
        command = self._build_command(workspace, prompt_file)
        completed = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            completed.communicate(),
            timeout=self.config.backends.claude_code_cli.timeout_seconds,
        )
        await self._run_hooks("after", workspace, prompt_file)
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            lowered = stderr_text.lower()
            if "auth" in lowered or "login" in lowered or "token" in lowered:
                raise PermanentBackendError(stderr_text or "Claude Code CLI authentication failed.")
            if "timeout" in lowered or "rate limit" in lowered or "temporarily unavailable" in lowered:
                raise RetryableBackendError(stderr_text or "Transient Claude Code CLI failure.")
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
                    metadata={"stderr": stderr_text[:500]},
                )
            ],
            request_payload={"command": command, "prompt_file": str(prompt_file)},
            response_summary={"stdout_preview": stdout_text[:500], "stderr_preview": stderr_text[:500]},
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
        executable = backend_config.command_template[0]
        resolved = shutil.which(executable)
        if resolved is None:
            raise ConfigurationError(f"CLI executable not found on PATH: {executable}")
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
        commands = self.config.backends.claude_code_cli.hook_commands
        if not commands:
            return
        for command_text in commands:
            command = [
                part.format(workspace=str(workspace), prompt_file=str(prompt_file), phase=phase)
                for part in shlex.split(command_text)
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(
                process.communicate(),
                timeout=min(30, self.config.backends.claude_code_cli.timeout_seconds),
            )

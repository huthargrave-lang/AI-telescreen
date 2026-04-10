"""Optional OpenAI Codex CLI backend wrapper."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import AppConfig
from ..models import ArtifactRecord, BackendResult, ConversationState, Job, JobStatus
from ..retry import ConfigurationError, PermanentBackendError, RetryPolicy, RetryableBackendError
from ..timeutils import utcnow
from ..workspaces import prepare_job_workspace, resolve_job_prompt, store_response_artifact
from .base import BackendAdapter, BackendContext


class CodexCliBackend(BackendAdapter):
    """Structured subprocess wrapper for explicitly configured Codex CLI runs."""

    name = "codex_cli"
    SHELL_EXECUTABLES = {"bash", "sh", "zsh", "fish", "pwsh", "powershell"}

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.retry_policy = RetryPolicy(config.retry)

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        workspace = prepare_job_workspace(context.workspace_root, job.id, job.metadata)
        prompt = resolve_job_prompt(job)
        prompt_file = workspace / "codex_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = self._build_command(workspace=workspace, prompt_file=prompt_file)
        returncode, stdout_text, stderr_text, stdout_truncated, stderr_truncated = await self._run_subprocess(
            command,
            cwd=workspace,
            timeout_seconds=self.config.backends.codex_cli.timeout_seconds,
        )

        if returncode != 0:
            self._raise_for_failure(stderr_text=stderr_text, stdout_text=stdout_text)

        output_artifact = store_response_artifact(workspace, stdout_text, name="codex_stdout.txt")
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history + [{"role": "assistant", "content": stdout_text}],
            compact_summary=(stdout_text[:280] + "...") if len(stdout_text) > 280 else stdout_text,
            tool_context=dict(state.tool_context),
            last_checkpoint_at=utcnow(),
        )

        preview_limit = self.config.backends.codex_cli.preview_characters
        return BackendResult(
            status=JobStatus.COMPLETED,
            output=stdout_text,
            updated_state=updated_state,
            usage={},
            headers={},
            artifacts=[
                ArtifactRecord(
                    id=None,
                    job_id=job.id,
                    created_at=utcnow(),
                    kind="codex_cli_output",
                    path=str(output_artifact),
                    metadata={
                        "stderr_preview": stderr_text[:preview_limit],
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                        "returncode": returncode,
                    },
                )
            ],
            request_payload={
                "command": command,
                "prompt_file": str(prompt_file),
                "workspace": str(workspace),
                "provider": job.provider,
                "backend": job.backend,
            },
            response_summary={
                "stdout_preview": stdout_text[:preview_limit],
                "stderr_preview": stderr_text[:preview_limit],
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

    def _build_command(self, *, workspace: Path, prompt_file: Path) -> List[str]:
        backend_config = self.config.backends.codex_cli
        if not backend_config.enabled:
            raise ConfigurationError("codex_cli backend is disabled in config.")
        if not backend_config.command_template:
            raise ConfigurationError("codex_cli.command_template is empty.")

        executable = backend_config.executable
        resolved = shutil.which(executable)
        if resolved is None:
            raise ConfigurationError(f"Codex executable not found on PATH: {executable}")

        first_token = backend_config.command_template[0]
        allowed_first_tokens = {backend_config.executable, "{executable}", resolved, Path(resolved).name}
        if first_token not in allowed_first_tokens:
            raise ConfigurationError(
                "codex_cli.command_template must begin with the configured executable or {executable}."
            )
        if Path(resolved).name in self.SHELL_EXECUTABLES:
            raise ConfigurationError("Shell wrappers are not allowed for codex_cli command execution.")

        command: List[str] = []
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

    def _raise_for_failure(self, *, stderr_text: str, stdout_text: str) -> None:
        lowered = f"{stderr_text}\n{stdout_text}".lower()
        message = stderr_text or stdout_text or "Codex CLI exited with a non-zero code."
        if any(token in lowered for token in ("auth", "login", "api key", "unauthorized", "forbidden")):
            raise PermanentBackendError(message)
        if any(token in lowered for token in ("rate limit", "temporarily unavailable", "timeout", "timed out")):
            raise RetryableBackendError(message)
        if any(token in lowered for token in ("connection", "network", "unavailable", "try again")):
            raise RetryableBackendError(message)
        raise PermanentBackendError(message)

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
            self._read_stream_limited(process.stdout, self.config.backends.codex_cli.max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            self._read_stream_limited(process.stderr, self.config.backends.codex_cli.max_output_bytes)
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise RetryableBackendError("Codex CLI timed out.") from exc

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

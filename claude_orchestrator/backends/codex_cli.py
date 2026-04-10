"""Optional OpenAI Codex CLI backend wrapper with durable stream events."""

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
        workspace_result = prepare_job_workspace(context.workspace_root, job.id, job.metadata)
        workspace = workspace_result.workspace_path
        prompt = resolve_job_prompt(job)
        prompt_file = workspace / "codex_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        command = self._build_command(workspace=workspace, prompt_file=prompt_file)
        returncode, stdout_text, stderr_text, stdout_truncated, stderr_truncated, latest_phase = await self._run_subprocess(
            command,
            cwd=workspace,
            timeout_seconds=self.config.backends.codex_cli.timeout_seconds,
            context=context,
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
                        "latest_phase": latest_phase,
                        "workspace_kind": workspace_result.workspace_kind,
                        "worktree_path": str(workspace_result.worktree_path) if workspace_result.worktree_path else None,
                        "branch_name": workspace_result.branch_name,
                    },
                )
            ],
            request_payload={
                "command": command,
                "prompt_file": str(prompt_file),
                "workspace": str(workspace),
                "provider": job.provider,
                "backend": job.backend,
                "workspace_kind": workspace_result.workspace_kind,
            },
            response_summary={
                "stdout_preview": stdout_text[:preview_limit],
                "stderr_preview": stderr_text[:preview_limit],
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "latest_phase": latest_phase,
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
        context: BackendContext,
    ) -> Tuple[int, str, str, bool, bool, Optional[str]]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(str(exc)) from exc

        current_phase: Optional[str] = None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_truncated = False
        stderr_truncated = False
        byte_limit = self.config.backends.codex_cli.max_output_bytes

        self._emit_event(
            context,
            event_type="process_started",
            phase=None,
            message="Codex CLI process started.",
            metadata={"command": command, "cwd": str(cwd), "pid": process.pid},
        )

        async def pump_stream(stream: Optional[asyncio.StreamReader], source: str) -> None:
            nonlocal current_phase, stdout_truncated, stderr_truncated
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                stripped_message = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if source == "stdout":
                    stdout_truncated = self._append_limited(stdout_buffer, line, byte_limit) or stdout_truncated
                else:
                    stderr_truncated = self._append_limited(stderr_buffer, line, byte_limit) or stderr_truncated

                inferred_phase = self._infer_phase(stripped_message)
                if inferred_phase and inferred_phase != current_phase:
                    current_phase = inferred_phase
                    self._emit_event(
                        context,
                        event_type="phase_changed",
                        phase=current_phase,
                        message=stripped_message or f"Entered {current_phase} phase.",
                        metadata={"source": source},
                    )
                if stripped_message:
                    self._emit_event(
                        context,
                        event_type=f"{source}_line",
                        phase=current_phase,
                        message=stripped_message,
                        metadata={"source": source},
                    )

        stdout_task = asyncio.create_task(pump_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(pump_stream(process.stderr, "stderr"))
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._emit_event(
                context,
                event_type="process_timeout",
                phase=current_phase,
                message="Codex CLI timed out.",
                metadata={"timeout_seconds": timeout_seconds},
            )
            await self._terminate_process(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise RetryableBackendError("Codex CLI timed out.") from exc
        except asyncio.CancelledError:
            self._emit_event(
                context,
                event_type="process_interrupted",
                phase=current_phase,
                message="Codex CLI interrupted during worker shutdown.",
                metadata={},
            )
            await self._terminate_process(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        await asyncio.gather(stdout_task, stderr_task)

        stdout_text = stdout_buffer.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_buffer.decode("utf-8", errors="replace").strip()
        self._emit_event(
            context,
            event_type="process_completed" if returncode == 0 else "process_failed",
            phase=current_phase or ("finalizing" if returncode == 0 else None),
            message=(stdout_text or stderr_text or f"Codex CLI exited with code {returncode}")[:500],
            metadata={"returncode": returncode},
        )
        return returncode, stdout_text, stderr_text, stdout_truncated, stderr_truncated, current_phase

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        terminate = getattr(process, "terminate", None)
        if callable(terminate):
            terminate()
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    def _append_limited(self, buffer: bytearray, chunk: bytes, byte_limit: int) -> bool:
        remaining = max(byte_limit - len(buffer), 0)
        if remaining > 0:
            buffer.extend(chunk[:remaining])
        return len(chunk) > remaining

    def _infer_phase(self, message: str) -> Optional[str]:
        lowered = message.lower()
        if any(token in lowered for token in ("plan", "planning", "inspect", "analy", "investigat")):
            return "planning"
        if any(token in lowered for token in ("edit", "patch", "write", "modify", "update file")):
            return "editing"
        if any(token in lowered for token in ("test", "pytest", "lint", "check", "verification")):
            return "testing"
        if any(token in lowered for token in ("final", "summary", "completed", "done")):
            return "finalizing"
        return None

    def _emit_event(
        self,
        context: BackendContext,
        *,
        event_type: str,
        phase: Optional[str],
        message: str,
        metadata: Dict[str, object],
    ) -> None:
        emit_stream_event = getattr(context, "emit_stream_event", None)
        if emit_stream_event is None:
            return
        emit_stream_event(event_type, phase, message, metadata)

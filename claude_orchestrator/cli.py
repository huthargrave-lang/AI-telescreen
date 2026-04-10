"""Typer-based command line for local orchestration operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from .bootstrap import bootstrap
from .config import render_example_config
from .models import EnqueueJobRequest
from .services.orchestrator import OrchestratorService
from .services.worker import WorkerService

try:  # pragma: no cover - exercised when dependency is installed.
    import typer
except ImportError:  # pragma: no cover - keeps module importable in minimal test envs.
    typer = None  # type: ignore[assignment]

try:  # pragma: no cover - optional local-dev dependency.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for test envs.
    load_dotenv = None  # type: ignore[assignment]


def _build_services(config_path: Optional[Path] = None) -> tuple[OrchestratorService, WorkerService]:
    root = Path.cwd()
    if load_dotenv is not None:
        load_dotenv()
    context = bootstrap(root, config_path=config_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    worker = WorkerService(context.repository, orchestrator)
    return orchestrator, worker


def _load_metadata(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    if raw.strip().startswith("@"):
        return json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return json.loads(raw)


if typer is not None:  # pragma: no branch
    app = typer.Typer(help="Durable local orchestration for official Anthropic workflows.")

    @app.command("enqueue")
    def enqueue_command(
        prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt text to enqueue."),
        prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="Read prompt text from a file."),
        backend: Optional[str] = typer.Option(None, "--backend", help="Backend name."),
        task_type: str = typer.Option("general", "--task-type", help="Logical task type."),
        priority: int = typer.Option(0, "--priority", help="Higher numbers run first."),
        metadata: Optional[str] = typer.Option(None, "--metadata", help="Inline JSON or @path/to/file.json."),
        system_prompt: Optional[str] = typer.Option(None, "--system-prompt", help="Optional system prompt."),
        idempotency_key: Optional[str] = typer.Option(None, "--idempotency-key", help="De-duplicate equivalent jobs."),
        max_attempts: int = typer.Option(5, "--max-attempts", help="Max attempts before permanent failure."),
        model: Optional[str] = typer.Option(None, "--model", help="Model override."),
        privacy_mode: bool = typer.Option(False, "--privacy", help="Store prompt outside SQLite."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        prompt_text = prompt or (prompt_file.read_text(encoding="utf-8") if prompt_file else None)
        if not prompt_text:
            raise typer.BadParameter("Provide either --prompt or --prompt-file.")
        job = orchestrator.enqueue(
            EnqueueJobRequest(
                backend=backend or orchestrator.config.default_backend,
                task_type=task_type,
                prompt=prompt_text,
                priority=priority,
                metadata=_load_metadata(metadata),
                max_attempts=max_attempts,
                idempotency_key=idempotency_key,
                system_prompt=system_prompt,
                model=model,
                privacy_mode=privacy_mode,
            )
        )
        typer.echo(f"enqueued {job.id} [{job.status.value}] backend={job.backend}")

    @app.command("run-worker")
    def run_worker_command(
        once: bool = typer.Option(False, "--once", help="Process available work and then exit."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, worker = _build_services(config)
        _ = orchestrator
        asyncio.run(worker.run_forever(run_once=once))

    @app.command("run-daemon")
    def run_daemon_command(
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        _, worker = _build_services(config)
        asyncio.run(worker.run_forever(run_once=False))

    @app.command("retry-due")
    def retry_due_command(
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        count = orchestrator.retry_due()
        typer.echo(f"requeued {count} due jobs")

    @app.command("status")
    def status_command(
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        counts = orchestrator.status()
        for name, count in counts.items():
            typer.echo(f"{name:14} {count}")

    @app.command("inspect")
    def inspect_command(
        job_id: str,
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        details = orchestrator.inspect(job_id)
        job = details.job
        typer.echo(f"job {job.id}")
        typer.echo(f"  status: {job.status.value}")
        typer.echo(f"  backend: {job.backend}")
        typer.echo(f"  attempts: {job.attempt_count}/{job.max_attempts}")
        typer.echo(f"  next_retry_at: {job.next_retry_at}")
        typer.echo(f"  last_error: {job.last_error_code} {job.last_error_message}")
        typer.echo(f"  workspace: {job.workspace_path}")
        typer.echo("  recent events:")
        for event in details.events[-10:]:
            typer.echo(f"    - {event.timestamp.isoformat()} {event.event_type} {json.dumps(event.detail)}")

    @app.command("cancel")
    def cancel_command(
        job_id: str,
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        job = orchestrator.cancel(job_id)
        typer.echo(f"{job.id} -> {job.status.value}")

    @app.command("retry")
    def retry_command(
        job_id: str,
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        job = orchestrator.retry_job(job_id)
        typer.echo(f"{job.id} -> {job.status.value}")

    @app.command("list")
    def list_command(
        status: Optional[str] = typer.Option(None, "--status", help="Filter by status."),
        backend: Optional[str] = typer.Option(None, "--backend", help="Filter by backend."),
        limit: int = typer.Option(50, "--limit", help="Maximum rows."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        jobs = orchestrator.list_jobs(status=status, backend=backend, limit=limit)
        for job in jobs:
            typer.echo(
                f"{job.id} {job.status.value:13} backend={job.backend:16} "
                f"priority={job.priority:3} attempts={job.attempt_count}/{job.max_attempts}"
            )

    @app.command("purge-completed")
    def purge_completed_command(
        older_than_days: int = typer.Option(7, "--older-than-days", help="Purge records older than this."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        purged = orchestrator.purge_completed(older_than_days=older_than_days)
        typer.echo(f"purged {purged} jobs")

    @app.command("export-logs")
    def export_logs_command(
        output: Path = typer.Argument(..., help="Destination JSON file."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        count = orchestrator.export_logs(output)
        typer.echo(f"exported {count} log rows to {output}")

    @app.command("migrate")
    def migrate_command(
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        bootstrap(Path.cwd(), config_path=config)
        typer.echo("database migrated")

    @app.command("config-init")
    def config_init_command(
        output: Path = typer.Argument(Path("claude-orchestrator.toml"), help="Destination TOML file."),
    ) -> None:
        output.write_text(render_example_config(), encoding="utf-8")
        typer.echo(f"wrote {output}")

    @app.command("doctor")
    def doctor_command(
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        typer.echo(f"sqlite: {orchestrator.config.sqlite_path(Path.cwd())}")
        typer.echo(f"default backend: {orchestrator.config.default_backend}")
        typer.echo(f"workspace root: {orchestrator.config.workspace_path(Path.cwd())}")
        typer.echo(f"privacy mode: {orchestrator.config.privacy.enabled}")

else:
    app = None


def main() -> None:
    """Console entrypoint."""

    if typer is None:
        raise RuntimeError("Typer is not installed. Install project dependencies to use the CLI.")
    app()

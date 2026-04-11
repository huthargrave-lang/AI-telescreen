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


def _integration_payload(details) -> dict:
    summary = details.integration_summary
    summary_payload = summary.to_dict() if summary is not None else None
    status = summary.status() if summary is not None else "not_scanned"
    labels = summary.status_labels() if summary is not None else ["not scanned"]
    return {
        "status": status,
        "labels": labels,
        "summary": summary_payload,
        "backend_support": details.backend_integration_support,
    }


if typer is not None:  # pragma: no branch
    app = typer.Typer(help="AI Telescreen: durable local orchestration for provider-aware coding-agent workflows.")

    @app.command("enqueue")
    def enqueue_command(
        prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt text to enqueue."),
        prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="Read prompt text from a file."),
        backend: Optional[str] = typer.Option(None, "--backend", help="Backend name."),
        provider: Optional[str] = typer.Option(None, "--provider", help="Provider name override."),
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
                provider=provider,
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
        typer.echo(f"enqueued {job.id} [{job.status.value}] provider={job.provider} backend={job.backend}")

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
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        details = orchestrator.inspect(job_id)
        job = details.job
        payload = {
            "job": {
                "id": job.id,
                "status": job.status.value,
                "provider": job.provider,
                "backend": job.backend,
                "task_type": job.task_type,
                "attempts": {"current": job.attempt_count, "max": job.max_attempts},
                "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
                "last_error_code": job.last_error_code,
                "last_error_message": job.last_error_message,
                "workspace_path": job.workspace_path,
                "workspace_kind": job.metadata.get("workspace_kind"),
                "repo_path": job.metadata.get("repo_path"),
                "worktree_path": job.metadata.get("worktree_path"),
                "branch_name": job.metadata.get("branch_name"),
                "base_branch": job.metadata.get("base_branch"),
                "cleanup_policy": job.metadata.get("cleanup_policy"),
                "lease_owner": job.lease_owner,
                "lease_expires_at": job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                "metadata": job.metadata,
            },
            "state": {
                "compact_summary": details.state.compact_summary,
                "tool_context": details.state.tool_context,
                "last_checkpoint_at": details.state.last_checkpoint_at.isoformat()
                if details.state.last_checkpoint_at
                else None,
            },
            "runs": [
                {
                    "id": run.id,
                    "started_at": run.started_at.isoformat(),
                    "ended_at": run.ended_at.isoformat() if run.ended_at else None,
                    "usage": run.usage,
                    "exit_reason": run.exit_reason,
                    "response_summary": run.response_summary,
                    "error": run.error,
                }
                for run in details.runs[-5:]
            ],
            "events": [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "detail": event.detail,
                }
                for event in details.events[-15:]
            ],
            "stream_events": [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "phase": event.phase,
                    "message": event.message,
                    "metadata": event.metadata,
                }
                for event in details.stream_events[-20:]
            ],
            "latest_phase": details.latest_phase,
            "latest_progress_message": details.latest_progress_message,
            "integration": _integration_payload(details),
            "artifacts": [
                {
                    "created_at": artifact.created_at.isoformat(),
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "metadata": artifact.metadata,
                }
                for artifact in details.artifacts
            ],
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(f"job {job.id}")
        typer.echo(f"  status: {job.status.value}")
        typer.echo(f"  provider: {job.provider}")
        typer.echo(f"  backend: {job.backend}")
        typer.echo(f"  task_type: {job.task_type}")
        typer.echo(f"  attempts: {job.attempt_count}/{job.max_attempts}")
        typer.echo(f"  next_retry_at: {job.next_retry_at}")
        typer.echo(f"  followup: {job.metadata.get('followup_type') or job.metadata.get('resume_hint') or 'n/a'}")
        typer.echo(f"  last_error: {job.last_error_code} {job.last_error_message}")
        typer.echo(f"  workspace: {job.workspace_path}")
        typer.echo(f"  workspace_kind: {job.metadata.get('workspace_kind') or 'directory'}")
        typer.echo(f"  repo_path: {job.metadata.get('repo_path') or 'n/a'}")
        typer.echo(f"  worktree_path: {job.metadata.get('worktree_path') or 'n/a'}")
        typer.echo(f"  branch_name: {job.metadata.get('branch_name') or 'n/a'}")
        typer.echo(f"  base_branch: {job.metadata.get('base_branch') or 'n/a'}")
        typer.echo(f"  cleanup_policy: {job.metadata.get('cleanup_policy') or 'none'}")
        typer.echo(f"  lease_owner: {job.lease_owner or 'n/a'}")
        typer.echo(f"  lease_expiry: {job.lease_expires_at or 'n/a'}")
        typer.echo(f"  latest_phase: {details.latest_phase or 'n/a'}")
        typer.echo(f"  latest_progress: {details.latest_progress_message or 'n/a'}")
        typer.echo(f"  integration_status: {_integration_payload(details)['status']}")
        typer.echo(f"  integration_labels: {', '.join(_integration_payload(details)['labels'])}")
        typer.echo(
            "  backend_integrations: "
            f"{details.backend_integration_support['effective_mode']} "
            f"(project_mcp={details.backend_integration_support['supports_project_mcp']}, "
            f"user_mcp={details.backend_integration_support['supports_user_mcp']})"
        )
        if details.integration_summary and details.integration_summary.config_paths:
            typer.echo("  integration_config_paths:")
            for config_path in details.integration_summary.config_paths:
                typer.echo(f"    - {config_path}")
        if details.integration_summary and details.integration_summary.capabilities:
            typer.echo("  integration_capabilities:")
            for capability in details.integration_summary.capabilities:
                status_label = "enabled" if capability.enabled else "disabled"
                typer.echo(
                    f"    - {capability.name} [{capability.kind}] source={capability.source} status={status_label}"
                )
        if details.integration_summary and details.integration_summary.notes:
            typer.echo("  integration_notes:")
            for note in details.integration_summary.notes:
                typer.echo(f"    - {note}")
        typer.echo(f"  compact_summary: {details.state.compact_summary or 'n/a'}")
        typer.echo(f"  tool_context: {json.dumps(details.state.tool_context)}")
        if details.runs:
            last_run = details.runs[-1]
            typer.echo("  last run:")
            typer.echo(f"    exit_reason: {last_run.exit_reason}")
            typer.echo(f"    usage: {json.dumps(last_run.usage)}")
            typer.echo(f"    response: {json.dumps(last_run.response_summary)}")
        typer.echo("  recent events:")
        for event in details.events[-10:]:
            typer.echo(f"    - {event.timestamp.isoformat()} {event.event_type} {json.dumps(event.detail)}")
        typer.echo("  recent stream events:")
        for event in details.stream_events[-10:]:
            typer.echo(
                f"    - {event.timestamp.isoformat()} {event.event_type} phase={event.phase or '-'} {event.message}"
            )

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
        provider: Optional[str] = typer.Option(None, "--provider", help="Filter by provider."),
        backend: Optional[str] = typer.Option(None, "--backend", help="Filter by backend."),
        limit: int = typer.Option(50, "--limit", help="Maximum rows."),
        config: Optional[Path] = typer.Option(None, "--config", help="Config TOML path."),
    ) -> None:
        orchestrator, _ = _build_services(config)
        jobs = orchestrator.list_jobs(status=status, provider=provider, backend=backend, limit=limit)
        for job in jobs:
            details = orchestrator.inspect(job.id)
            typer.echo(
                f"{job.id} {job.status.value:13} provider={job.provider:10} backend={job.backend:16} "
                f"priority={job.priority:3} attempts={job.attempt_count}/{job.max_attempts} "
                f"workspace={job.metadata.get('workspace_kind') or 'directory':12} "
                f"phase={details.latest_phase or '-':12} "
                f"followup={job.metadata.get('followup_type') or job.metadata.get('resume_hint') or '-'}"
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
        typer.echo("provider/backend split: provider is inferred from backend unless --provider is supplied.")
        typer.echo(f"enabled backends: {', '.join(sorted(orchestrator.backends))}")
        typer.echo(f"workspace root: {orchestrator.config.workspace_path(Path.cwd())}")
        typer.echo(f"privacy mode: {orchestrator.config.privacy.enabled}")
        typer.echo(f"lease seconds: {orchestrator.config.effective_lease_seconds()}")
        typer.echo(f"heartbeat interval: {orchestrator.config.worker.heartbeat_interval_seconds}")

else:
    app = None


def main() -> None:
    """Console entrypoint."""

    if typer is None:
        raise RuntimeError("Typer is not installed. Install project dependencies to use the CLI.")
    app()

"""FastAPI application for the lightweight operator dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..bootstrap import bootstrap
from ..models import JobStreamEvent, JobStatus
from ..providers import infer_provider
from ..services.orchestrator import OrchestratorService

try:  # pragma: no cover - optional dependency in the sandbox.
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.templating import Jinja2Templates
except ImportError:  # pragma: no cover - keeps module importable in tests.
    FastAPI = None  # type: ignore[assignment]
    Request = object  # type: ignore[assignment]
    HTMLResponse = object  # type: ignore[assignment]
    RedirectResponse = object  # type: ignore[assignment]
    Jinja2Templates = None  # type: ignore[assignment]


def build_app(root: Optional[Path] = None, config_path: Optional[Path] = None):
    """Create the dashboard application."""

    if FastAPI is None or Jinja2Templates is None:
        raise RuntimeError("FastAPI and Jinja2 must be installed to run the web dashboard.")

    root = root or Path.cwd()
    context = bootstrap(root, config_path=config_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app = FastAPI(title="claude-orchestrator")

    def _stream_snapshot(job_id: str) -> tuple[Optional[JobStreamEvent], Optional[str], Optional[str]]:
        events = orchestrator.repository.get_stream_events(job_id, limit=50)
        latest = events[-1] if events else None
        latest_phase = next((event.phase for event in reversed(events) if event.phase), None)
        latest_progress = next((event.message for event in reversed(events) if event.message), None)
        return latest, latest_phase, latest_progress

    def _job_detail_context(request: Request, job_id: str) -> dict:
        details = orchestrator.inspect(job_id)
        return {
            "request": request,
            "job": details.job,
            "state": details.state,
            "runs": details.runs,
            "events": details.events,
            "stream_events": details.stream_events,
            "latest_stream_event": details.latest_stream_event,
            "latest_phase": details.latest_phase,
            "latest_progress_message": details.latest_progress_message,
            "artifacts": details.artifacts,
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
            "is_active": details.job.status == JobStatus.RUNNING,
        }

    def render_dashboard_context(request: Request) -> dict:
        status = request.query_params.get("status") or None
        provider = request.query_params.get("provider") or None
        backend = request.query_params.get("backend") or None
        jobs = orchestrator.list_jobs(status=status, provider=provider, backend=backend, limit=50)
        return {
            "request": request,
            "counts": orchestrator.status(),
            "jobs": jobs,
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
            "filters": {"status": status, "provider": provider, "backend": backend},
            "available_providers": sorted(
                {job.provider for job in orchestrator.list_jobs(limit=200)}
                | {infer_provider(name) for name in orchestrator.backends.keys()}
            ),
            "available_backends": sorted(orchestrator.backends.keys()),
        }

    def serialize_job(job) -> dict:
        latest_stream_event, latest_phase, latest_progress = _stream_snapshot(job.id)
        return {
            "id": job.id,
            "status": job.status.value,
            "provider": job.provider,
            "backend": job.backend,
            "task_type": job.task_type,
            "priority": job.priority,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "model": job.model,
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
            "latest_phase": latest_phase,
            "latest_progress_message": latest_progress,
            "last_activity_at": latest_stream_event.timestamp.isoformat() if latest_stream_event else job.updated_at.isoformat(),
            "metadata": job.metadata,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=render_dashboard_context(request),
        )

    @app.get("/partials/counts", response_class=HTMLResponse)
    async def counts_partial(request: Request):
        context_data = render_dashboard_context(request)
        return templates.TemplateResponse(
            request=request,
            name="partials/job_counts.html",
            context=context_data,
        )

    @app.get("/partials/jobs", response_class=HTMLResponse)
    async def jobs_partial(request: Request):
        context_data = render_dashboard_context(request)
        return templates.TemplateResponse(
            request=request,
            name="partials/job_table.html",
            context=context_data,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: str):
        return templates.TemplateResponse(
            request=request,
            name="job_detail.html",
            context=_job_detail_context(request, job_id),
        )

    @app.get("/partials/jobs/{job_id}/overview", response_class=HTMLResponse)
    async def job_overview_partial(request: Request, job_id: str):
        return templates.TemplateResponse(
            request=request,
            name="partials/job_overview.html",
            context=_job_detail_context(request, job_id),
        )

    @app.get("/partials/jobs/{job_id}/stream-events", response_class=HTMLResponse)
    async def job_stream_events_partial(request: Request, job_id: str):
        return templates.TemplateResponse(
            request=request,
            name="partials/job_stream_events.html",
            context=_job_detail_context(request, job_id),
        )

    @app.get("/api/status")
    async def api_status():
        return {
            "counts": orchestrator.status(),
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
        }

    @app.get("/api/jobs")
    async def api_jobs(
        limit: int = 50,
        status: Optional[str] = None,
        provider: Optional[str] = None,
        backend: Optional[str] = None,
    ):
        return {
            "jobs": [
                serialize_job(job)
                for job in orchestrator.list_jobs(limit=limit, status=status, provider=provider, backend=backend)
            ]
        }

    @app.get("/api/jobs/{job_id}")
    async def api_job_detail(job_id: str):
        details = orchestrator.inspect(job_id)
        return {
            "job": serialize_job(details.job),
            "state": {
                "compact_summary": details.state.compact_summary,
                "last_checkpoint_at": details.state.last_checkpoint_at.isoformat()
                if details.state.last_checkpoint_at
                else None,
                "tool_context": details.state.tool_context,
            },
            "runs": [
                {
                    "id": run.id,
                    "started_at": run.started_at.isoformat(),
                    "ended_at": run.ended_at.isoformat() if run.ended_at else None,
                    "provider": run.provider,
                    "backend": run.backend,
                    "usage": run.usage,
                    "exit_reason": run.exit_reason,
                    "response_summary": run.response_summary,
                    "error": run.error,
                }
                for run in details.runs
            ],
            "events": [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "detail": event.detail,
                }
                for event in details.events
            ],
            "latest_phase": details.latest_phase,
            "latest_progress_message": details.latest_progress_message,
            "stream_events": [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "phase": event.phase,
                    "message": event.message,
                    "metadata": event.metadata,
                }
                for event in details.stream_events
            ],
        }

    @app.get("/api/jobs/{job_id}/stream-events")
    async def api_job_stream_events(job_id: str, limit: int = 100):
        events = orchestrator.repository.get_stream_events(job_id, limit=limit)
        latest_phase = next((event.phase for event in reversed(events) if event.phase), None)
        latest_progress = next((event.message for event in reversed(events) if event.message), None)
        return {
            "latest_phase": latest_phase,
            "latest_progress_message": latest_progress,
            "events": [
                {
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "phase": event.phase,
                    "message": event.message,
                    "metadata": event.metadata,
                }
                for event in events
            ],
        }

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        orchestrator.retry_job(job_id)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        orchestrator.cancel(job_id)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    return app

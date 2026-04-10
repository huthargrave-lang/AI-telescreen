"""FastAPI application for the lightweight operator dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..bootstrap import bootstrap
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
    templates = Jinja2Templates(directory=str(root / "claude_orchestrator" / "web" / "templates"))
    app = FastAPI(title="claude-orchestrator")

    def render_dashboard_context(request: Request) -> dict:
        jobs = orchestrator.list_jobs(limit=50)
        return {
            "request": request,
            "counts": orchestrator.status(),
            "jobs": jobs,
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", render_dashboard_context(request))

    @app.get("/partials/counts", response_class=HTMLResponse)
    async def counts_partial(request: Request):
        context_data = render_dashboard_context(request)
        return templates.TemplateResponse("partials/job_counts.html", context_data)

    @app.get("/partials/jobs", response_class=HTMLResponse)
    async def jobs_partial(request: Request):
        context_data = render_dashboard_context(request)
        return templates.TemplateResponse("partials/job_table.html", context_data)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: str):
        details = orchestrator.inspect(job_id)
        return templates.TemplateResponse(
            "job_detail.html",
            {
                "request": request,
                "job": details.job,
                "state": details.state,
                "runs": details.runs,
                "events": details.events,
                "artifacts": details.artifacts,
            },
        )

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        orchestrator.retry_job(job_id)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        orchestrator.cancel(job_id)
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    return app

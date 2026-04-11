"""FastAPI application for the lightweight operator dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from ..bootstrap import bootstrap
from ..models import EnqueueJobRequest
from ..models import JobStreamEvent, JobStatus
from ..providers import infer_provider
from ..services.orchestrator import OrchestratorService
from ..services.worker import WorkerService
from ..workspaces import resolve_job_prompt

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
        config_path=context.config_path,
    )
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app = FastAPI(title="AI Telescreen")

    def _available_backends() -> list[str]:
        return sorted(orchestrator.backends.keys())

    def _available_providers() -> list[str]:
        return sorted(
            {job.provider for job in orchestrator.list_jobs(limit=200)}
            | {infer_provider(name) for name in orchestrator.backends.keys()}
        )

    def serialize_integration(summary, backend_name: str) -> dict:
        backend_support = orchestrator.describe_backend_integrations(
            backend_name,
            integration_summary=summary,
        )
        if summary is None:
            return {
                "status": "not_scanned",
                "labels": ["not scanned"],
                "summary": None,
                "backend_support": backend_support,
            }
        return {
            "status": summary.status(),
            "labels": summary.status_labels(),
            "summary": summary.to_dict(),
            "backend_support": backend_support,
        }

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
            "integration": serialize_integration(details.integration_summary, details.job.backend),
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
            "is_active": details.job.status == JobStatus.RUNNING,
        }

    def _default_job_form_values() -> dict:
        return {
            "prompt": "",
            "backend": orchestrator.config.default_backend,
            "provider": "",
            "task_type": "code",
            "priority": "0",
            "max_attempts": "5",
            "repo_path": "",
            "use_git_worktree": False,
            "base_branch": "",
            "project_id": "",
        }

    def _job_form_context(
        request: Request,
        *,
        form_values: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> dict:
        values = _default_job_form_values()
        if form_values:
            values.update(form_values)
        selected_project = None
        project_id = values.get("project_id")
        if project_id:
            try:
                selected_project = orchestrator.get_saved_project(project_id)
            except KeyError:
                selected_project = None
        return {
            "request": request,
            "form_values": values,
            "projects": orchestrator.list_saved_projects(),
            "available_backends": _available_backends(),
            "available_providers": _available_providers(),
            "selected_project": selected_project,
            "error": error,
        }

    def _project_form_context(
        request: Request,
        *,
        form_values: Optional[dict] = None,
        error: Optional[str] = None,
        project=None,
    ) -> dict:
        values = {
            "name": "",
            "repo_path": "",
            "default_backend": orchestrator.config.default_backend,
            "default_provider": "",
            "default_base_branch": "",
            "default_use_git_worktree": False,
            "notes": "",
        }
        if form_values:
            values.update(form_values)
        return {
            "request": request,
            "form_values": values,
            "available_backends": _available_backends(),
            "available_providers": _available_providers(),
            "error": error,
            "project": project,
            "page_title": "Edit Project" if project else "Add Project",
            "page_subtitle": (
                "Update launch defaults and repository notes for this saved project."
                if project
                else "Register a repository once, then launch jobs with browser-friendly defaults."
            ),
            "form_action": f"/projects/{project.id}/edit" if project else "/projects",
            "submit_label": "Save Changes" if project else "Save Project",
            "cancel_href": f"/projects/{project.id}" if project else "/projects",
        }

    def _coerce_checkbox(value) -> bool:
        return str(value).lower() in {"1", "true", "on", "yes"}

    def _redirect_target(request: Request, default: str) -> str:
        return request.query_params.get("next") or default

    async def _parse_form_values(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _job_form_values_from_project(project_id: str) -> dict:
        project = orchestrator.get_saved_project(project_id)
        values = _default_job_form_values()
        values.update(
            {
                "backend": project.default_backend or orchestrator.config.default_backend,
                "provider": project.default_provider or "",
                "repo_path": project.repo_path,
                "use_git_worktree": project.default_use_git_worktree,
                "base_branch": project.default_base_branch or "",
                "project_id": project.id,
            }
        )
        return values

    def _job_form_values_from_job(job_id: str) -> dict:
        job = orchestrator.repository.get_job(job_id)
        values = _default_job_form_values()
        values.update(
            {
                "prompt": resolve_job_prompt(job),
                "backend": job.backend,
                "provider": job.provider,
                "task_type": job.task_type,
                "priority": str(job.priority),
                "max_attempts": str(job.max_attempts),
                "repo_path": job.metadata.get("repo_path") or "",
                "use_git_worktree": bool(job.metadata.get("use_git_worktree")),
                "base_branch": job.metadata.get("base_branch") or "",
                "project_id": job.metadata.get("project_id") or "",
            }
        )
        return values

    def render_dashboard_context(request: Request) -> dict:
        status = request.query_params.get("status") or None
        provider = request.query_params.get("provider") or None
        backend = request.query_params.get("backend") or None
        jobs = orchestrator.list_jobs(status=status, provider=provider, backend=backend, limit=50)
        counts = orchestrator.status()
        return {
            "request": request,
            "counts": counts,
            "jobs": jobs,
            "projects": orchestrator.list_saved_projects(),
            "refresh_seconds": orchestrator.config.ui.refresh_seconds,
            "filters": {"status": status, "provider": provider, "backend": backend},
            "available_providers": _available_providers(),
            "available_backends": _available_backends(),
            "worker_panel": {
                "running_jobs": counts.get("running", 0),
                "queued_jobs": counts.get("queued", 0),
                "waiting_retry_jobs": counts.get("waiting_retry", 0),
            },
        }

    def serialize_job(job) -> dict:
        latest_stream_event, latest_phase, latest_progress = _stream_snapshot(job.id)
        integration_snapshot = job.metadata.get("integration_summary") or {}
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
            "integration": {
                "status": integration_snapshot.get("status", "not_scanned"),
                "labels": integration_snapshot.get("labels", ["not scanned"]),
                "capability_names": integration_snapshot.get("capability_names", []),
                "capability_count": integration_snapshot.get("capability_count", 0),
                "config_paths": integration_snapshot.get("config_paths", []),
            },
            "metadata": job.metadata,
        }

    def serialize_project_integration(summary) -> dict:
        return {
            "status": summary.status(),
            "labels": summary.status_labels(),
            "summary": summary.to_dict(),
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

    @app.get("/doctor", response_class=HTMLResponse)
    async def doctor_page(request: Request):
        report = await asyncio.to_thread(orchestrator.collect_diagnostics, run_smoke_tests=True)
        return templates.TemplateResponse(
            request=request,
            name="doctor.html",
            context={"request": request, "report": report},
        )

    @app.get("/api/doctor")
    async def api_doctor():
        report = await asyncio.to_thread(orchestrator.collect_diagnostics, run_smoke_tests=True)
        return report.to_dict()

    @app.get("/jobs/new", response_class=HTMLResponse)
    async def new_job(request: Request, project_id: Optional[str] = None, from_job: Optional[str] = None):
        form_values = _default_job_form_values()
        if project_id:
            form_values.update(_job_form_values_from_project(project_id))
        if from_job:
            form_values.update(_job_form_values_from_job(from_job))
        return templates.TemplateResponse(
            request=request,
            name="job_new.html",
            context=_job_form_context(request, form_values=form_values),
        )

    @app.post("/jobs", response_class=HTMLResponse)
    async def create_job(request: Request):
        form = await _parse_form_values(request)
        form_values = {
            "prompt": str(form.get("prompt") or ""),
            "backend": str(form.get("backend") or orchestrator.config.default_backend),
            "provider": str(form.get("provider") or ""),
            "task_type": str(form.get("task_type") or "code"),
            "priority": str(form.get("priority") or "0"),
            "max_attempts": str(form.get("max_attempts") or "5"),
            "repo_path": str(form.get("repo_path") or ""),
            "use_git_worktree": _coerce_checkbox(form.get("use_git_worktree")),
            "base_branch": str(form.get("base_branch") or ""),
            "project_id": str(form.get("project_id") or ""),
        }
        try:
            prompt = form_values["prompt"].strip()
            if not prompt:
                raise ValueError("Prompt is required.")
            priority = int(form_values["priority"] or 0)
            max_attempts = int(form_values["max_attempts"] or 5)
            provider = form_values["provider"] or None
            project_id = form_values["project_id"] or None
            if project_id:
                job = orchestrator.launch_job_from_project(
                    project_id,
                    prompt=prompt,
                    task_type=form_values["task_type"] or "code",
                    priority=priority,
                    max_attempts=max_attempts,
                    backend=form_values["backend"] or None,
                    provider=provider,
                    use_git_worktree=form_values["use_git_worktree"],
                    base_branch=form_values["base_branch"] or None,
                )
            else:
                metadata = {
                    key: value
                    for key, value in {
                        "repo_path": form_values["repo_path"] or None,
                        "use_git_worktree": form_values["use_git_worktree"],
                        "base_branch": form_values["base_branch"] or None,
                    }.items()
                    if value not in (None, "")
                }
                job = orchestrator.enqueue(
                    EnqueueJobRequest(
                        backend=form_values["backend"] or orchestrator.config.default_backend,
                        provider=provider,
                        task_type=form_values["task_type"] or "code",
                        prompt=prompt,
                        priority=priority,
                        metadata=metadata,
                        max_attempts=max_attempts,
                    )
                )
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="job_new.html",
                context=_job_form_context(request, form_values=form_values, error=str(exc)),
                status_code=400,
            )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

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
            "integration": serialize_integration(details.integration_summary, details.job.backend),
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

    @app.get("/projects", response_class=HTMLResponse)
    async def projects_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="projects.html",
            context={"request": request, "projects": orchestrator.list_saved_projects()},
        )

    @app.get("/projects/new", response_class=HTMLResponse)
    async def new_project(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="project_new.html",
            context=_project_form_context(request),
        )

    @app.post("/projects", response_class=HTMLResponse)
    async def create_project(request: Request):
        form = await _parse_form_values(request)
        form_values = {
            "name": str(form.get("name") or ""),
            "repo_path": str(form.get("repo_path") or ""),
            "default_backend": str(form.get("default_backend") or orchestrator.config.default_backend),
            "default_provider": str(form.get("default_provider") or ""),
            "default_base_branch": str(form.get("default_base_branch") or ""),
            "default_use_git_worktree": _coerce_checkbox(form.get("default_use_git_worktree")),
            "notes": str(form.get("notes") or ""),
        }
        try:
            if not form_values["name"].strip():
                raise ValueError("Project name is required.")
            if not form_values["repo_path"].strip():
                raise ValueError("Repository path is required.")
            project = orchestrator.create_saved_project(
                name=form_values["name"].strip(),
                repo_path=form_values["repo_path"].strip(),
                default_backend=form_values["default_backend"] or None,
                default_provider=form_values["default_provider"] or None,
                default_base_branch=form_values["default_base_branch"] or None,
                default_use_git_worktree=form_values["default_use_git_worktree"],
                notes=form_values["notes"].strip() or None,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="project_new.html",
                context=_project_form_context(request, form_values=form_values, error=str(exc)),
                status_code=400,
            )
        return RedirectResponse(url=f"/projects/{project.id}", status_code=303)

    @app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
    async def edit_project(request: Request, project_id: str):
        project = orchestrator.get_saved_project(project_id)
        return templates.TemplateResponse(
            request=request,
            name="project_new.html",
            context=_project_form_context(
                request,
                project=project,
                form_values={
                    "name": project.name,
                    "repo_path": project.repo_path,
                    "default_backend": project.default_backend or orchestrator.config.default_backend,
                    "default_provider": project.default_provider or "",
                    "default_base_branch": project.default_base_branch or "",
                    "default_use_git_worktree": project.default_use_git_worktree,
                    "notes": project.notes or "",
                },
            ),
        )

    @app.post("/projects/{project_id}/edit", response_class=HTMLResponse)
    async def update_project(request: Request, project_id: str):
        project = orchestrator.get_saved_project(project_id)
        form = await _parse_form_values(request)
        form_values = {
            "name": str(form.get("name") or ""),
            "repo_path": str(form.get("repo_path") or ""),
            "default_backend": str(form.get("default_backend") or orchestrator.config.default_backend),
            "default_provider": str(form.get("default_provider") or ""),
            "default_base_branch": str(form.get("default_base_branch") or ""),
            "default_use_git_worktree": _coerce_checkbox(form.get("default_use_git_worktree")),
            "notes": str(form.get("notes") or ""),
        }
        try:
            if not form_values["name"].strip():
                raise ValueError("Project name is required.")
            if not form_values["repo_path"].strip():
                raise ValueError("Repository path is required.")
            orchestrator.update_saved_project(
                project_id,
                name=form_values["name"].strip(),
                repo_path=form_values["repo_path"].strip(),
                default_backend=form_values["default_backend"] or None,
                default_provider=form_values["default_provider"] or None,
                default_base_branch=form_values["default_base_branch"] or None,
                default_use_git_worktree=form_values["default_use_git_worktree"],
                notes=form_values["notes"].strip() or None,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="project_new.html",
                context=_project_form_context(
                    request,
                    project=project,
                    form_values=form_values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    async def project_detail(request: Request, project_id: str):
        project = orchestrator.get_saved_project(project_id)
        jobs = [serialize_job(job) for job in orchestrator.list_project_jobs(project_id, limit=20)]
        integration_summary = orchestrator.get_project_integration_summary(project_id)
        return templates.TemplateResponse(
            request=request,
            name="project_detail.html",
            context={
                "request": request,
                "project": project,
                "jobs": jobs,
                "integration": serialize_project_integration(integration_summary),
            },
        )

    @app.post("/projects/{project_id}/launch")
    async def launch_project_job(request: Request, project_id: str):
        form = await _parse_form_values(request)
        prompt = str(form.get("prompt") or "").strip()
        if not prompt:
            return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
        job = orchestrator.launch_job_from_project(
            project_id,
            prompt=prompt,
            task_type=str(form.get("task_type") or "code"),
            priority=int(str(form.get("priority") or "0")),
            max_attempts=int(str(form.get("max_attempts") or "5")),
        )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    @app.post("/worker/run-once")
    async def worker_run_once(request: Request):
        worker = WorkerService(orchestrator.repository, orchestrator)
        await worker.run_forever(run_once=True)
        return RedirectResponse(url=_redirect_target(request, "/"), status_code=303)

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str):
        orchestrator.retry_job(job_id)
        return RedirectResponse(url=_redirect_target(request, f"/jobs/{job_id}"), status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        orchestrator.cancel(job_id)
        return RedirectResponse(url=_redirect_target(request, f"/jobs/{job_id}"), status_code=303)

    @app.post("/jobs/{job_id}/duplicate")
    async def duplicate_job(request: Request, job_id: str):
        job = orchestrator.duplicate_job(job_id)
        return RedirectResponse(url=_redirect_target(request, f"/jobs/{job.id}"), status_code=303)

    return app

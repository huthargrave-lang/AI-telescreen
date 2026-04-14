"""FastAPI application for the lightweight operator dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from ..bootstrap import bootstrap
from ..models import EnqueueJobRequest
from ..models import JobStreamEvent, JobStatus
from ..providers import infer_provider
from ..services.orchestrator import OrchestratorService
from ..services.worker import WorkerService
from ..workspaces import resolve_job_prompt

try:  # pragma: no cover - optional dependency in the sandbox.
    from fastapi import BackgroundTasks, FastAPI, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.templating import Jinja2Templates
except ImportError:  # pragma: no cover - keeps module importable in tests.
    BackgroundTasks = object  # type: ignore[assignment]
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

    def _coding_agent_options() -> list[dict[str, str]]:
        return orchestrator.describe_coding_agent_options()

    def _coding_agent_form_value(value: Optional[str]) -> str:
        alias = orchestrator.coding_agent_alias_for_backend(value)
        return alias or "auto"

    def _coding_agent_label(value: Optional[str]) -> Optional[str]:
        alias = _coding_agent_form_value(value)
        return {
            "auto": "Auto",
            "codex": "Codex",
            "claude": "Claude",
        }.get(alias, value)

    def _project_coding_agent_options() -> list[dict[str, str]]:
        return [dict(option) for option in _coding_agent_options()]

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

    def _short_id(value: str, *, prefix: int = 8, suffix: int = 4) -> str:
        if len(value) <= prefix + suffix + 1:
            return value
        return f"{value[:prefix]}…{value[-suffix:]}"

    def _humanize_label(value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        return str(value).replace("_", " ").strip().title()

    def _truncate_text(value: Optional[str], *, limit: int = 88) -> Optional[str]:
        if not value:
            return value
        text = str(value).strip()
        if len(text) <= limit:
            return text
        return f"{text[: limit - 1].rstrip()}…"

    def _format_timestamp(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        try:
            localized = value.astimezone()
        except ValueError:
            localized = value
        return localized.strftime("%Y-%m-%d %I:%M:%S %p")

    def _short_path(value: Optional[str], *, keep_parts: int = 3, limit: int = 48) -> Optional[str]:
        if not value:
            return value
        raw = str(value)
        home = str(Path.home())
        display = raw.replace(home, "~") if raw.startswith(home) else raw
        parts = Path(display).parts
        if len(parts) > keep_parts:
            display = f"…/{'/'.join(parts[-keep_parts:])}"
        if len(display) > limit:
            display = f"{display[: limit - 1].rstrip()}…"
        return display

    def _execution_mode_label(value: Optional[str]) -> str:
        return {
            "read_only": "read-only review",
            "safe_changes": "small safe code change",
            "coding_pass": "coding pass",
        }.get(str(value or "").strip(), "task")

    def _compact_file_label(value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        return _short_path(value, keep_parts=2, limit=34) or str(value)

    def _collect_changed_files(response_summary, artifacts) -> list[str]:
        candidates: list[str] = []
        if isinstance(response_summary, dict):
            for key in ("changed_files", "files_changed", "files_modified", "modified_files"):
                raw = response_summary.get(key)
                if isinstance(raw, list):
                    candidates.extend(str(item).strip() for item in raw if str(item).strip())
        for artifact in artifacts or []:
            metadata = getattr(artifact, "metadata", {}) or {}
            if not isinstance(metadata, dict):
                continue
            for key in ("changed_files", "files_changed", "files_modified", "modified_files"):
                raw = metadata.get(key)
                if isinstance(raw, list):
                    candidates.extend(str(item).strip() for item in raw if str(item).strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped[:12]

    def _response_summary_preview(response_summary) -> Optional[str]:
        if not isinstance(response_summary, dict):
            return None
        for key in ("summary", "message", "result", "output", "text"):
            value = response_summary.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value, limit=220)
        preview_parts: list[str] = []
        for key in ("changed_files", "files_changed", "notes", "status", "reason"):
            value = response_summary.get(key)
            if value in (None, "", [], {}):
                continue
            preview_parts.append(f"{key}={value}")
        return _truncate_text("; ".join(preview_parts), limit=220) if preview_parts else None

    def _default_change_summary(
        *,
        execution_mode: Optional[str],
        changed_files: list[str],
        response_preview: Optional[str],
        fallback: Optional[str],
    ) -> str:
        normalized_preview = str(response_preview or "").strip()
        if normalized_preview and normalized_preview.lower() not in {"done", "ok", "completed", "success"}:
            return response_preview
        if changed_files:
            touched = ", ".join(_compact_file_label(path) or path for path in changed_files[:3])
            return f"Updated {touched}."
        if execution_mode == "read_only":
            return "Completed a read-only review without modifying files."
        return fallback or "Completed the current task without a detailed change summary."

    def _change_file_items(changed_files: list[str], *, href: Optional[str] = None) -> list[dict[str, str]]:
        items = []
        for path in changed_files[:4]:
            item = {
                "path": path,
                "label": _compact_file_label(path) or path,
            }
            if href:
                item["href"] = href
            items.append(item)
        return items

    def _workspace_summary(job) -> dict:
        workspace_kind = job.metadata.get("workspace_kind") or "directory"
        primary_path = job.workspace_path or job.metadata.get("workspace_path")
        branch_name = job.metadata.get("branch_name")
        worktree_path = job.metadata.get("worktree_path")
        secondary = None
        secondary_title = None
        if branch_name:
            secondary = f"branch {branch_name}"
            secondary_title = secondary
        elif worktree_path and worktree_path != primary_path:
            secondary = _short_path(worktree_path, keep_parts=3, limit=42)
            secondary_title = worktree_path
        return {
            "kind": workspace_kind,
            "path_full": primary_path or "Workspace pending",
            "path_short": _short_path(primary_path, keep_parts=3, limit=42) or "Workspace pending",
            "secondary": secondary,
            "secondary_title": secondary_title,
        }

    def _project_defaults_summary(project) -> dict:
        badges = []
        if project.default_backend:
            badges.append(
                {
                    "label": _coding_agent_label(project.default_backend) or project.default_backend,
                    "title": "Default coding agent",
                }
            )
        if project.default_provider:
            badges.append({"label": project.default_provider, "title": "Default provider"})
        if project.default_base_branch:
            badges.append({"label": f"base {project.default_base_branch}", "title": "Default base branch"})
        if project.default_use_git_worktree:
            badges.append({"label": "worktree", "title": "Uses git worktree by default"})
        return {
            "badges": badges,
            "fallback": "Uses app defaults" if not badges else None,
        }

    def serialize_project(project) -> dict:
        defaults = _project_defaults_summary(project)
        return {
            "id": project.id,
            "name": project.name,
            "repo_path": project.repo_path,
            "repo_path_short": _short_path(project.repo_path, keep_parts=4, limit=54) or project.repo_path,
            "defaults": defaults,
            "default_backend": project.default_backend,
            "default_provider": project.default_provider,
            "default_base_branch": project.default_base_branch,
            "default_use_git_worktree": project.default_use_git_worktree,
            "autonomy_mode": project.autonomy_mode,
            "notes": project.notes,
        }

    def serialize_project_manager(snapshot) -> dict:
        recommendation = snapshot.state.latest_recommendation
        response = snapshot.state.latest_response

        def _message_status_label(decision: Optional[str]) -> Optional[str]:
            if not decision:
                return None
            return {
                "launch_followup_job": "draft ready",
                "request_manual_test": "action needed",
                "wait_for_operator": "waiting",
                "mark_complete": "stable",
                "needs_clarification": "needs input",
            }.get(decision, _humanize_label(decision))

        return {
            "state": {
                "current_phase": snapshot.state.current_phase,
                "summary": snapshot.state.summary,
                "testing_status": snapshot.state.testing_status,
                "needs_manual_testing": snapshot.state.needs_manual_testing,
                "rolling_summary": snapshot.state.rolling_summary,
                "stable_project_facts": snapshot.state.stable_project_facts,
                "workflow_state": snapshot.state.workflow_state,
                "active_autonomy_session_id": snapshot.state.active_autonomy_session_id,
                "active_job_id": snapshot.state.active_job_id,
                "auto_tasks_run_count": snapshot.state.auto_tasks_run_count,
                "project_guidance": list(snapshot.state.project_guidance),
                "last_guidance_saved_at": snapshot.state.last_guidance_saved_at.isoformat()
                if snapshot.state.last_guidance_saved_at
                else None,
                "last_job_ingested_at": snapshot.state.last_job_ingested_at.isoformat()
                if snapshot.state.last_job_ingested_at
                else None,
                "last_compacted_at": snapshot.state.last_compacted_at.isoformat()
                if snapshot.state.last_compacted_at
                else None,
            },
            "response": response.to_dict() if response else None,
            "display": dict(snapshot.state.display_snapshot),
            "recommendation": (
                {
                    "decision": recommendation.decision,
                    "reason": recommendation.reason,
                    "next_task": recommendation.next_task,
                    "manual_test_checklist": recommendation.manual_test_checklist,
                }
                if recommendation
                else None
            ),
            "recent_events": [
                {
                    "id": event.id,
                    "created_at": event.created_at.isoformat(),
                    "event_type": event.event_type,
                    "source_job_id": event.source_job_id,
                    "source_job_short_id": _short_id(event.source_job_id) if event.source_job_id else None,
                    "attempt_number": event.attempt_number,
                    "outcome_status": event.outcome_status,
                    "prompt": event.summary.get("prompt"),
                    "provider": event.summary.get("provider"),
                    "backend": event.summary.get("backend"),
                    "task_type": event.summary.get("task_type"),
                    "response_summary_preview": event.summary.get("response_summary_preview"),
                    "error_summary": event.summary.get("error_summary"),
                    "error_reason": event.summary.get("error_reason"),
                    "manual_testing_needed": bool(event.summary.get("manual_testing_needed")),
                    "testing_notes": event.summary.get("testing_notes"),
                    "changed_files": event.summary.get("changed_files") or [],
                    "stream_highlights": event.summary.get("stream_highlights") or [],
                }
                for event in snapshot.recent_events
            ],
            "recent_messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "content_preview": _truncate_text(message.content, limit=180) or "",
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "message_type": message.metadata.get("message_type"),
                    "urgency": message.metadata.get("urgency"),
                    "backend_preference": message.metadata.get("backend_preference"),
                    "coding_agent_label": _coding_agent_label(message.metadata.get("backend_preference")),
                    "execution_mode": message.metadata.get("execution_mode"),
                    "decision": (
                        message.metadata.get("decision")
                        or ((message.metadata.get("response") or {}).get("decision") if isinstance(message.metadata.get("response"), dict) else None)
                    ),
                    "decision_label": _message_status_label(
                        message.metadata.get("decision")
                        or ((message.metadata.get("response") or {}).get("decision") if isinstance(message.metadata.get("response"), dict) else None)
                    ),
                    "phase": (
                        message.metadata.get("phase")
                        or ((message.metadata.get("response") or {}).get("phase") if isinstance(message.metadata.get("response"), dict) else None)
                    ),
                    "phase_label": _humanize_label(
                        message.metadata.get("phase")
                        or ((message.metadata.get("response") or {}).get("phase") if isinstance(message.metadata.get("response"), dict) else None)
                    ),
                    "confidence": (
                        (message.metadata.get("response") or {}).get("confidence")
                        if isinstance(message.metadata.get("response"), dict)
                        else None
                    ),
                }
                for message in snapshot.recent_messages
            ],
            "recent_feedback": [
                {
                    "id": feedback.id,
                    "outcome": feedback.outcome,
                    "notes": feedback.notes,
                    "notes_preview": _truncate_text(feedback.notes, limit=180) or "",
                    "severity": feedback.severity,
                    "area": feedback.area,
                    "screenshot_reference": feedback.screenshot_reference,
                    "requires_followup": feedback.requires_followup,
                    "created_at": feedback.created_at.isoformat() if feedback.created_at else None,
                }
                for feedback in snapshot.recent_feedback
            ],
        }

    def serialize_project_manager_session(session) -> dict:
        badges = [
            {"label": session.autonomy_mode.title(), "class_name": ""},
        ]
        state_badges: list[dict[str, str]] = []
        if session.waiting_on_worker:
            state_badges.append({"label": "waiting on worker", "class_name": "status-pill status-queued"})
        elif session.active_managed_job_status == JobStatus.RUNNING.value:
            state_badges.append({"label": "running", "class_name": "status-pill status-running"})
        elif session.workflow_state == "awaiting_continue_decision":
            state_badges.append({"label": "waiting on operator", "class_name": "status-pill status-waiting_retry"})
        elif session.workflow_state == "blocked_on_manual_test":
            state_badges.append({"label": "action needed", "class_name": "status-pill status-waiting_retry"})
        elif session.active_managed_job_status == JobStatus.WAITING_RETRY.value:
            state_badges.append({"label": "waiting to retry", "class_name": "status-pill status-waiting_retry"})
        elif session.active_managed_job_status == JobStatus.FAILED.value:
            state_badges.append({"label": "failed", "class_name": "status-pill status-failed"})
        elif session.active_managed_job_status == JobStatus.COMPLETED.value:
            state_badges.append({"label": "completed", "class_name": "status-pill status-completed"})
        elif session.workflow_state == "idle":
            state_badges.append({"label": "idle", "class_name": ""})
        elif session.workflow_state == "awaiting_confirmation":
            state_badges.append({"label": "waiting on approval", "class_name": "status-pill status-waiting_retry"})
        else:
            state_badges.append({"label": _humanize_label(session.workflow_state) or "session", "class_name": ""})
        badges.extend(state_badges)

        return {
            "autonomy_mode": session.autonomy_mode,
            "autonomy_mode_label": session.autonomy_mode.title(),
            "workflow_state": session.workflow_state,
            "workflow_label": _humanize_label(session.workflow_state) or session.workflow_state,
            "headline": session.session_headline,
            "detail": session.session_detail,
            "auto_task_count": session.auto_task_count,
            "waiting_on_operator": session.waiting_on_operator,
            "waiting_on_worker": session.waiting_on_worker,
            "needs_manual_testing": session.needs_manual_testing,
            "active_job_id": session.active_managed_job_id,
            "active_job_short_id": _short_id(session.active_managed_job_id) if session.active_managed_job_id else None,
            "active_job_status": session.active_managed_job_status,
            "active_job_status_label": _humanize_label(session.active_managed_job_status),
            "active_job_headline": session.active_managed_job_headline,
            "last_activity_at": session.last_manager_activity_at.isoformat() if session.last_manager_activity_at else None,
            "last_activity_label": _format_timestamp(session.last_manager_activity_at),
            "last_completed_job_id": session.last_completed_managed_job_id,
            "last_completed_job_short_id": _short_id(session.last_completed_managed_job_id) if session.last_completed_managed_job_id else None,
            "last_completed_job_status": session.last_completed_managed_job_status,
            "last_failed_job_id": session.last_failed_managed_job_id,
            "last_failed_job_short_id": _short_id(session.last_failed_managed_job_id) if session.last_failed_managed_job_id else None,
            "last_failed_job_status": session.last_failed_managed_job_status,
            "current_recommendation": _truncate_text(session.current_recommendation, limit=180) or None,
            "next_prompt": _truncate_text(session.next_prompt, limit=160) or None,
            "status_badges": badges[:4],
        }

    def _project_workspace_refresh(session: dict) -> dict:
        workflow_state = str(session.get("workflow_state") or "")
        active_status = str(session.get("active_job_status") or "")
        if workflow_state == "running_current_task":
            if active_status in {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.WAITING_RETRY.value}:
                return {
                    "interval_seconds": 4,
                    "mode": "active",
                    "label": "Live · 4s",
                }
            return {
                "interval_seconds": 6,
                "mode": "settling",
                "label": "Settling · 6s",
            }
        if workflow_state in {"awaiting_continue_decision", "blocked_on_manual_test"}:
            return {
                "interval_seconds": 6,
                "mode": "paused",
                "label": "Paused · 6s",
            }
        if workflow_state == "awaiting_confirmation":
            return {
                "interval_seconds": 15,
                "mode": "idle",
                "label": "Idle · 15s",
            }
        return {
            "interval_seconds": 20,
            "mode": "idle",
            "label": "Idle · 20s",
        }

    def _managed_focus_job(session: dict, jobs: list[dict]) -> Optional[dict]:
        jobs_by_id = {job["id"]: job for job in jobs}
        focus_job_id = session.get("active_job_id")
        if not focus_job_id and session.get("workflow_state") in {"awaiting_continue_decision", "blocked_on_manual_test"}:
            focus_job_id = session.get("last_failed_job_id") or session.get("last_completed_job_id")
        if not focus_job_id:
            focus_job_id = session.get("last_failed_job_id") or session.get("last_completed_job_id")
        return jobs_by_id.get(focus_job_id) if focus_job_id else None

    def _project_manager_live_surface(project_manager: dict, jobs: list[dict]) -> dict:
        session = project_manager["session"]
        response = project_manager.get("response") or {}
        display = project_manager.get("display") or {}
        session_ledger = dict(display.get("session_ledger") or {})
        focus_job = _managed_focus_job(session, jobs)
        latest_operator_message = next(
            (message for message in project_manager.get("recent_messages", []) if message.get("role") == "operator"),
            None,
        )
        selected_backend = (
            (focus_job or {}).get("backend")
            or ((response.get("draft_task") or {}).get("backend"))
            or (latest_operator_message or {}).get("backend_preference")
            or "auto"
        )
        coding_agent_label = _coding_agent_label(selected_backend)
        workflow_state = session.get("workflow_state")
        draft_task = response.get("draft_task") or {}
        recent_events = project_manager.get("recent_events", [])
        latest_event = (
            next(
                (event for event in recent_events if focus_job and event.get("source_job_id") == focus_job.get("id")),
                None,
            )
            or (recent_events[0] if recent_events else None)
        )
        changed_files = list((latest_event or {}).get("changed_files") or [])
        execution_mode = (
            str((focus_job or {}).get("metadata", {}).get("execution_mode") or "").strip()
            or str(draft_task.get("execution_mode") or "").strip()
            or str((latest_operator_message or {}).get("execution_mode") or "").strip()
        )
        manual_testing_needed = bool(
            session.get("needs_manual_testing")
            or response.get("needs_manual_testing")
            or (latest_event or {}).get("manual_testing_needed")
        )

        def _draft_launch_label(mode: str, *, continue_step: bool = False) -> str:
            normalized = str(mode or "").strip()
            if normalized == "read_only":
                return "Run follow-up review" if continue_step else "Run review"
            if normalized == "safe_changes":
                return "Start next improvement"
            return "Run follow-up task"

        def _primary_action(mode: str) -> dict:
            if workflow_state == "blocked_on_manual_test":
                return {
                    "kind": "feedback",
                    "label": "Record result",
                    "detail": "Provide your test result to continue.",
                }
            if workflow_state == "awaiting_continue_decision" and draft_task:
                return {
                    "kind": "continue",
                    "label": "Continue",
                    "detail": "",
                }
            if workflow_state != "running_current_task" and response.get("decision") == "launch_followup_job" and draft_task:
                return {
                    "kind": "launch_draft",
                    "label": "Run",
                    "detail": "",
                }
            if focus_job and focus_job.get("status") == JobStatus.WAITING_RETRY.value and focus_job.get("actions", {}).get("retry"):
                return {
                    "kind": "retry_job",
                    "label": "Retry",
                    "detail": "",
                }
            return {
                "kind": None,
                "label": None,
                "detail": "",
            }

        if workflow_state == "running_current_task":
            if session.get("waiting_on_worker"):
                manager_headline = "Task queued"
                manager_detail = "Waiting for a worker to pick it up."
                manager_action = ""
            elif session.get("active_job_status") == JobStatus.RUNNING.value:
                manager_headline = "Working"
                manager_detail = (
                    (focus_job or {}).get("latest_progress_preview")
                    or "Agent is executing the current step."
                )
                manager_action = ""
            elif session.get("active_job_status") == JobStatus.WAITING_RETRY.value:
                manager_headline = "Retrying"
                manager_detail = "Hit a temporary issue. Retrying shortly."
                manager_action = ""
            else:
                manager_headline = display.get("reply_lead") or "Processing"
                manager_detail = session.get("detail") or ""
                manager_action = ""
        elif workflow_state == "awaiting_continue_decision":
            manager_headline = "Step complete. Ready for next."
            manager_detail = response.get("reason") or display.get("reply_why") or session.get("detail") or ""
            manager_action = "Continue?"
        elif workflow_state == "blocked_on_manual_test":
            manager_headline = "Action needed"
            manager_detail = response.get("reason") or session.get("detail") or ""
            manager_action = "Record your result below to continue."
        elif workflow_state == "awaiting_confirmation":
            manager_headline = display.get("reply_lead") or "Ready to run"
            manager_detail = session.get("detail") or ""
            manager_action = display.get("reply_question") or ""
        else:
            manager_headline = display.get("reply_lead") or session.get("headline") or "Idle"
            manager_detail = display.get("reply_why") or session.get("detail") or ""
            manager_action = ""

        if workflow_state == "blocked_on_manual_test":
            next_need_label = "Action needed"
            next_need_detail = "Record your test result to unblock."
        elif workflow_state == "awaiting_continue_decision":
            next_need_label = "Ready"
            next_need_detail = response.get("reason") or "Next step is prepared."
        elif workflow_state == "awaiting_confirmation":
            next_need_label = "Awaiting approval"
            next_need_detail = ""
        elif workflow_state == "running_current_task" and session.get("waiting_on_worker"):
            next_need_label = "Queued"
            next_need_detail = "Waiting for worker."
        elif workflow_state == "running_current_task":
            next_need_label = "Running"
            next_need_detail = ""
        elif response.get("decision") == "needs_clarification":
            next_need_label = "Needs input"
            next_need_detail = response.get("reason") or ""
        elif response.get("decision") == "mark_complete":
            next_need_label = "Complete"
            next_need_detail = response.get("reason") or ""
        else:
            next_need_label = "Idle"
            next_need_detail = ""

        chat_turn = {
            "operator": _truncate_text(
                (latest_operator_message or {}).get("content") or (latest_operator_message or {}).get("content_preview"),
                limit=180,
            ),
            "manager": _truncate_text(display.get("conversation_reply"), limit=220),
        }
        ledger = {
            "objective": session_ledger.get("current_objective"),
            "objective_reason": session_ledger.get("objective_reason"),
            "changes_this_session": list(session_ledger.get("session_change_log") or [])[:3],
            "intended_outcomes": list(session_ledger.get("intended_outcomes") or [])[:3],
            "latest_result_summary": session_ledger.get("latest_result_summary"),
            "current_hypothesis": session_ledger.get("current_hypothesis"),
            "next_step": session_ledger.get("next_intended_step"),
            "stop_reason": session_ledger.get("stop_reason"),
        }

        primary_action = _primary_action(execution_mode)
        change_action_href = f"/jobs/{focus_job['id']}#change-summary-section" if focus_job else None
        change_summary_text = _default_change_summary(
            execution_mode=execution_mode or None,
            changed_files=changed_files,
            response_preview=(latest_event or {}).get("response_summary_preview"),
            fallback=(
                "The drafted task has not started yet."
                if draft_task and not focus_job
                else "I’ll summarize the change here when the current task finishes."
            ),
        )
        changed_file_items = _change_file_items(changed_files, href=change_action_href)

        if focus_job:
            status = focus_job.get("status")
            if status == JobStatus.QUEUED.value:
                queue_reason = str((focus_job.get("metadata") or {}).get("project_manager_queue_reason") or "").strip()
                agent_headline = "Task queued"
                agent_detail = queue_reason or "Waiting for a worker to pick it up."
            elif status == JobStatus.RUNNING.value:
                agent_headline = "Task running"
                agent_detail = focus_job.get("latest_progress_preview") or "The coding agent is actively working."
            elif status == JobStatus.COMPLETED.value:
                agent_headline = "Task completed"
                agent_detail = focus_job.get("latest_progress_preview") or "The coding agent finished the current step."
            elif status == JobStatus.FAILED.value:
                agent_headline = "Task failed"
                agent_detail = focus_job.get("last_error_preview") or "The coding agent hit a blocking failure."
            elif status == JobStatus.WAITING_RETRY.value:
                agent_headline = "Task waiting to retry"
                agent_detail = focus_job.get("lifecycle", {}).get("headline") or "The coding agent is waiting for the next retry."
            else:
                agent_headline = focus_job.get("lifecycle", {}).get("headline") or "Task state updated"
                agent_detail = focus_job.get("latest_progress_preview") or session.get("detail")
        elif response.get("draft_task"):
            agent_headline = "Ready for handoff"
            agent_detail = "The manager has a scoped task draft ready, but nothing has been launched yet."
        elif workflow_state == "blocked_on_manual_test":
            agent_headline = "No new task yet"
            agent_detail = "The next task is paused until manual testing is complete."
        else:
            agent_headline = "No active task"
            agent_detail = "The coding agent is idle until the manager launches work."

        if focus_job:
            status = focus_job.get("status")
            if status == JobStatus.QUEUED.value:
                if execution_mode == "read_only":
                    outcome_headline = f"{coding_agent_label} is queued to start a read-only review."
                else:
                    outcome_headline = f"{coding_agent_label} is queued to start the current task."
                outcome_detail = agent_detail
            elif status == JobStatus.RUNNING.value:
                if execution_mode == "read_only":
                    outcome_headline = f"{coding_agent_label} is running a read-only review."
                elif execution_mode == "safe_changes":
                    outcome_headline = f"{coding_agent_label} is working on a small safe code change."
                elif execution_mode == "coding_pass":
                    outcome_headline = f"{coding_agent_label} is running the current coding pass."
                else:
                    outcome_headline = f"{coding_agent_label} is working on the current task."
                outcome_detail = agent_detail
            elif status == JobStatus.COMPLETED.value:
                if changed_files:
                    outcome_headline = f"{coding_agent_label} completed a code change."
                elif execution_mode == "read_only":
                    outcome_headline = f"{coding_agent_label} completed a read-only review."
                else:
                    outcome_headline = f"{coding_agent_label} completed the current task."
                outcome_detail = (
                    (latest_event or {}).get("response_summary_preview")
                    or focus_job.get("latest_progress_preview")
                    or "The current step finished and the Project Manager reviewed the result."
                )
            elif status == JobStatus.FAILED.value:
                outcome_headline = f"{coding_agent_label} failed on the current task."
                outcome_detail = agent_detail
            elif status == JobStatus.WAITING_RETRY.value:
                outcome_headline = f"{coding_agent_label} is waiting to retry the current task."
                outcome_detail = agent_detail
            else:
                outcome_headline = agent_headline
                outcome_detail = agent_detail
        elif draft_task:
            if execution_mode == "read_only":
                outcome_headline = "The Project Manager prepared a read-only review."
            elif execution_mode == "safe_changes":
                outcome_headline = f"The Project Manager prepared a small safe task for {coding_agent_label}."
            elif execution_mode == "coding_pass":
                outcome_headline = f"The Project Manager prepared the next coding pass for {coding_agent_label}."
            else:
                outcome_headline = f"The Project Manager prepared the next task for {coding_agent_label}."
            outcome_detail = "Nothing has started yet."
        elif workflow_state == "blocked_on_manual_test":
            outcome_headline = "No new task is running."
            outcome_detail = "The next step is paused until manual testing is complete."
        else:
            outcome_headline = "No task is in progress."
            outcome_detail = "The coding agent will stay idle until the manager launches work."

        if changed_files:
            change_label = f"{len(changed_files)} file{'s' if len(changed_files) != 1 else ''} were changed."
            change_detail = ", ".join(item["label"] for item in changed_file_items)
            change_action_label = "View changed files"
        elif focus_job and focus_job.get("status") == JobStatus.COMPLETED.value:
            change_label = "No files were modified."
            change_detail = (
                "This finished as a read-only review."
                if execution_mode == "read_only"
                else "The task completed without any recorded file changes."
            )
            change_action_label = None
        elif focus_job and focus_job.get("status") == JobStatus.RUNNING.value:
            change_label = "Changes are still in progress."
            change_detail = "I’ll summarize touched files here once the task finishes."
            change_action_label = None
        elif draft_task:
            change_label = "No files were modified yet."
            change_detail = "The drafted task has not started yet."
            change_action_label = None
        else:
            change_label = "No files were modified."
            change_detail = "There is no active task changing code right now."
            change_action_label = None

        if manual_testing_needed:
            manual_test_checklist = list(response.get("manual_test_checklist") or [])
            if not manual_test_checklist and (latest_event or {}).get("testing_notes"):
                manual_test_checklist = [str((latest_event or {}).get("testing_notes"))]
            test_next = {
                "headline": "Action needed",
                "detail": (latest_event or {}).get("testing_notes") or "Verify the changes, then record your result.",
                "checklist_items": manual_test_checklist[:4],
                "needs_manual_testing": True,
            }
        elif focus_job and focus_job.get("status") == JobStatus.COMPLETED.value:
            test_next = {
                "headline": "",
                "detail": "",
                "checklist_items": [],
                "needs_manual_testing": False,
            }
        else:
            test_next = {
                "headline": "",
                "detail": "",
                "checklist_items": [],
                "needs_manual_testing": False,
            }

        agent_preview = None
        if focus_job:
            agent_preview = (
                focus_job.get("latest_progress_preview")
                or focus_job.get("last_error_preview")
                or focus_job.get("lifecycle", {}).get("helper_text")
            )
        elif response.get("draft_task"):
            agent_preview = _truncate_text(response["draft_task"].get("prompt"), limit=160)

        timeline = []
        if response:
            timeline.append(
                {
                    "label": "Manager decided next step",
                    "text": display.get("recommendation_title") or response.get("decision") or session.get("headline"),
                }
            )
        if focus_job:
            timeline.append(
                {
                    "label": f"Task sent to {coding_agent_label}",
                    "text": f"{focus_job.get('short_id')} · {focus_job.get('backend')}",
                }
            )
            timeline.append(
                {
                    "label": agent_headline,
                    "text": agent_detail,
                }
            )
        elif response.get("draft_task"):
            timeline.append(
                {
                    "label": f"{coding_agent_label} waiting for handoff",
                    "text": primary_action.get("detail") or "The drafted task is ready whenever you want to start it.",
                }
            )
        if workflow_state == "awaiting_continue_decision":
            timeline.append(
                {
                    "label": "Ready for next step",
                    "text": "Approve to continue.",
                }
            )
        elif workflow_state == "blocked_on_manual_test":
            timeline.append(
                {
                    "label": "Action needed",
                    "text": "Record your test result to unblock.",
                }
            )
        elif workflow_state == "running_current_task" and session.get("waiting_on_worker"):
            timeline.append(
                {
                    "label": "Queued",
                    "text": "Waiting for worker.",
                }
            )

        return {
            "manager": {
                "headline": manager_headline,
                "detail": manager_detail,
                "action": manager_action,
                "chat_turn": chat_turn,
                "session_ledger": ledger,
                "next_need": {
                    "label": next_need_label,
                    "detail": next_need_detail,
                },
                "primary_action": primary_action,
            },
            "coding_agent": {
                "label": coding_agent_label,
                "backend": selected_backend,
                "headline": agent_headline,
                "detail": agent_detail,
                "preview": agent_preview,
                "active_job": focus_job,
                "outcome": {
                    "headline": outcome_headline,
                    "detail": outcome_detail,
                },
                "change_summary": {
                    "headline": "What changed",
                    "detail": change_summary_text,
                },
                "changes": {
                    "label": "Files changed",
                    "count_label": change_label,
                    "detail": change_detail,
                    "files": changed_file_items,
                    "has_changes": bool(changed_files),
                    "action_label": change_action_label,
                    "action_href": change_action_href,
                },
                "test_next": test_next,
            },
            "timeline": timeline[:4],
        }

    def _build_project_workspace_view(project_id: str) -> dict:
        project = orchestrator.get_saved_project(project_id)
        jobs = [serialize_job(job) for job in orchestrator.list_project_jobs(project_id, limit=20)]
        project_manager = serialize_project_manager(orchestrator.get_project_manager_snapshot(project_id))
        project_manager["session"] = serialize_project_manager_session(
            orchestrator.describe_project_manager_session(project_id)
        )
        project_manager["workspace"] = _project_manager_live_surface(project_manager, jobs)
        project_manager["refresh"] = _project_workspace_refresh(project_manager["session"])
        return {
            "project": project,
            "project_display": serialize_project(project),
            "jobs": jobs,
            "project_manager": project_manager,
        }

    def _manager_run_now_worker_id(project_id: str, job_id: str) -> str:
        return f"web-manager-{project_id[:8]}-{job_id[:8]}"

    def _maybe_start_manager_job_now(
        project_id: str,
        *,
        background_tasks: BackgroundTasks,
    ) -> tuple[Optional[dict], Optional[str]]:
        session = orchestrator.describe_project_manager_session(project_id)
        if session.workflow_state != "running_current_task" or not session.active_managed_job_id:
            return None, None
        start_result = orchestrator.try_start_manager_job_now(
            session.active_managed_job_id,
            worker_id=_manager_run_now_worker_id(project_id, session.active_managed_job_id),
            launch_context="browser_project_page",
        )
        if start_result.claimed_job is not None:
            background_tasks.add_task(
                orchestrator.process_claimed_job,
                start_result.claimed_job,
                _manager_run_now_worker_id(project_id, session.active_managed_job_id),
            )
        refreshed = serialize_project_manager_session(orchestrator.describe_project_manager_session(project_id))
        return refreshed, start_result.reason

    def _job_change_summary(details, *, project_manager_snapshot=None) -> dict:
        ordered_runs = sorted(details.runs, key=lambda run: run.started_at, reverse=True)
        latest_run = ordered_runs[0] if ordered_runs else None
        response_summary = latest_run.response_summary if latest_run else {}
        project_event_summary = None
        if project_manager_snapshot is not None:
            project_event_summary = next(
                (
                    event.summary
                    for event in project_manager_snapshot.recent_events
                    if event.source_job_id == details.job.id
                ),
                None,
            )
        changed_files = (
            list((project_event_summary or {}).get("changed_files") or [])
            or _collect_changed_files(response_summary, details.artifacts)
        )
        summary_text = _default_change_summary(
            execution_mode=details.job.metadata.get("execution_mode"),
            changed_files=changed_files,
            response_preview=(project_event_summary or {}).get("response_summary_preview")
            or _response_summary_preview(response_summary),
            fallback=details.latest_progress_message or details.state.compact_summary or details.job.last_error_message,
        )
        manual_testing_needed = bool((project_event_summary or {}).get("manual_testing_needed"))
        manual_test_checklist: list[str] = []
        if (
            manual_testing_needed
            and project_manager_snapshot is not None
            and project_manager_snapshot.state.latest_response is not None
        ):
            manual_test_checklist = list(project_manager_snapshot.state.latest_response.manual_test_checklist or [])[:4]
        testing_notes = (
            (project_event_summary or {}).get("testing_notes")
            or ("No manual test is requested right now." if details.job.status == JobStatus.COMPLETED else "")
        )
        return {
            "summary": summary_text,
            "has_changes": bool(changed_files),
            "files_count": len(changed_files),
            "files_count_label": (
                f"{len(changed_files)} file{'s' if len(changed_files) != 1 else ''} changed"
                if changed_files
                else "No files were modified."
            ),
            "files": _change_file_items(changed_files),
            "manual_testing_needed": manual_testing_needed,
            "manual_test_checklist": manual_test_checklist,
            "testing_notes": testing_notes,
        }

    def _job_detail_context(request: Request, job_id: str) -> dict:
        details = orchestrator.inspect(job_id)
        lifecycle = orchestrator.describe_job_lifecycle(details.job)
        project_link = None
        project_manager_session = None
        project_manager_can_continue = False
        project_manager_snapshot = None
        project_id = details.job.metadata.get("project_id")
        if project_id:
            try:
                project = orchestrator.get_saved_project(project_id)
                project_link = {"id": project.id, "name": project.name}
                project_manager_session = serialize_project_manager_session(
                    orchestrator.describe_project_manager_session(project_id)
                )
                snapshot = orchestrator.get_project_manager_snapshot(project_id)
                project_manager_snapshot = snapshot
                related_job_ids = {
                    project_manager_session.get("active_job_id"),
                    project_manager_session.get("last_completed_job_id"),
                    project_manager_session.get("last_failed_job_id"),
                }
                project_manager_can_continue = bool(
                    details.job.metadata.get("project_manager_auto_launched")
                    and snapshot.state.workflow_state == "awaiting_continue_decision"
                    and snapshot.state.latest_response
                    and snapshot.state.latest_response.draft_task
                    and details.job.id in {item for item in related_job_ids if item}
                )
            except KeyError:
                project_link = None
                project_manager_session = None
        return {
            "request": request,
            "job": details.job,
            "job_serialized": serialize_job(details.job),
            "job_actions": orchestrator.describe_job_actions(details.job),
            "job_lifecycle": lifecycle,
            "state": details.state,
            "runs": details.runs,
            "events": details.events,
            "stream_events": details.stream_events,
            "latest_stream_event": details.latest_stream_event,
            "latest_phase": details.latest_phase,
            "latest_progress_message": details.latest_progress_message,
            "artifacts": details.artifacts,
            "integration": serialize_integration(details.integration_summary, details.job.backend),
            "job_change_summary": _job_change_summary(details, project_manager_snapshot=project_manager_snapshot),
            "project_link": project_link,
            "project_manager_session": project_manager_session,
            "project_manager_can_continue": project_manager_can_continue,
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
            "execution_mode": "",
            "_manager_context": None,
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
            "manager_context": values.get("_manager_context"),
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
            "default_backend": _coding_agent_form_value(orchestrator.config.default_backend),
            "default_provider": "",
            "default_base_branch": "",
            "default_use_git_worktree": False,
            "autonomy_mode": "minimal",
            "notes": "",
            "initial_context": "",
        }
        if form_values:
            values.update(form_values)
        return {
            "request": request,
            "form_values": values,
            "available_backends": _available_backends(),
            "coding_agent_options": _project_coding_agent_options(),
            "available_providers": _available_providers(),
            "error": error,
            "project": project,
            "page_title": "Edit Project" if project else "New Project",
            "page_subtitle": (
                "Update project settings."
                if project
                else "Set up a new project workspace."
            ),
            "form_action": f"/projects/{project.id}/edit" if project else "/projects",
            "submit_label": "Save Changes" if project else "Save Project",
            "cancel_href": f"/projects/{project.id}" if project else "/projects",
        }

    def _default_manager_form_values() -> dict:
        return {
            "message": "",
            "urgency": "normal",
            "coding_agent": "auto",
            "execution_mode": "",
        }

    def _default_feedback_form_values() -> dict:
        return {
            "outcome": "mixed",
            "notes": "",
            "severity": "normal",
            "area": "",
            "screenshot_reference": "",
            "requires_followup": False,
        }

    def _project_manager_composer_values(
        project,
        project_manager: dict,
        *,
        followup: bool = False,
        form_values: Optional[dict] = None,
    ) -> dict:
        values = _default_manager_form_values()
        response = project_manager.get("response") or {}
        if followup:
            questions = response.get("followup_questions") or []
            if questions:
                values["message"] = "\n".join(
                    [
                        f"Continue the current AI Telescreen project-manager discussion for {project.name}.",
                        "Please help with these follow-up points:",
                        *[f"- {question}" for question in questions],
                    ]
                )
            else:
                values["message"] = (
                    f"Continue the current AI Telescreen project-manager discussion for {project.name} "
                    f"and refine the latest recommendation: {response.get('reason') or 'What should happen next?'}"
                )
        draft_task = response.get("draft_task") or {}
        if draft_task.get("backend"):
            values["coding_agent"] = _coding_agent_form_value(draft_task["backend"])
        if form_values:
            values.update(form_values)
        return values

    def _coerce_checkbox(value) -> bool:
        return str(value).lower() in {"1", "true", "on", "yes"}

    def _redirect_target(request: Request, default: str) -> str:
        return request.query_params.get("next") or default

    def _redirect_with_feedback(
        request: Request,
        default: str,
        *,
        notice: Optional[str] = None,
        error: Optional[str] = None,
    ) -> RedirectResponse:
        target = _redirect_target(request, default)
        split = urlsplit(target)
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        if notice:
            query["notice"] = notice
        if error:
            query["error"] = error
        url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))
        return RedirectResponse(url=url, status_code=303)

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

    def _project_manager_followup_prompt(project, response: dict) -> str:
        questions = response.get("followup_questions") or []
        if not questions:
            questions = [response.get("reason") or "What should happen next for this project?"]
        lines = [f"Review the current AI Telescreen project context for {project.name} and answer these follow-up questions:"]
        lines.extend(f"- {question}" for question in questions)
        return "\n".join(lines)

    def _job_form_values_from_project_manager(project_id: str, *, followup: bool = False) -> dict:
        project = orchestrator.get_saved_project(project_id)
        snapshot = orchestrator.get_project_manager_snapshot(project_id)
        response = snapshot.state.latest_response
        if response is None:
            raise ValueError("This project does not have a saved Project Manager response yet.")
        values = _job_form_values_from_project(project_id)
        response_payload = response.to_dict()
        if followup:
            values.update(
                {
                    "prompt": _project_manager_followup_prompt(project, response_payload),
                    "execution_mode": "",
                    "_manager_context": {
                        "mode": "followup",
                        "decision": response.decision,
                        "reason": response.reason,
                        "questions": response.followup_questions,
                    },
                }
            )
            return values
        draft_task = response.draft_task
        if draft_task is None:
            raise ValueError("This project does not currently have a Project Manager draft task.")
        values.update(
            {
                "prompt": draft_task.prompt,
                "backend": draft_task.backend,
                "provider": draft_task.provider or "",
                "task_type": draft_task.task_type,
                "priority": str(draft_task.priority),
                "use_git_worktree": draft_task.use_git_worktree,
                "base_branch": draft_task.base_branch or "",
                "execution_mode": draft_task.execution_mode or "",
                "_manager_context": {
                    "mode": "draft",
                    "decision": response.decision,
                    "reason": response.reason,
                    "execution_mode": draft_task.execution_mode,
                    "rationale_bullets": draft_task.rationale_bullets,
                },
            }
        )
        return values

    def _project_detail_context(
        request: Request,
        project_id: str,
        *,
        manager_form_values: Optional[dict] = None,
        manager_error: Optional[str] = None,
        manager_followup: bool = False,
        feedback_form_values: Optional[dict] = None,
        feedback_error: Optional[str] = None,
    ) -> dict:
        workspace_view = _build_project_workspace_view(project_id)
        project = workspace_view["project"]
        integration_summary = orchestrator.get_project_integration_summary(project_id)
        project_manager = workspace_view["project_manager"]
        return {
            "request": request,
            "project": project,
            "project_display": workspace_view["project_display"],
            "jobs": workspace_view["jobs"],
            "integration": serialize_project_integration(integration_summary),
            "project_manager": project_manager,
            "manager_form_values": _project_manager_composer_values(
                project,
                project_manager,
                followup=manager_followup,
                form_values=manager_form_values,
            ),
            "manager_error": manager_error,
            "feedback_form_values": {**_default_feedback_form_values(), **(feedback_form_values or {})},
            "feedback_error": feedback_error,
            "coding_agent_options": _coding_agent_options(),
        }

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
            "jobs": [serialize_job(job) for job in jobs],
            "projects": [serialize_project(project) for project in orchestrator.list_saved_projects()],
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
        lifecycle = orchestrator.describe_job_lifecycle(job)
        workspace = _workspace_summary(job)
        error_preview = _truncate_text(job.last_error_message, limit=72)
        latest_progress_preview = _truncate_text(latest_progress, limit=72)
        activity_source = latest_stream_event.timestamp if latest_stream_event else job.updated_at
        return {
            "id": job.id,
            "short_id": _short_id(job.id),
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
            "latest_progress_preview": latest_progress_preview,
            "last_activity_at": activity_source.isoformat(),
            "last_activity_label": _format_timestamp(activity_source),
            "integration": {
                "status": integration_snapshot.get("status", "not_scanned"),
                "labels": integration_snapshot.get("labels", ["not scanned"]),
                "capability_names": integration_snapshot.get("capability_names", []),
                "capability_count": integration_snapshot.get("capability_count", 0),
                "config_paths": integration_snapshot.get("config_paths", []),
            },
            "workspace": workspace,
            "metadata": job.metadata,
            "actions": orchestrator.describe_job_actions(job),
            "lifecycle": {
                "headline": lifecycle.headline,
                "helper_text": lifecycle.helper_text,
            },
            "last_error_preview": error_preview,
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
    async def new_job(
        request: Request,
        project_id: Optional[str] = None,
        from_job: Optional[str] = None,
        manager_draft: Optional[bool] = None,
        manager_followup: Optional[bool] = None,
    ):
        form_values = _default_job_form_values()
        try:
            if project_id:
                form_values.update(_job_form_values_from_project(project_id))
                if manager_draft:
                    form_values.update(_job_form_values_from_project_manager(project_id))
                if manager_followup:
                    form_values.update(_job_form_values_from_project_manager(project_id, followup=True))
            if from_job:
                form_values.update(_job_form_values_from_job(from_job))
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="job_new.html",
                context=_job_form_context(request, form_values=form_values, error=str(exc)),
                status_code=400,
            )
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
            "execution_mode": str(form.get("execution_mode") or ""),
        }
        try:
            prompt = form_values["prompt"].strip()
            if not prompt:
                raise ValueError("Prompt is required.")
            priority = int(form_values["priority"] or 0)
            max_attempts = int(form_values["max_attempts"] or 5)
            provider = form_values["provider"] or None
            project_id = form_values["project_id"] or None
            extra_metadata = {
                key: value
                for key, value in {
                    "execution_mode": form_values["execution_mode"] or None,
                }.items()
                if value not in (None, "")
            }
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
                    extra_metadata=extra_metadata,
                )
            else:
                metadata = {
                    key: value
                    for key, value in {
                        "repo_path": form_values["repo_path"] or None,
                        "use_git_worktree": form_values["use_git_worktree"],
                        "base_branch": form_values["base_branch"] or None,
                        "execution_mode": form_values["execution_mode"] or None,
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
            context={"request": request, "projects": [serialize_project(project) for project in orchestrator.list_saved_projects()]},
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
        default_backend_input = str(form.get("default_backend") or "auto")
        form_values = {
            "name": str(form.get("name") or ""),
            "repo_path": str(form.get("repo_path") or ""),
            "default_backend": default_backend_input,
            "default_provider": str(form.get("default_provider") or ""),
            "default_base_branch": str(form.get("default_base_branch") or ""),
            "default_use_git_worktree": _coerce_checkbox(form.get("default_use_git_worktree")),
            "autonomy_mode": str(form.get("autonomy_mode") or "minimal"),
            "notes": str(form.get("notes") or ""),
            "initial_context": str(form.get("initial_context") or ""),
        }
        try:
            if not form_values["name"].strip():
                raise ValueError("Project name is required.")
            if not form_values["repo_path"].strip():
                raise ValueError("Repository path is required.")
            resolved_default_backend = orchestrator.resolve_coding_agent_preference(
                form_values["default_backend"],
                default_backend=orchestrator.config.default_backend,
            )
            project = orchestrator.create_saved_project(
                name=form_values["name"].strip(),
                repo_path=form_values["repo_path"].strip(),
                default_backend=resolved_default_backend or None,
                default_provider=form_values["default_provider"] or None,
                default_base_branch=form_values["default_base_branch"] or None,
                default_use_git_worktree=form_values["default_use_git_worktree"],
                notes=form_values["notes"].strip() or None,
                autonomy_mode=form_values["autonomy_mode"] or "minimal",
                initial_context=form_values["initial_context"].strip() or None,
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
                    "default_backend": _coding_agent_form_value(project.default_backend),
                    "default_provider": project.default_provider or "",
                    "default_base_branch": project.default_base_branch or "",
                    "default_use_git_worktree": project.default_use_git_worktree,
                    "autonomy_mode": project.autonomy_mode,
                    "notes": project.notes or "",
                    "initial_context": project.initial_context or "",
                },
            ),
        )

    @app.post("/projects/{project_id}/edit", response_class=HTMLResponse)
    async def update_project(request: Request, project_id: str):
        project = orchestrator.get_saved_project(project_id)
        form = await _parse_form_values(request)
        default_backend_input = str(form.get("default_backend") or "auto")
        form_values = {
            "name": str(form.get("name") or ""),
            "repo_path": str(form.get("repo_path") or ""),
            "default_backend": default_backend_input,
            "default_provider": str(form.get("default_provider") or ""),
            "default_base_branch": str(form.get("default_base_branch") or ""),
            "default_use_git_worktree": _coerce_checkbox(form.get("default_use_git_worktree")),
            "autonomy_mode": str(form.get("autonomy_mode") or "minimal"),
            "notes": str(form.get("notes") or ""),
            "initial_context": str(form.get("initial_context") or ""),
        }
        try:
            if not form_values["name"].strip():
                raise ValueError("Project name is required.")
            if not form_values["repo_path"].strip():
                raise ValueError("Repository path is required.")
            resolved_default_backend = orchestrator.resolve_coding_agent_preference(
                form_values["default_backend"],
                default_backend=orchestrator.config.default_backend,
            )
            orchestrator.update_saved_project(
                project_id,
                name=form_values["name"].strip(),
                repo_path=form_values["repo_path"].strip(),
                default_backend=resolved_default_backend or None,
                default_provider=form_values["default_provider"] or None,
                default_base_branch=form_values["default_base_branch"] or None,
                default_use_git_worktree=form_values["default_use_git_worktree"],
                notes=form_values["notes"].strip() or None,
                autonomy_mode=form_values["autonomy_mode"] or "minimal",
                initial_context=form_values["initial_context"].strip() or None,
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
    async def project_detail(request: Request, project_id: str, manager_followup: Optional[bool] = None):
        return templates.TemplateResponse(
            request=request,
            name="project_detail.html",
            context=_project_detail_context(
                request,
                project_id,
                manager_followup=bool(manager_followup),
            ),
        )

    @app.get("/projects/{project_id}/workspace", response_class=HTMLResponse)
    async def project_workspace_partial(request: Request, project_id: str):
        context = _build_project_workspace_view(project_id)
        context["request"] = request
        return templates.TemplateResponse(
            request=request,
            name="partials/project_workspace_live.html",
            context=context,
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

    @app.post("/projects/{project_id}/manager/messages", response_class=HTMLResponse)
    async def submit_project_manager_message(request: Request, project_id: str, background_tasks: BackgroundTasks):
        form = await _parse_form_values(request)
        session_before = orchestrator.describe_project_manager_session(project_id)
        manager_form_values = {
            "message": str(form.get("message") or ""),
            "urgency": str(form.get("urgency") or "normal"),
            "coding_agent": str(form.get("coding_agent") or form.get("backend_preference") or "auto"),
            "execution_mode": str(form.get("execution_mode") or ""),
        }
        try:
            snapshot = orchestrator.submit_project_manager_message(
                project_id,
                manager_form_values["message"],
                urgency=manager_form_values["urgency"],
                backend_preference=manager_form_values["coding_agent"],
                execution_mode=manager_form_values["execution_mode"] or None,
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="project_detail.html",
                context=_project_detail_context(
                    request,
                    project_id,
                    manager_form_values=manager_form_values,
                    manager_error=str(exc),
                ),
                status_code=400,
            )
        session_after_obj = orchestrator.describe_project_manager_session(project_id)
        session_after = serialize_project_manager_session(session_after_obj)
        notice = "Project Manager updated the plan and Project Context."
        same_active_job = (
            session_before.active_managed_job_id
            and session_before.active_managed_job_id == session_after_obj.active_managed_job_id
        )
        if session_after["workflow_state"] == "running_current_task":
            refreshed_session, queue_reason = _maybe_start_manager_job_now(
                project_id,
                background_tasks=background_tasks,
            )
            if refreshed_session is not None:
                session_after = refreshed_session
            if same_active_job:
                if session_after["waiting_on_worker"]:
                    notice = queue_reason or (
                        "Your follow-up was saved. The Project Manager is still waiting for a worker to pick up the active task."
                    )
                else:
                    notice = (
                        "Your follow-up was saved. The Project Manager is waiting for the active task result before it launches anything else."
                    )
            elif session_after["active_job_status"] == JobStatus.RUNNING.value:
                notice = "The Project Manager handed the task to the coding agent and started it immediately."
            elif session_after["waiting_on_worker"]:
                notice = queue_reason or "Task was created but could not start immediately because immediate execution is unavailable in this context."
            else:
                notice = "The Project Manager launched the current task and will report back after it finishes."
        elif session_after["workflow_state"] == "awaiting_continue_decision":
            notice = "Your follow-up was saved into Project Context. The Project Manager is waiting on your decision before it launches another task."
        elif session_after["workflow_state"] == "blocked_on_manual_test":
            notice = "Your follow-up was saved into Project Context. The Project Manager is paused until manual testing is complete."
        elif session_after["workflow_state"] == "awaiting_confirmation":
            notice = "The Project Manager drafted the next step and is waiting for your approval before it starts a coding job."
        else:
            notice = "Project Manager updated the plan and Project Context. No coding job was launched."
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice=notice,
        )

    @app.post("/projects/{project_id}/feedback", response_class=HTMLResponse)
    async def submit_project_feedback(request: Request, project_id: str):
        form = await _parse_form_values(request)
        feedback_form_values = {
            "outcome": str(form.get("outcome") or "mixed"),
            "notes": str(form.get("notes") or ""),
            "severity": str(form.get("severity") or "normal"),
            "area": str(form.get("area") or ""),
            "screenshot_reference": str(form.get("screenshot_reference") or ""),
            "requires_followup": _coerce_checkbox(form.get("requires_followup")),
        }
        try:
            orchestrator.submit_project_operator_feedback(
                project_id,
                outcome=feedback_form_values["outcome"],
                notes=feedback_form_values["notes"],
                severity=feedback_form_values["severity"],
                area=feedback_form_values["area"] or None,
                screenshot_reference=feedback_form_values["screenshot_reference"] or None,
                requires_followup=feedback_form_values["requires_followup"],
            )
        except Exception as exc:
            return templates.TemplateResponse(
                request=request,
                name="project_detail.html",
                context=_project_detail_context(
                    request,
                    project_id,
                    feedback_form_values=feedback_form_values,
                    feedback_error=str(exc),
                ),
                status_code=400,
            )
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice="Operator feedback was recorded and the Project Manager refreshed its recommendation.",
        )

    @app.post("/projects/{project_id}/manager/save-advisory")
    async def save_project_manager_advisory(request: Request, project_id: str):
        try:
            orchestrator.save_project_manager_advisory(project_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/projects/{project_id}", error=str(exc))
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice="Added the latest recommendation to Project Context. The manager will use it in future decisions without launching work.",
        )

    @app.post("/projects/{project_id}/autonomy")
    async def update_project_autonomy(request: Request, project_id: str):
        form = await _parse_form_values(request)
        try:
            orchestrator.set_project_autonomy_mode(
                project_id,
                str(form.get("autonomy_mode") or "minimal"),
            )
        except Exception as exc:
            return _redirect_with_feedback(request, f"/projects/{project_id}", error=str(exc))
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice="Updated the Project Manager autonomy mode for this project.",
        )

    @app.post("/projects/{project_id}/manager/continue")
    async def continue_project_manager(request: Request, project_id: str, background_tasks: BackgroundTasks):
        try:
            snapshot = orchestrator.continue_project_manager_autonomy(project_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/projects/{project_id}", error=str(exc))
        notice = "The Project Manager refreshed the next-step recommendation."
        if snapshot.state.workflow_state == "running_current_task":
            session_after, queue_reason = _maybe_start_manager_job_now(
                project_id,
                background_tasks=background_tasks,
            )
            if session_after and session_after["active_job_status"] == JobStatus.RUNNING.value:
                notice = "The Project Manager handed the next supervised task to the coding agent and started it immediately."
            elif session_after and session_after["waiting_on_worker"]:
                notice = queue_reason or "Task was created but could not start immediately because immediate execution is unavailable in this context."
            else:
                notice = "The Project Manager launched the next supervised task and will report back after it finishes."
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice=notice,
        )

    @app.post("/projects/{project_id}/manager/launch-draft")
    async def launch_project_manager_draft(request: Request, project_id: str, background_tasks: BackgroundTasks):
        try:
            job = orchestrator.launch_job_from_project_manager_draft(project_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/projects/{project_id}", error=str(exc))
        start_result = orchestrator.try_start_manager_job_now(
            job.id,
            worker_id=_manager_run_now_worker_id(project_id, job.id),
            launch_context="browser_project_page",
        )
        if start_result.claimed_job is not None:
            background_tasks.add_task(
                orchestrator.process_claimed_job,
                start_result.claimed_job,
                _manager_run_now_worker_id(project_id, job.id),
            )
        return _redirect_with_feedback(
            request,
            f"/projects/{project_id}",
            notice=(
                "The Project Manager handed the latest task to the coding agent and started it immediately."
                if start_result.claimed_job is not None or start_result.current_status == JobStatus.RUNNING.value
                else start_result.reason or "The Project Manager handed the latest task to the coding agent."
            ),
        )

    @app.post("/worker/run-once")
    async def worker_run_once(request: Request):
        worker = WorkerService(orchestrator.repository, orchestrator)
        await worker.run_forever(run_once=True)
        return _redirect_with_feedback(
            request,
            "/",
            notice="Ran one queue-processing pass. Queued jobs that could start have been picked up.",
        )

    @app.post("/jobs/{job_id}/run-now")
    async def run_job_now(request: Request, job_id: str, background_tasks: BackgroundTasks):
        worker_id = f"web-run-now-{job_id[:8]}"
        try:
            claimed = orchestrator.claim_job_for_run_now(job_id, worker_id=worker_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        background_tasks.add_task(orchestrator.process_claimed_job, claimed, worker_id)
        return _redirect_with_feedback(
            request,
            f"/jobs/{job_id}",
            notice="Job was moved out of the queue and started immediately.",
        )

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str):
        try:
            job = orchestrator.retry_job(job_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        return _redirect_with_feedback(
            request,
            f"/jobs/{job.id}",
            notice="Job was re-queued. Use Run Now if you want to start it immediately.",
        )

    @app.post("/jobs/{job_id}/retry-now")
    async def retry_job_now(request: Request, job_id: str, background_tasks: BackgroundTasks):
        worker_id = f"web-retry-now-{job_id[:8]}"
        try:
            claimed = orchestrator.retry_job_now(job_id, worker_id=worker_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        background_tasks.add_task(orchestrator.process_claimed_job, claimed, worker_id)
        return _redirect_with_feedback(
            request,
            f"/jobs/{job_id}",
            notice="Job was retried immediately.",
        )

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        try:
            job = orchestrator.cancel(job_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        return _redirect_with_feedback(
            request,
            f"/jobs/{job.id}",
            notice="Job was cancelled.",
        )

    @app.post("/jobs/{job_id}/duplicate")
    async def duplicate_job(request: Request, job_id: str):
        try:
            job = orchestrator.duplicate_job(job_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        return _redirect_with_feedback(
            request,
            f"/jobs/{job.id}",
            notice="Created a duplicate queued job.",
        )

    @app.post("/jobs/{job_id}/delete")
    async def delete_job(request: Request, job_id: str):
        try:
            deleted = orchestrator.delete_job(job_id)
        except Exception as exc:
            return _redirect_with_feedback(request, f"/jobs/{job_id}", error=str(exc))
        notice = "Job was deleted."
        if not deleted.workspace_cleaned:
            notice = f"Job was deleted. Workspace cleanup was skipped: {deleted.workspace_cleanup_reason}."
        return _redirect_with_feedback(request, "/", notice=notice)

    return app

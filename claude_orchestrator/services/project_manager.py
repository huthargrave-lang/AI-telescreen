"""Persistent per-project manager memory and recommendation helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import (
    JobStatus,
    ProjectManagerEvent,
    ProjectManagerRecommendation,
    ProjectManagerSnapshot,
    ProjectManagerState,
    SavedProject,
)
from ..repository import JobRepository
from ..timeutils import utcnow
from ..workspaces import resolve_job_prompt


class ProjectManagerService:
    """Maintains compact durable project memory and advisory recommendations."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        recent_event_limit: int = 5,
        retained_event_limit: int = 6,
    ) -> None:
        self.repository = repository
        self.recent_event_limit = recent_event_limit
        self.retained_event_limit = max(retained_event_limit, recent_event_limit)

    def sync_project(self, project_id: str) -> ProjectManagerState:
        """Ensure manager state exists and reflects the latest saved-project defaults."""

        return self._refresh_state(project_id)

    def compact_manager_state(self, project_id: str) -> ProjectManagerState:
        """Force a deterministic state refresh and event compaction pass."""

        return self._refresh_state(project_id)

    def get_snapshot(self, project_id: str) -> ProjectManagerSnapshot:
        state = self._refresh_state(project_id)
        recent_events = self.repository.list_project_manager_events(
            project_id,
            limit=self.recent_event_limit,
        )
        return ProjectManagerSnapshot(state=state, recent_events=recent_events)

    def ingest_job_outcome(
        self,
        job_id: str,
        *,
        response_summary: Optional[Dict[str, Any]] = None,
        needs_followup: bool = False,
        followup_reason: Optional[str] = None,
        error_reason: Optional[str] = None,
    ) -> Optional[ProjectManagerSnapshot]:
        job = self.repository.get_job(job_id)
        if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
            return None
        project_id = str(job.metadata.get("project_id") or "").strip()
        if not project_id:
            return None

        self._refresh_state(project_id)
        event = ProjectManagerEvent(
            id=str(uuid.uuid4()),
            project_id=project_id,
            created_at=utcnow(),
            event_type="job_outcome",
            source_job_id=job.id,
            attempt_number=job.attempt_count,
            outcome_status=job.status.value,
            summary=self._build_event_summary(
                job_id=job.id,
                response_summary=response_summary or {},
                needs_followup=needs_followup,
                followup_reason=followup_reason,
                error_reason=error_reason,
            ),
        )
        self.repository.add_project_manager_event(event)
        return self.get_snapshot(project_id)

    def _refresh_state(self, project_id: str) -> ProjectManagerState:
        project = self.repository.get_saved_project(project_id)
        previous = self.repository.get_project_manager_state(project_id)
        recent_events, rolling_summary, rolling_facts, last_compacted_at = self._compact_if_needed(
            project_id,
            previous,
        )
        stable_project_facts = self._build_stable_project_facts(project, previous)
        recommendation = self._build_recommendation(project, recent_events)
        current_phase = self._determine_phase(recent_events, recommendation)
        testing_status = self._determine_testing_status(recent_events, recommendation)
        needs_manual_testing = recommendation.decision == "request_manual_test"
        last_job_ingested_at = recent_events[0].created_at if recent_events else (
            previous.last_job_ingested_at if previous else None
        )
        now = utcnow()
        summary = self._build_state_summary(
            project,
            current_phase=current_phase,
            recent_events=recent_events,
            rolling_summary=rolling_summary,
            recommendation=recommendation,
        )
        state = ProjectManagerState(
            project_id=project_id,
            current_phase=current_phase,
            summary=summary,
            stable_project_facts=stable_project_facts,
            testing_status=testing_status,
            needs_manual_testing=needs_manual_testing,
            rolling_summary=rolling_summary,
            rolling_facts=rolling_facts,
            latest_recommendation=recommendation,
            latest_recommendation_type=recommendation.decision,
            latest_recommendation_reason=recommendation.reason,
            last_job_ingested_at=last_job_ingested_at,
            last_compacted_at=last_compacted_at,
            created_at=previous.created_at if previous and previous.created_at else now,
            updated_at=now,
        )
        return self.repository.upsert_project_manager_state(state)

    def _compact_if_needed(
        self,
        project_id: str,
        previous: Optional[ProjectManagerState],
    ) -> tuple[List[ProjectManagerEvent], Optional[str], Dict[str, Any], Optional[datetime]]:
        events = self.repository.list_project_manager_events(project_id, limit=None)
        rolling_summary = previous.rolling_summary if previous else None
        rolling_facts = dict(previous.rolling_facts) if previous else {}
        last_compacted_at = previous.last_compacted_at if previous else None
        if len(events) <= self.retained_event_limit:
            return events, rolling_summary, rolling_facts, last_compacted_at

        older_events = events[self.retained_event_limit :]
        rolling_facts = self._merge_rolling_facts(rolling_facts, older_events)
        rolling_summary = self._build_rolling_summary(rolling_facts)
        self.repository.delete_project_manager_events(
            project_id,
            [event.id for event in older_events],
        )
        return events[: self.retained_event_limit], rolling_summary, rolling_facts, utcnow()

    def _build_stable_project_facts(
        self,
        project: SavedProject,
        previous: Optional[ProjectManagerState],
    ) -> Dict[str, Any]:
        stable_facts = dict(previous.stable_project_facts) if previous else {}
        stable_facts.update(
            {
                "project_name": project.name,
                "repo_path": project.repo_path,
                "default_backend": project.default_backend,
                "default_provider": project.default_provider,
                "default_base_branch": project.default_base_branch,
                "default_use_git_worktree": project.default_use_git_worktree,
                "notes": project.notes,
            }
        )
        return stable_facts

    def _build_event_summary(
        self,
        *,
        job_id: str,
        response_summary: Dict[str, Any],
        needs_followup: bool,
        followup_reason: Optional[str],
        error_reason: Optional[str],
    ) -> Dict[str, Any]:
        job = self.repository.get_job(job_id)
        artifacts = self.repository.get_artifacts(job_id)
        stream_events = self.repository.get_stream_events(job_id, limit=6)
        prompt_preview = self._truncate(resolve_job_prompt(job), 240)
        changed_files = self._collect_changed_files(response_summary, artifacts)
        response_preview = self._response_summary_preview(response_summary)
        stream_highlights = [
            self._truncate(event.message, 120)
            for event in stream_events
            if event.message.strip()
        ][-3:]
        latest_phase = next((event.phase for event in reversed(stream_events) if event.phase), None)
        manual_testing_needed = self._should_request_manual_testing(
            prompt_preview,
            changed_files,
            response_preview,
            stream_highlights,
        )
        return {
            "job_id": job.id,
            "status": job.status.value,
            "provider": job.provider,
            "backend": job.backend,
            "task_type": job.task_type,
            "attempt_count": job.attempt_count,
            "prompt": prompt_preview,
            "changed_files": changed_files,
            "artifacts": [
                {
                    "kind": artifact.kind,
                    "path": artifact.path,
                }
                for artifact in artifacts[-4:]
            ],
            "artifact_kinds": sorted({artifact.kind for artifact in artifacts}),
            "latest_phase": latest_phase,
            "stream_highlights": stream_highlights,
            "response_summary_preview": response_preview,
            "error_summary": self._truncate(job.last_error_message, 240),
            "error_reason": error_reason,
            "last_error_code": job.last_error_code,
            "needs_followup": needs_followup,
            "followup_reason": self._truncate(followup_reason, 240),
            "manual_testing_needed": manual_testing_needed,
            "testing_notes": (
                "Browser-facing changes were detected, so a manual verification pass is recommended."
                if manual_testing_needed
                else None
            ),
        }

    def _collect_changed_files(
        self,
        response_summary: Dict[str, Any],
        artifacts: Sequence[Any],
    ) -> List[str]:
        candidates: List[str] = []
        for key in ("changed_files", "files_changed", "files_modified", "modified_files"):
            raw = response_summary.get(key)
            if isinstance(raw, list):
                candidates.extend(str(item) for item in raw if str(item).strip())
        for artifact in artifacts:
            for key in ("changed_files", "files_changed", "files_modified"):
                raw = artifact.metadata.get(key)
                if isinstance(raw, list):
                    candidates.extend(str(item) for item in raw if str(item).strip())
        deduped: List[str] = []
        seen = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped[:12]

    def _response_summary_preview(self, response_summary: Dict[str, Any]) -> Optional[str]:
        if not response_summary:
            return None
        for key in ("summary", "message", "result", "output", "text"):
            value = response_summary.get(key)
            if isinstance(value, str) and value.strip():
                return self._truncate(value, 240)
        preview_parts: List[str] = []
        for key in ("changed_files", "files_changed", "notes", "status", "reason"):
            value = response_summary.get(key)
            if value in (None, "", [], {}):
                continue
            preview_parts.append(f"{key}={value}")
        return self._truncate("; ".join(preview_parts), 240) if preview_parts else None

    def _merge_rolling_facts(
        self,
        existing: Dict[str, Any],
        older_events: Sequence[ProjectManagerEvent],
    ) -> Dict[str, Any]:
        merged = {
            "compacted_event_count": int(existing.get("compacted_event_count", 0)),
            "completed_count": int(existing.get("completed_count", 0)),
            "failed_count": int(existing.get("failed_count", 0)),
            "manual_testing_count": int(existing.get("manual_testing_count", 0)),
            "common_blockers": list(existing.get("common_blockers", [])),
        }
        blocker_set = {str(item) for item in merged["common_blockers"] if str(item).strip()}
        for event in older_events:
            merged["compacted_event_count"] += 1
            if event.outcome_status == JobStatus.COMPLETED.value:
                merged["completed_count"] += 1
            if event.outcome_status == JobStatus.FAILED.value:
                merged["failed_count"] += 1
            if event.summary.get("manual_testing_needed"):
                merged["manual_testing_count"] += 1
            blocker = self._truncate(
                event.summary.get("error_summary") or event.summary.get("error_reason"),
                90,
            )
            if blocker and blocker not in blocker_set:
                blocker_set.add(blocker)
        merged["common_blockers"] = list(sorted(blocker_set))[:3]
        return merged

    def _build_rolling_summary(self, rolling_facts: Dict[str, Any]) -> Optional[str]:
        if not rolling_facts.get("compacted_event_count"):
            return None
        parts = [
            (
                f"Older manager memory covers {rolling_facts['compacted_event_count']} earlier outcomes "
                f"({rolling_facts['completed_count']} completed, {rolling_facts['failed_count']} failed)."
            )
        ]
        if rolling_facts.get("manual_testing_count"):
            parts.append(
                f"Manual verification was requested {rolling_facts['manual_testing_count']} time(s)."
            )
        blockers = rolling_facts.get("common_blockers") or []
        if blockers:
            parts.append(f"Common blockers: {', '.join(blockers)}.")
        return self._truncate(" ".join(parts), 420)

    def _build_recommendation(
        self,
        project: SavedProject,
        recent_events: Sequence[ProjectManagerEvent],
    ) -> ProjectManagerRecommendation:
        if not recent_events:
            return ProjectManagerRecommendation(
                decision="wait_for_operator",
                reason="No completed or failed project jobs have been ingested yet.",
            )

        latest = recent_events[0]
        summary = latest.summary
        latest_error = str(summary.get("error_summary") or summary.get("error_reason") or "").lower()
        latest_prompt = str(summary.get("prompt") or "")

        if latest.outcome_status == JobStatus.FAILED.value:
            if any(
                token in latest_error
                for token in ("auth", "api key", "configuration", "backend_disabled", "not currently enabled", "permission", "missing executable")
            ):
                return ProjectManagerRecommendation(
                    decision="wait_for_operator",
                    reason=(
                        "The latest project job failed in a way that likely needs operator intervention "
                        "before another coding pass should start."
                    ),
                )
            return ProjectManagerRecommendation(
                decision="launch_followup_job",
                reason=(
                    summary.get("error_summary")
                    or summary.get("error_reason")
                    or "The latest project job failed and should be investigated."
                ),
                next_task={
                    "backend": summary.get("backend") or project.default_backend or "messages_api",
                    "task_type": summary.get("task_type") or "code",
                    "prompt": self._build_failure_followup_prompt(project, summary),
                },
            )

        if summary.get("manual_testing_needed"):
            return ProjectManagerRecommendation(
                decision="request_manual_test",
                reason="Recent browser-facing changes should be verified manually before another coding pass.",
                manual_test_checklist=self._manual_test_checklist(project, summary),
            )

        if summary.get("needs_followup"):
            return ProjectManagerRecommendation(
                decision="launch_followup_job",
                reason=summary.get("followup_reason") or "Recent project work suggests a follow-up coding task.",
                next_task={
                    "backend": summary.get("backend") or project.default_backend or "messages_api",
                    "task_type": summary.get("task_type") or "code",
                    "prompt": self._build_followup_prompt(project, summary),
                },
            )

        if len(recent_events) >= 3 and all(event.outcome_status == JobStatus.COMPLETED.value for event in recent_events[:3]):
            return ProjectManagerRecommendation(
                decision="mark_complete",
                reason="Recent project work completed cleanly without new failures or manual-test requests.",
            )

        if any(keyword in latest_prompt.lower() for keyword in ("clarify", "decide", "plan", "brainstorm")):
            return ProjectManagerRecommendation(
                decision="needs_clarification",
                reason="The latest outcome looks more like planning work than an implementation-ready next step.",
            )

        return ProjectManagerRecommendation(
            decision="wait_for_operator",
            reason="Recent work is recorded. Review the latest outcome and decide whether to launch another task.",
        )

    def _determine_phase(
        self,
        recent_events: Sequence[ProjectManagerEvent],
        recommendation: ProjectManagerRecommendation,
    ) -> str:
        if not recent_events:
            return "planning"
        latest = recent_events[0]
        if latest.outcome_status == JobStatus.FAILED.value:
            return "blocked"
        if recommendation.decision == "request_manual_test":
            return "awaiting_manual_test"
        if recommendation.decision == "launch_followup_job":
            return "follow_up_recommended"
        if recommendation.decision == "mark_complete":
            return "complete"
        return "active"

    def _determine_testing_status(
        self,
        recent_events: Sequence[ProjectManagerEvent],
        recommendation: ProjectManagerRecommendation,
    ) -> str:
        if recommendation.decision == "request_manual_test":
            return "manual_testing_requested"
        if recent_events and recent_events[0].outcome_status == JobStatus.FAILED.value:
            return "blocked_by_failure"
        if recent_events and recent_events[0].outcome_status == JobStatus.COMPLETED.value:
            return "no_manual_testing_requested"
        return "not_requested"

    def _build_state_summary(
        self,
        project: SavedProject,
        *,
        current_phase: str,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
        recommendation: ProjectManagerRecommendation,
    ) -> str:
        if not recent_events:
            return self._truncate(
                f"{project.name} is in planning. Launch the first job from this project to start building manager memory.",
                520,
            )
        latest = recent_events[0].summary
        latest_line = (
            f"Latest outcome: {recent_events[0].outcome_status} via {latest.get('backend') or project.default_backend or 'unknown backend'}."
        )
        if latest.get("response_summary_preview"):
            latest_line += f" {latest['response_summary_preview']}"
        elif latest.get("error_summary"):
            latest_line += f" {latest['error_summary']}"
        parts = [
            f"Current phase: {current_phase.replace('_', ' ')}.",
            latest_line,
            f"Recommendation: {recommendation.decision.replace('_', ' ')}.",
        ]
        if rolling_summary:
            parts.append(rolling_summary)
        return self._truncate(" ".join(parts), 520)

    def _should_request_manual_testing(
        self,
        prompt_preview: str,
        changed_files: Sequence[str],
        response_preview: Optional[str],
        stream_highlights: Sequence[str],
    ) -> bool:
        combined = " ".join(
            [
                prompt_preview,
                response_preview or "",
                " ".join(changed_files),
                " ".join(stream_highlights),
            ]
        ).lower()
        keywords = (
            "ui",
            "dashboard",
            "browser",
            "project page",
            "job detail",
            "template",
            "html",
            "css",
            "format",
            "layout",
            "tooltip",
            "badge",
            "web/",
        )
        if any(keyword in combined for keyword in keywords):
            return True
        return any(
            Path(path).suffix.lower() in {".html", ".css", ".js", ".ts", ".tsx"}
            or "web/" in path
            or "templates/" in path
            for path in changed_files
        )

    def _manual_test_checklist(
        self,
        project: SavedProject,
        summary: Dict[str, Any],
    ) -> List[str]:
        context = " ".join(
            [
                str(summary.get("prompt") or ""),
                " ".join(summary.get("changed_files") or []),
                str(summary.get("response_summary_preview") or ""),
            ]
        ).lower()
        if any(keyword in context for keyword in ("queued", "retry", "cancel", "delete", "job table", "dashboard")):
            return [
                "Create a queued job in the browser.",
                "Confirm the main job actions render correctly for that state.",
                "Check that retry-only actions stay hidden for queued work.",
                "Cancel or delete a throwaway job and confirm the table updates cleanly.",
            ]
        if any(keyword in context for keyword in ("project", "repo path", "defaults", "project row")):
            return [
                "Open the saved project page in the browser.",
                "Confirm the project defaults and summary cards are readable.",
                "Launch a throwaway job from the project page.",
                "Check that the recent-launch history and recommendation panel update cleanly.",
            ]
        return [
            "Open the updated AI Telescreen browser flow.",
            "Exercise the main operator path touched by the recent job.",
            "Confirm the visible text, layout, and actions read clearly.",
            "Record any regressions before launching another coding task.",
        ]

    def _build_failure_followup_prompt(
        self,
        project: SavedProject,
        summary: Dict[str, Any],
    ) -> str:
        failure_reason = summary.get("error_summary") or summary.get("error_reason") or "unknown failure"
        recent_prompt = summary.get("prompt") or "recent project task"
        return (
            f"Follow up on the latest failed AI Telescreen project task for {project.name}. "
            f"The previous task was: {recent_prompt}. "
            f"It failed with: {failure_reason}. "
            "Investigate the root cause, implement a fix if appropriate, and summarize any manual verification still needed."
        )

    def _build_followup_prompt(
        self,
        project: SavedProject,
        summary: Dict[str, Any],
    ) -> str:
        recent_prompt = summary.get("prompt") or "recent project task"
        followup_reason = summary.get("followup_reason") or "continue the project from the latest successful result"
        return (
            f"Continue the AI Telescreen project work for {project.name}. "
            f"The previous completed task was: {recent_prompt}. "
            f"Follow-up reason: {followup_reason}. "
            "Take the next coding step and call out any manual testing that should happen afterward."
        )

    def _truncate(self, value: Optional[str], limit: int) -> Optional[str]:
        if not value:
            return value
        text = str(value).strip().replace("\n", " ")
        if len(text) <= limit:
            return text
        return f"{text[: limit - 1].rstrip()}…"

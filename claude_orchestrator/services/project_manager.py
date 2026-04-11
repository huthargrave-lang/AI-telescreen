"""Persistent per-project manager memory and recommendation helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import (
    JobStatus,
    ProjectManagerDraftTask,
    ProjectManagerEvent,
    ProjectManagerRecommendation,
    ProjectManagerResponse,
    ProjectManagerSnapshot,
    ProjectManagerState,
    SavedProject,
)
from ..repository import JobRepository
from ..timeutils import utcnow
from ..workspaces import resolve_job_prompt


class ProjectManagerService:
    """Maintains compact durable project memory and advisory recommendations."""

    VALID_PHASES = {"planning", "implementation", "debugging", "polish", "release_ready", "blocked"}
    VALID_DECISIONS = {
        "launch_followup_job",
        "request_manual_test",
        "wait_for_operator",
        "mark_complete",
        "needs_clarification",
    }
    VALID_CONFIDENCE = {"low", "medium", "high"}
    VALID_LAYOUT_HINTS = {"planning", "implementation", "debugging", "polish"}
    VALID_EXECUTION_MODES = {"read_only", "safe_changes", "coding_pass"}

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
        latest_response = self._build_response(
            project,
            recent_events=recent_events,
            rolling_summary=rolling_summary,
        )
        recommendation = ProjectManagerRecommendation.from_response(latest_response)
        current_phase = latest_response.phase
        testing_status = self._determine_testing_status(recent_events, latest_response)
        needs_manual_testing = latest_response.needs_manual_testing
        last_job_ingested_at = recent_events[0].created_at if recent_events else (
            previous.last_job_ingested_at if previous else None
        )
        now = utcnow()
        summary = self._legacy_summary_from_response(latest_response, rolling_summary)
        state = ProjectManagerState(
            project_id=project_id,
            current_phase=current_phase,
            summary=summary,
            stable_project_facts=stable_project_facts,
            testing_status=testing_status,
            needs_manual_testing=needs_manual_testing,
            rolling_summary=rolling_summary,
            rolling_facts=rolling_facts,
            latest_response=latest_response,
            display_snapshot=self._build_display_snapshot(latest_response),
            latest_recommendation=recommendation,
            latest_recommendation_type=recommendation.decision if recommendation else None,
            latest_recommendation_reason=recommendation.reason if recommendation else None,
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

    def _build_response(
        self,
        project: SavedProject,
        *,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
    ) -> ProjectManagerResponse:
        if not recent_events:
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="planning",
                    summary_bullets=[
                        f"{project.name} does not have any completed or failed project-linked jobs in manager memory yet.",
                        self._project_defaults_bullet(project),
                        "Launch the first saved-project job to give the manager concrete outcomes to supervise.",
                    ],
                    recent_change_bullets=[],
                    active_focus_bullets=[
                        "Choose the next concrete coding or debugging pass for this project.",
                        "Use the saved project defaults as the starting point so new jobs stay tied to project memory.",
                    ],
                    risks_or_blockers=[],
                    decision="wait_for_operator",
                    reason="No completed or failed project jobs have been ingested yet.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="planning",
                    confidence="medium",
                )
            )

        latest_event = recent_events[0]
        latest_summary = latest_event.summary
        recent_change_bullets = self._recent_change_bullets(recent_events)
        risks_or_blockers = self._risk_bullets(recent_events, rolling_summary=rolling_summary)
        summary_bullets = self._summary_bullets(
            project,
            recent_events=recent_events,
            rolling_summary=rolling_summary,
        )
        latest_error = str(latest_summary.get("error_summary") or latest_summary.get("error_reason") or "").lower()
        latest_prompt = str(latest_summary.get("prompt") or "")

        if latest_event.outcome_status == JobStatus.FAILED.value:
            if any(
                token in latest_error
                for token in (
                    "auth",
                    "api key",
                    "configuration",
                    "backend_disabled",
                    "not currently enabled",
                    "permission",
                    "missing executable",
                )
            ):
                return self._normalize_response(
                    ProjectManagerResponse(
                        phase="blocked",
                        summary_bullets=summary_bullets,
                        recent_change_bullets=recent_change_bullets,
                        active_focus_bullets=[
                            "Resolve the operator-facing configuration or access issue before launching another coding pass.",
                            "Use Doctor and the saved project defaults to confirm the environment is healthy again.",
                        ],
                        risks_or_blockers=risks_or_blockers,
                        decision="wait_for_operator",
                        reason=(
                            "The latest project job failed in a way that likely needs operator intervention before "
                            "another coding pass should start."
                        ),
                        draft_task=None,
                        needs_manual_testing=False,
                        manual_test_checklist=[],
                        followup_questions=[
                            "Has the backend or local environment issue been fixed so another job can run cleanly?"
                        ],
                        ui_layout_hint="debugging",
                        confidence="high",
                    )
                )
            reason = (
                latest_summary.get("error_summary")
                or latest_summary.get("error_reason")
                or "The latest project job failed and should be investigated."
            )
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="debugging",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Reproduce the latest failure in the prepared project context.",
                        "Implement the smallest safe fix that addresses the current blocker.",
                        "Call out any manual verification still needed after the fix lands.",
                    ],
                    risks_or_blockers=risks_or_blockers,
                    decision="launch_followup_job",
                    reason=reason,
                    draft_task=self._build_draft_task(
                        project,
                        latest_summary,
                        prompt=self._build_failure_followup_prompt(project, latest_summary),
                        execution_mode="coding_pass",
                        rationale_bullets=[
                            "The latest project-linked job failed and needs a focused debugging pass.",
                            "Keeping the follow-up attached to the saved project preserves context across runs.",
                            "The next pass should end with a clear verification summary, not just a code change.",
                        ],
                    ),
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="debugging",
                    confidence="high",
                )
            )

        if latest_summary.get("manual_testing_needed"):
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="polish",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Run the manual browser checks before starting another coding pass.",
                        "Capture any regressions or awkward UI behavior directly from the visible operator flow.",
                    ],
                    risks_or_blockers=risks_or_blockers
                    + ["Manual verification is still pending on the latest browser-facing change."],
                    decision="request_manual_test",
                    reason="Recent browser-facing changes should be verified manually before another coding pass.",
                    draft_task=None,
                    needs_manual_testing=True,
                    manual_test_checklist=self._manual_test_checklist(project, latest_summary),
                    followup_questions=[],
                    ui_layout_hint="polish",
                    confidence="high",
                )
            )

        if latest_summary.get("needs_followup"):
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="implementation",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Continue the project from the latest completed result instead of starting from scratch.",
                        "Keep the next pass scoped to the concrete follow-up reason captured in manager memory.",
                    ],
                    risks_or_blockers=risks_or_blockers,
                    decision="launch_followup_job",
                    reason=latest_summary.get("followup_reason") or "Recent project work suggests a follow-up coding task.",
                    draft_task=self._build_draft_task(
                        project,
                        latest_summary,
                        prompt=self._build_followup_prompt(project, latest_summary),
                        execution_mode=self._infer_execution_mode(latest_summary),
                        rationale_bullets=[
                            "The latest completed task explicitly asked for a follow-up step.",
                            "Reusing saved-project defaults keeps the next pass aligned with the current project context.",
                            "The manager is staying advisory by drafting work instead of auto-enqueueing it.",
                        ],
                    ),
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="implementation",
                    confidence="high",
                )
            )

        if len(recent_events) >= 3 and all(event.outcome_status == JobStatus.COMPLETED.value for event in recent_events[:3]):
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="release_ready",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Review the last few completed passes and decide whether the current project phase is done.",
                        "If you still want another pass, start from an explicit draft instead of assuming more work is required.",
                    ],
                    risks_or_blockers=risks_or_blockers,
                    decision="mark_complete",
                    reason="Recent project work completed cleanly without new failures or manual-test requests.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="polish",
                    confidence="medium",
                )
            )

        if any(keyword in latest_prompt.lower() for keyword in ("clarify", "decide", "plan", "brainstorm")):
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="planning",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Turn the latest planning output into a concrete implementation-ready task.",
                        "Resolve the open questions before launching another coding pass.",
                    ],
                    risks_or_blockers=risks_or_blockers,
                    decision="needs_clarification",
                    reason="The latest outcome looks more like planning work than an implementation-ready next step.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[
                        "What is the next concrete user-visible outcome this project should deliver?",
                        "Should the next pass focus on implementation, debugging, or polish?",
                    ],
                    ui_layout_hint="planning",
                    confidence="low",
                )
            )

        return self._normalize_response(
            ProjectManagerResponse(
                phase="implementation",
                summary_bullets=summary_bullets,
                recent_change_bullets=recent_change_bullets,
                active_focus_bullets=[
                    "Review the latest outcome and decide whether to launch another scoped task or pause for operator review.",
                    "Use the saved project context and recent ingested outcomes to keep the next step grounded.",
                ],
                risks_or_blockers=risks_or_blockers,
                decision="wait_for_operator",
                reason="Recent work is recorded. Review the latest outcome and decide whether to launch another task.",
                draft_task=None,
                needs_manual_testing=False,
                manual_test_checklist=[],
                followup_questions=[],
                ui_layout_hint="implementation",
                confidence="medium",
            )
        )

    def _determine_testing_status(
        self,
        recent_events: Sequence[ProjectManagerEvent],
        response: ProjectManagerResponse,
    ) -> str:
        if response.needs_manual_testing:
            return "manual_testing_requested"
        if response.phase == "blocked":
            return "blocked"
        if response.phase == "debugging":
            return "debugging_in_progress"
        if response.phase == "release_ready":
            return "ready_for_review"
        if recent_events and recent_events[0].outcome_status == JobStatus.COMPLETED.value:
            return "no_manual_testing_requested"
        return "not_requested"

    def _legacy_summary_from_response(
        self,
        response: ProjectManagerResponse,
        rolling_summary: Optional[str],
    ) -> str:
        parts: List[str] = []
        if response.summary_bullets:
            parts.append(response.summary_bullets[0])
        if response.reason:
            parts.append(f"Recommendation: {response.decision.replace('_', ' ')}. {response.reason}")
        if rolling_summary:
            parts.append(rolling_summary)
        return self._truncate(" ".join(parts), 520) or ""

    def _project_defaults_bullet(self, project: SavedProject) -> str:
        backend = project.default_backend or "default backend"
        provider = project.default_provider or "auto provider"
        branch = project.default_base_branch or "current branch"
        workspace_mode = "git worktree" if project.default_use_git_worktree else "plain workspace"
        return (
            f"Saved defaults currently point at {backend} / {provider}, base branch {branch}, using {workspace_mode} mode."
        )

    def _summary_bullets(
        self,
        project: SavedProject,
        *,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
    ) -> List[str]:
        latest_event = recent_events[0]
        latest_summary = latest_event.summary
        latest_backend = latest_summary.get("backend") or project.default_backend or "unknown backend"
        latest_provider = latest_summary.get("provider") or project.default_provider or "unknown provider"
        latest_outcome = latest_event.outcome_status or "unknown"
        latest_detail = (
            latest_summary.get("response_summary_preview")
            or latest_summary.get("error_summary")
            or latest_summary.get("error_reason")
            or "No concise outcome summary was recorded."
        )
        bullets = [
            f"{project.name} most recently recorded a {latest_outcome} job via {latest_backend} / {latest_provider}.",
            self._truncate(str(latest_detail), 180) or "The latest job completed without a compact detail summary.",
            self._project_defaults_bullet(project),
        ]
        if rolling_summary:
            bullets.append(rolling_summary)
        return self._normalize_bullets(bullets, limit=4)

    def _recent_change_bullets(self, recent_events: Sequence[ProjectManagerEvent]) -> List[str]:
        bullets: List[str] = []
        for event in recent_events[:3]:
            summary = event.summary
            backend = summary.get("backend") or "unknown backend"
            if event.outcome_status == JobStatus.FAILED.value:
                detail = summary.get("error_summary") or summary.get("error_reason") or "Failed without an error summary."
                bullet = f"Failed via {backend}: {detail}"
            else:
                detail = summary.get("response_summary_preview")
                if detail:
                    bullet = f"Completed via {backend}: {detail}"
                elif summary.get("changed_files"):
                    touched = ", ".join(summary.get("changed_files")[:3])
                    bullet = f"Completed via {backend}: touched {touched}"
                else:
                    bullet = f"Completed via {backend} without an explicit compact change summary."
            bullets.append(bullet)
        return self._normalize_bullets(bullets, limit=4)

    def _risk_bullets(
        self,
        recent_events: Sequence[ProjectManagerEvent],
        *,
        rolling_summary: Optional[str],
    ) -> List[str]:
        risks: List[str] = []
        for event in recent_events[:3]:
            summary = event.summary
            error_text = summary.get("error_summary") or summary.get("error_reason")
            if error_text:
                risks.append(str(error_text))
            if summary.get("manual_testing_needed"):
                risks.append("The latest browser-facing change still needs manual verification.")
        common_blockers = recent_events[0].summary.get("followup_reason")
        if common_blockers and "manual" not in str(common_blockers).lower():
            risks.append(str(common_blockers))
        if rolling_summary and "common blockers:" in rolling_summary.lower():
            risks.append(rolling_summary)
        return self._normalize_bullets(risks, limit=4)

    def _build_draft_task(
        self,
        project: SavedProject,
        summary: Dict[str, Any],
        *,
        prompt: str,
        execution_mode: str,
        rationale_bullets: Sequence[str],
    ) -> ProjectManagerDraftTask:
        return self._normalize_draft_task(
            ProjectManagerDraftTask(
                prompt=prompt,
                backend=str(summary.get("backend") or project.default_backend or "messages_api"),
                task_type=str(summary.get("task_type") or "code"),
                priority=0,
                use_git_worktree=bool(project.default_use_git_worktree),
                base_branch=project.default_base_branch,
                provider=str(summary.get("provider") or project.default_provider or "").strip() or None,
                execution_mode=execution_mode,
                rationale_bullets=list(rationale_bullets),
                derived_from_operator_message=str(summary.get("prompt") or ""),
            )
        )

    def _infer_execution_mode(self, summary: Dict[str, Any]) -> str:
        combined = " ".join(
            [
                str(summary.get("prompt") or ""),
                str(summary.get("response_summary_preview") or ""),
                " ".join(summary.get("changed_files") or []),
            ]
        ).lower()
        if any(token in combined for token in ("format", "layout", "copy", "badge", "tooltip", "ui", "browser")):
            return "safe_changes"
        return "coding_pass"

    def _normalize_response(self, response: ProjectManagerResponse) -> ProjectManagerResponse:
        phase = response.phase if response.phase in self.VALID_PHASES else "planning"
        decision = response.decision if response.decision in self.VALID_DECISIONS else "wait_for_operator"
        ui_layout_hint = response.ui_layout_hint if response.ui_layout_hint in self.VALID_LAYOUT_HINTS else "planning"
        confidence = response.confidence if response.confidence in self.VALID_CONFIDENCE else "medium"
        draft_task = self._normalize_draft_task(response.draft_task) if response.draft_task else None
        manual_test_checklist = self._normalize_bullets(response.manual_test_checklist, limit=6)
        followup_questions = self._normalize_bullets(response.followup_questions, limit=4)
        return ProjectManagerResponse(
            phase=phase,
            summary_bullets=self._normalize_bullets(response.summary_bullets, limit=4),
            recent_change_bullets=self._normalize_bullets(response.recent_change_bullets, limit=4),
            active_focus_bullets=self._normalize_bullets(response.active_focus_bullets, limit=4),
            risks_or_blockers=self._normalize_bullets(response.risks_or_blockers, limit=4),
            decision=decision,
            reason=self._truncate(response.reason, 240) or "",
            draft_task=draft_task,
            needs_manual_testing=bool(
                response.needs_manual_testing or manual_test_checklist or decision == "request_manual_test"
            ),
            manual_test_checklist=manual_test_checklist,
            followup_questions=followup_questions,
            ui_layout_hint=ui_layout_hint,
            confidence=confidence,
        )

    def _normalize_draft_task(self, draft_task: ProjectManagerDraftTask) -> ProjectManagerDraftTask:
        execution_mode = (
            draft_task.execution_mode
            if draft_task.execution_mode in self.VALID_EXECUTION_MODES
            else "coding_pass"
        )
        return ProjectManagerDraftTask(
            prompt=self._truncate(draft_task.prompt, 1200) or "",
            backend=draft_task.backend or "messages_api",
            task_type=draft_task.task_type or "code",
            priority=int(draft_task.priority),
            use_git_worktree=bool(draft_task.use_git_worktree),
            base_branch=self._truncate(draft_task.base_branch, 120),
            provider=self._truncate(draft_task.provider, 80),
            execution_mode=execution_mode,
            rationale_bullets=self._normalize_bullets(draft_task.rationale_bullets, limit=4),
            derived_from_operator_message=self._truncate(draft_task.derived_from_operator_message, 280) or "",
        )

    def _normalize_bullets(self, items: Sequence[str], *, limit: int = 4) -> List[str]:
        normalized: List[str] = []
        seen = set()
        for item in items:
            text = self._truncate(str(item), 220)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(text)
            if len(normalized) >= limit:
                break
        return normalized

    def _build_display_snapshot(self, response: ProjectManagerResponse) -> Dict[str, Any]:
        section_titles = {
            "summary_bullets": "Summary",
            "recent_change_bullets": "Recent Changes",
            "active_focus_bullets": "Active Focus",
            "risks_or_blockers": "Risks or Blockers",
            "manual_test_checklist": "Manual Test Checklist",
            "followup_questions": "Follow-up Questions",
        }
        section_values = {
            "summary_bullets": response.summary_bullets,
            "recent_change_bullets": response.recent_change_bullets,
            "active_focus_bullets": response.active_focus_bullets,
            "risks_or_blockers": response.risks_or_blockers,
            "manual_test_checklist": response.manual_test_checklist,
            "followup_questions": response.followup_questions,
        }
        primary_order_map = {
            "planning": ["summary_bullets", "risks_or_blockers", "followup_questions"],
            "implementation": ["recent_change_bullets", "active_focus_bullets", "summary_bullets"],
            "debugging": ["risks_or_blockers", "recent_change_bullets", "manual_test_checklist"],
            "polish": ["active_focus_bullets", "manual_test_checklist", "recent_change_bullets"],
        }
        primary_order = primary_order_map.get(response.ui_layout_hint, primary_order_map["planning"])

        def build_sections(keys: Sequence[str]) -> List[Dict[str, Any]]:
            sections: List[Dict[str, Any]] = []
            for key in keys:
                items = section_values.get(key) or []
                if not items:
                    continue
                sections.append({"key": key, "title": section_titles[key], "items": list(items)})
            return sections

        primary_sections = build_sections(primary_order)
        secondary_sections = build_sections(
            [key for key in section_values.keys() if key not in primary_order]
        )
        return {
            "headline": response.reason or (response.summary_bullets[0] if response.summary_bullets else ""),
            "phase": response.phase,
            "decision": response.decision,
            "confidence": response.confidence,
            "ui_layout_hint": response.ui_layout_hint,
            "primary_sections": primary_sections,
            "secondary_sections": secondary_sections,
            "has_draft_task": response.draft_task is not None,
            "needs_manual_testing": response.needs_manual_testing,
        }

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

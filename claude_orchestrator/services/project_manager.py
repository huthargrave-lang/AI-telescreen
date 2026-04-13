"""Persistent per-project manager memory and bounded autonomy helpers."""

from __future__ import annotations

from dataclasses import replace
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import (
    JobStatus,
    ProjectManagerDraftTask,
    ProjectManagerEvent,
    ProjectManagerMessage,
    ProjectManagerRecommendation,
    ProjectManagerResponse,
    ProjectManagerSnapshot,
    ProjectManagerState,
    ProjectOperatorFeedback,
    SavedProject,
)
from ..providers import infer_provider
from ..repository import JobRepository
from ..timeutils import utcnow
from ..workspaces import resolve_job_prompt


class ProjectManagerService:
    """Maintains compact project planning memory separate from coding execution backends."""

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
    VALID_AUTONOMY_MODES = {"minimal", "partial", "full"}
    VALID_WORKFLOW_STATES = {
        "idle",
        "awaiting_confirmation",
        "running_current_task",
        "awaiting_continue_decision",
        "blocked_on_manual_test",
    }
    VALID_FEEDBACK_OUTCOMES = {"passed", "failed", "mixed", "blocked"}
    VALID_FEEDBACK_AREAS = {"layout", "actions", "settings", "diagnostics", "integrations", "project-manager", "other"}

    def __init__(
        self,
        repository: JobRepository,
        *,
        recent_event_limit: int = 5,
        retained_event_limit: int = 6,
        recent_message_limit: int = 8,
        retained_message_limit: int = 10,
        recent_feedback_limit: int = 5,
        retained_feedback_limit: int = 8,
        recent_guidance_limit: int = 3,
        retained_guidance_limit: int = 4,
    ) -> None:
        self.repository = repository
        self.recent_event_limit = recent_event_limit
        self.retained_event_limit = max(retained_event_limit, recent_event_limit)
        self.recent_message_limit = recent_message_limit
        self.retained_message_limit = max(retained_message_limit, recent_message_limit)
        self.recent_feedback_limit = recent_feedback_limit
        self.retained_feedback_limit = max(retained_feedback_limit, recent_feedback_limit)
        self.recent_guidance_limit = recent_guidance_limit
        self.retained_guidance_limit = max(retained_guidance_limit, recent_guidance_limit)

    def sync_project(self, project_id: str) -> ProjectManagerState:
        """Ensure manager state exists and reflects the latest saved-project defaults."""

        return self._refresh_state(project_id)

    def compact_manager_state(self, project_id: str) -> ProjectManagerState:
        """Force a deterministic state refresh and event compaction pass."""

        return self._refresh_state(project_id)

    def update_workflow_state(
        self,
        project_id: str,
        *,
        workflow_state: str,
        active_job_id: Optional[str] = None,
        active_autonomy_session_id: Optional[str] = None,
        auto_tasks_run_count: Optional[int] = None,
    ) -> ProjectManagerSnapshot:
        state = self._refresh_state(project_id)
        updated = self._replace_state(
            state,
            workflow_state=self._normalize_workflow_state(workflow_state),
            active_job_id=active_job_id,
            active_autonomy_session_id=active_autonomy_session_id,
            auto_tasks_run_count=(
                state.auto_tasks_run_count
                if auto_tasks_run_count is None
                else max(int(auto_tasks_run_count), 0)
            ),
            updated_at=utcnow(),
        )
        self.repository.upsert_project_manager_state(updated)
        return self.get_snapshot(project_id)

    def get_snapshot(self, project_id: str) -> ProjectManagerSnapshot:
        state = self._refresh_state(project_id)
        recent_events = self.repository.list_project_manager_events(
            project_id,
            limit=self.recent_event_limit,
        )
        recent_messages = list(
            reversed(
                self.repository.list_project_manager_messages(
                    project_id,
                    limit=self.recent_message_limit,
                )
            )
        )
        recent_feedback = self.repository.list_project_operator_feedback(
            project_id,
            limit=self.recent_feedback_limit,
        )
        return ProjectManagerSnapshot(
            state=state,
            recent_events=recent_events,
            recent_messages=recent_messages,
            recent_feedback=recent_feedback,
        )

    def submit_project_manager_message(
        self,
        project_id: str,
        message: str,
        *,
        urgency: Optional[str] = None,
        backend_preference: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> ProjectManagerSnapshot:
        content = message.strip()
        if not content:
            raise ValueError("Project Manager message is required.")
        self.repository.get_saved_project(project_id)
        self._add_manager_message(
            project_id,
            role="operator",
            content=content,
            metadata={
                "message_type": "ask",
                "urgency": self._normalize_urgency(urgency),
                "backend_preference": self._normalize_backend_preference(backend_preference),
                "execution_mode": self._normalize_execution_mode_hint(execution_mode),
            },
        )
        state = self._refresh_state(project_id)
        response = state.latest_response
        if response is not None:
            self._add_manager_message(
                project_id,
                role="manager",
                content=(
                    str(state.display_snapshot.get("conversation_reply") or "").strip()
                    or response.reason
                    or (response.summary_bullets[0] if response.summary_bullets else "")
                ),
                metadata={
                    "message_type": "response",
                    "response": response.to_dict(),
                    "display_snapshot": dict(state.display_snapshot),
                    "decision": response.decision,
                    "phase": response.phase,
                    "confidence": response.confidence,
                },
            )
        return self.get_snapshot(project_id)

    def save_project_manager_advisory(self, project_id: str) -> ProjectManagerSnapshot:
        state = self._refresh_state(project_id)
        if state.latest_response is None:
            raise ValueError("This project does not have a Project Manager response to save yet.")
        guidance_text = self._guidance_text_from_response(state.latest_response)
        state = self._store_project_guidance(project_id, guidance_text)
        self._add_manager_message(
            project_id,
            role="operator",
            content="Stored the latest Project Manager recommendation as project guidance.",
            metadata={
                "message_type": "guidance_save",
                "decision": state.latest_response.decision,
                "reason": state.latest_response.reason,
                "guidance_text": guidance_text,
            },
        )
        return self.get_snapshot(project_id)

    def submit_operator_feedback(
        self,
        project_id: str,
        *,
        outcome: str,
        notes: str,
        severity: Optional[str] = None,
        area: Optional[str] = None,
        screenshot_reference: Optional[str] = None,
        requires_followup: bool = False,
    ) -> ProjectManagerSnapshot:
        self.repository.get_saved_project(project_id)
        normalized_outcome = self._normalize_feedback_outcome(outcome)
        normalized_notes = self._truncate(notes, 1200)
        if not normalized_notes:
            raise ValueError("Operator feedback notes are required.")
        feedback = ProjectOperatorFeedback(
            id=str(uuid.uuid4()),
            project_id=project_id,
            outcome=normalized_outcome,
            notes=normalized_notes,
            severity=self._normalize_urgency(severity),
            area=self._normalize_feedback_area(area),
            screenshot_reference=self._truncate(screenshot_reference, 280),
            requires_followup=bool(requires_followup),
            created_at=utcnow(),
        )
        self.repository.add_project_operator_feedback(feedback)
        state = self._refresh_state(project_id)
        response = state.latest_response
        if response is not None:
            self._add_manager_message(
                project_id,
                role="manager",
                content=(
                    str(state.display_snapshot.get("conversation_reply") or "").strip()
                    or response.reason
                    or (response.summary_bullets[0] if response.summary_bullets else "")
                ),
                metadata={
                    "message_type": "response",
                    "response_source": "operator_feedback",
                    "feedback_outcome": normalized_outcome,
                    "feedback_area": feedback.area,
                    "response": response.to_dict(),
                    "display_snapshot": dict(state.display_snapshot),
                    "decision": response.decision,
                    "phase": response.phase,
                    "confidence": response.confidence,
                },
            )
        return self.get_snapshot(project_id)

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
        recent_messages = self._compact_messages_if_needed(project_id)
        recent_feedback = self._compact_feedback_if_needed(project_id, rolling_facts)
        project_guidance = self._compact_guidance_if_needed(
            list(previous.project_guidance) if previous else [],
            rolling_facts,
        )
        rolling_summary = self._build_rolling_summary(rolling_facts) or rolling_summary
        stable_project_facts = self._build_stable_project_facts(project, previous)
        latest_response = self._build_response(
            project,
            recent_events=recent_events,
            rolling_summary=rolling_summary,
            recent_messages=recent_messages,
            recent_feedback=recent_feedback,
            project_guidance=project_guidance,
        )
        display_snapshot = self._build_display_snapshot(latest_response)
        latest_feedback = self._latest_operator_feedback(recent_feedback)
        latest_feedback_time = latest_feedback.created_at if latest_feedback else None
        latest_request_time = self._latest_operator_request_time(recent_messages)
        display_snapshot["reacting_to_operator_feedback"] = bool(
            latest_feedback_time and (latest_request_time is None or latest_feedback_time >= latest_request_time)
        )
        if latest_feedback is not None:
            display_snapshot["latest_feedback_outcome"] = latest_feedback.outcome
            display_snapshot["latest_feedback_area"] = latest_feedback.area
        recommendation = ProjectManagerRecommendation.from_response(latest_response)
        current_phase = latest_response.phase
        testing_status = self._determine_testing_status(recent_events, latest_response)
        needs_manual_testing = latest_response.needs_manual_testing
        workflow_state = self._refresh_workflow_state(
            project,
            previous=previous,
            latest_response=latest_response,
            needs_manual_testing=needs_manual_testing,
        )
        active_autonomy_session_id = previous.active_autonomy_session_id if previous else None
        active_job_id = previous.active_job_id if previous else None
        auto_tasks_run_count = previous.auto_tasks_run_count if previous else 0
        if active_job_id:
            try:
                active_job = self.repository.get_job(active_job_id)
            except KeyError:
                active_job = None
            if active_job is None or active_job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                active_job_id = None
        if workflow_state != "running_current_task":
            active_job_id = None
        if workflow_state == "idle" and project.autonomy_mode == "minimal" and latest_response.draft_task:
            workflow_state = "awaiting_confirmation"
        self._apply_autonomy_display_snapshot(
            display_snapshot,
            project=project,
            workflow_state=workflow_state,
            active_job_id=active_job_id,
            auto_tasks_run_count=auto_tasks_run_count,
            project_guidance=project_guidance,
            last_guidance_saved_at=(previous.last_guidance_saved_at if previous else None),
            rolling_summary=rolling_summary,
            rolling_facts=rolling_facts,
        )
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
            display_snapshot=display_snapshot,
            latest_recommendation=recommendation,
            latest_recommendation_type=recommendation.decision if recommendation else None,
            latest_recommendation_reason=recommendation.reason if recommendation else None,
            workflow_state=workflow_state,
            active_autonomy_session_id=active_autonomy_session_id,
            active_job_id=active_job_id,
            auto_tasks_run_count=auto_tasks_run_count,
            project_guidance=project_guidance,
            last_guidance_saved_at=previous.last_guidance_saved_at if previous else None,
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

    def _compact_messages_if_needed(self, project_id: str) -> List[ProjectManagerMessage]:
        messages = self.repository.list_project_manager_messages(project_id, limit=None)
        if len(messages) > self.retained_message_limit:
            older_messages = messages[self.retained_message_limit :]
            self.repository.delete_project_manager_messages(
                project_id,
                [message.id for message in older_messages],
            )
            messages = messages[: self.retained_message_limit]
        return list(reversed(messages))

    def _compact_feedback_if_needed(
        self,
        project_id: str,
        rolling_facts: Dict[str, Any],
    ) -> List[ProjectOperatorFeedback]:
        feedback_entries = self.repository.list_project_operator_feedback(project_id, limit=None)
        if len(feedback_entries) > self.retained_feedback_limit:
            older_feedback = feedback_entries[self.retained_feedback_limit :]
            self._merge_feedback_rolling_facts(rolling_facts, older_feedback)
            self.repository.delete_project_operator_feedback(
                project_id,
                [entry.id for entry in older_feedback],
            )
            feedback_entries = feedback_entries[: self.retained_feedback_limit]
        return feedback_entries

    def _compact_guidance_if_needed(
        self,
        project_guidance: List[str],
        rolling_facts: Dict[str, Any],
    ) -> List[str]:
        normalized = self._normalize_bullets(project_guidance, limit=self.retained_guidance_limit + 2)
        if len(normalized) <= self.retained_guidance_limit:
            return normalized
        older_guidance = normalized[self.retained_guidance_limit :]
        rolling_facts["saved_guidance_count"] = int(rolling_facts.get("saved_guidance_count", 0)) + len(older_guidance)
        return normalized[: self.retained_guidance_limit]

    def _store_project_guidance(self, project_id: str, guidance_text: str) -> ProjectManagerState:
        state = self._refresh_state(project_id)
        project_guidance = self._normalize_bullets(
            [guidance_text, *state.project_guidance],
            limit=self.retained_guidance_limit + 1,
        )
        rolling_facts = dict(state.rolling_facts)
        if len(project_guidance) > self.retained_guidance_limit:
            overflow = project_guidance[self.retained_guidance_limit :]
            rolling_facts["saved_guidance_count"] = int(rolling_facts.get("saved_guidance_count", 0)) + len(overflow)
            project_guidance = project_guidance[: self.retained_guidance_limit]
        updated = self._replace_state(
            state,
            project_guidance=project_guidance,
            rolling_facts=rolling_facts,
            last_guidance_saved_at=utcnow(),
            updated_at=utcnow(),
        )
        return self.repository.upsert_project_manager_state(updated)

    def _add_manager_message(
        self,
        project_id: str,
        *,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        message = ProjectManagerMessage(
            id=str(uuid.uuid4()),
            project_id=project_id,
            role=role,
            content=self._truncate(content, 1200) or "",
            metadata=dict(metadata or {}),
            created_at=utcnow(),
        )
        self.repository.add_project_manager_message(message)
        self._compact_messages_if_needed(project_id)

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
                "autonomy_mode": self._normalize_autonomy_mode(project.autonomy_mode),
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

    def _merge_feedback_rolling_facts(
        self,
        rolling_facts: Dict[str, Any],
        older_feedback: Sequence[ProjectOperatorFeedback],
    ) -> None:
        rolling_facts["operator_feedback_count"] = int(rolling_facts.get("operator_feedback_count", 0))
        rolling_facts["feedback_requires_followup_count"] = int(
            rolling_facts.get("feedback_requires_followup_count", 0)
        )
        rolling_facts["feedback_failed_count"] = int(rolling_facts.get("feedback_failed_count", 0))
        rolling_facts["feedback_mixed_count"] = int(rolling_facts.get("feedback_mixed_count", 0))
        rolling_facts["feedback_passed_count"] = int(rolling_facts.get("feedback_passed_count", 0))
        feedback_areas = {
            str(item)
            for item in (rolling_facts.get("feedback_areas") or [])
            if str(item).strip()
        }
        for feedback in older_feedback:
            rolling_facts["operator_feedback_count"] += 1
            if feedback.requires_followup:
                rolling_facts["feedback_requires_followup_count"] += 1
            if feedback.outcome == "failed":
                rolling_facts["feedback_failed_count"] += 1
            elif feedback.outcome == "mixed":
                rolling_facts["feedback_mixed_count"] += 1
            elif feedback.outcome == "passed":
                rolling_facts["feedback_passed_count"] += 1
            if feedback.area:
                feedback_areas.add(feedback.area)
        rolling_facts["feedback_areas"] = list(sorted(feedback_areas))[:4]

    def _build_rolling_summary(self, rolling_facts: Dict[str, Any]) -> Optional[str]:
        if (
            not rolling_facts.get("compacted_event_count")
            and not rolling_facts.get("operator_feedback_count")
            and not rolling_facts.get("saved_guidance_count")
        ):
            return None
        parts = [
            (
                f"Older manager memory covers {rolling_facts.get('compacted_event_count', 0)} earlier outcomes "
                f"({rolling_facts.get('completed_count', 0)} completed, {rolling_facts.get('failed_count', 0)} failed)."
            )
        ]
        if rolling_facts.get("manual_testing_count"):
            parts.append(
                f"Manual verification was requested {rolling_facts['manual_testing_count']} time(s)."
            )
        if rolling_facts.get("operator_feedback_count"):
            parts.append(
                f"Older operator feedback entries: {rolling_facts['operator_feedback_count']} "
                f"(passed {rolling_facts.get('feedback_passed_count', 0)}, mixed {rolling_facts.get('feedback_mixed_count', 0)}, "
                f"failed {rolling_facts.get('feedback_failed_count', 0)})."
            )
        if rolling_facts.get("feedback_requires_followup_count"):
            parts.append(
                f"Operator feedback requested follow-up work {rolling_facts['feedback_requires_followup_count']} time(s)."
            )
        if rolling_facts.get("saved_guidance_count"):
            parts.append(
                f"Saved project guidance has been compacted {rolling_facts['saved_guidance_count']} time(s)."
            )
        feedback_areas = rolling_facts.get("feedback_areas") or []
        if feedback_areas:
            parts.append(f"Common feedback areas: {', '.join(feedback_areas)}.")
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
        recent_messages: Sequence[ProjectManagerMessage],
        recent_feedback: Sequence[ProjectOperatorFeedback],
        project_guidance: Sequence[str],
    ) -> ProjectManagerResponse:
        baseline = self._build_baseline_response(
            project,
            recent_events=recent_events,
            rolling_summary=rolling_summary,
            recent_feedback=recent_feedback,
            project_guidance=project_guidance,
        )
        latest_request = self._latest_operator_request(recent_messages)
        latest_feedback = self._latest_operator_feedback(recent_feedback)
        latest_request_time = self._latest_operator_request_time(recent_messages)
        latest_feedback_time = latest_feedback.created_at if latest_feedback else None
        if latest_feedback is not None and (latest_request_time is None or (latest_feedback_time and latest_feedback_time >= latest_request_time)):
            return self._build_feedback_response(
                project,
                baseline=baseline,
                recent_events=recent_events,
                latest_feedback=latest_feedback,
            )
        if latest_request is not None:
            return self._build_message_response(
                project,
                baseline=baseline,
                recent_events=recent_events,
                rolling_summary=rolling_summary,
                operator_request=latest_request,
            )
        if latest_feedback is not None:
            return self._build_feedback_response(
                project,
                baseline=baseline,
                recent_events=recent_events,
                latest_feedback=latest_feedback,
            )
        return baseline

    def _build_baseline_response(
        self,
        project: SavedProject,
        *,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
        recent_feedback: Sequence[ProjectOperatorFeedback],
        project_guidance: Sequence[str],
    ) -> ProjectManagerResponse:
        if not recent_events:
            latest_feedback = self._latest_operator_feedback(recent_feedback)
            feedback_bullets = self._feedback_summary_bullets(recent_feedback)
            feedback_risks = self._feedback_risk_bullets(recent_feedback)
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="debugging" if latest_feedback and latest_feedback.outcome in {"failed", "mixed"} else "planning",
                    summary_bullets=[
                        f"{project.name} does not have any completed or failed project-linked jobs in manager memory yet.",
                        self._project_defaults_bullet(project),
                        *self._guidance_summary_bullets(project_guidance)[:1],
                        *feedback_bullets[:1],
                        "Launch the first saved-project job to give the manager concrete outcomes to supervise.",
                    ],
                    recent_change_bullets=[],
                    active_focus_bullets=[
                        "Choose the next concrete coding or debugging pass for this project.",
                        "Use the saved project defaults as the starting point so new jobs stay tied to project memory.",
                    ],
                    risks_or_blockers=feedback_risks,
                    decision="launch_followup_job" if latest_feedback and latest_feedback.requires_followup else "wait_for_operator",
                    reason=(
                        "Recent operator feedback is recorded, but the project still needs its first completed or failed project-linked job."
                        if latest_feedback
                        else "No completed or failed project jobs have been ingested yet."
                    ),
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="debugging" if latest_feedback and latest_feedback.outcome in {"failed", "mixed"} else "planning",
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
            project_guidance=project_guidance,
        )
        summary_bullets = self._normalize_bullets(summary_bullets + self._feedback_summary_bullets(recent_feedback), limit=4)
        risks_or_blockers = self._normalize_bullets(risks_or_blockers + self._feedback_risk_bullets(recent_feedback), limit=4)
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

    def _build_message_response(
        self,
        project: SavedProject,
        *,
        baseline: ProjectManagerResponse,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
        operator_request: Dict[str, Optional[str]],
    ) -> ProjectManagerResponse:
        operator_message = str(operator_request.get("content") or "").strip()
        lowered = operator_message.lower()
        urgency = self._normalize_urgency(operator_request.get("urgency"))
        backend_preference = self._normalize_backend_preference(operator_request.get("backend_preference"))
        execution_mode_hint = self._normalize_execution_mode_hint(operator_request.get("execution_mode"))
        latest_summary = recent_events[0].summary if recent_events else {}
        explicit_exploration = self._is_exploration_request(lowered)
        explicit_plan = explicit_exploration or any(
            token in lowered
            for token in (
                "what to do next",
                "what should we do next",
                "next step",
                "plan",
                "brainstorm",
                "review the codebase",
                "read-only",
                "read only",
                "audit",
            )
        )
        explicit_debug = any(
            token in lowered
            for token in ("debug", "broken", "failure", "failing", "error", "regression", "fix")
        )
        explicit_polish = any(
            token in lowered
            for token in (
                "polish",
                "cleanup",
                "wasted space",
                "spacing",
                "format",
                "layout",
                "copy",
                "badge",
                "tooltip",
                "dense",
                "compact",
            )
        )
        explicit_implementation = any(
            token in lowered
            for token in ("implement", "add", "build", "wire", "connect", "launch", "create", "make")
        )
        urgent_only = (
            urgency in {"high", "critical"}
            and len(operator_message.split()) <= 6
            and not any((explicit_plan, explicit_debug, explicit_polish, explicit_implementation))
        )
        execution_mode = execution_mode_hint or self._infer_execution_mode_from_request(
            operator_message,
            baseline,
            latest_summary,
        )
        phase = self._phase_from_request(
            baseline=baseline,
            operator_message=operator_message,
            explicit_plan=explicit_plan,
            explicit_debug=explicit_debug,
            explicit_polish=explicit_polish,
        )
        summary_bullets = self._normalize_bullets(
            [
                self._operator_request_summary_bullet(
                    operator_message,
                    phase=phase,
                    execution_mode=execution_mode,
                ),
                *baseline.summary_bullets,
            ],
            limit=4,
        )
        recent_change_bullets = baseline.recent_change_bullets or self._recent_change_bullets(recent_events)
        risks_or_blockers = list(baseline.risks_or_blockers)
        if urgency in {"high", "critical"}:
            risks_or_blockers.insert(0, f"The operator marked this request as {urgency}.")
        ui_layout_hint = self._layout_hint_for_phase(phase)
        confidence = (
            "high"
            if any((explicit_exploration, explicit_plan, explicit_debug, explicit_polish, explicit_implementation))
            else "medium"
        )
        active_focus_bullets = self._build_active_focus_bullets(
            operator_message=operator_message,
            baseline=baseline,
            execution_mode=execution_mode,
            phase=phase,
        )

        if urgent_only and baseline.draft_task is None and baseline.decision != "launch_followup_job":
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="planning",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Turn the urgent request into a concrete scoped task before launching work.",
                        "Keep the next pass grounded in one specific repo or UI outcome.",
                    ],
                    risks_or_blockers=self._normalize_bullets(
                        risks_or_blockers
                        + ["The request was marked urgent, but it still needs a clearer task scope."],
                        limit=4,
                    ),
                    decision="needs_clarification",
                    reason="The request is urgent, but it still needs one concrete focus before the manager should draft work.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[
                        "What is the single highest-priority outcome for the next pass?",
                        "Should the next pass stay read-only, make safe UI changes, or do a full coding pass?",
                    ],
                    ui_layout_hint="planning",
                    confidence="medium",
                )
            )

        preferred_backend = self._preferred_backend(
            project,
            baseline=baseline,
            latest_summary=latest_summary,
            backend_preference=backend_preference,
        )
        preferred_provider = self._preferred_provider(
            project,
            baseline=baseline,
            latest_summary=latest_summary,
            preferred_backend=preferred_backend,
        )
        draft_task = self._build_operator_draft_task(
            project,
            baseline=baseline,
            latest_summary=latest_summary,
            operator_message=operator_message,
            preferred_backend=preferred_backend,
            preferred_provider=preferred_provider,
            urgency=urgency,
            execution_mode=execution_mode,
        )

        manual_test_checklist = baseline.manual_test_checklist if baseline.needs_manual_testing else []
        if explicit_exploration:
            reason = (
                "The operator asked the manager to start with an exploratory low-risk step, "
                "so the first pass stays read-only."
            )
        elif explicit_plan and execution_mode == "read_only":
            reason = "The operator asked for a planning or audit pass, so the manager drafted a read-only next step."
        elif explicit_debug:
            reason = "The operator asked for a debugging-focused pass, so the manager drafted a targeted follow-up task."
        elif explicit_polish:
            reason = "The operator asked for a polish-focused pass, so the manager drafted a scoped cleanup task."
        elif urgent_only:
            reason = "The operator marked the current recommendation as urgent, so the manager raised its priority."
        else:
            reason = "The operator asked for a concrete next step, so the manager drafted an advisory task instead of auto-launching it."

        if baseline.needs_manual_testing:
            manual_test_checklist = self._normalize_bullets(
                list(baseline.manual_test_checklist)
                + ["Keep the follow-up scoped so the pending manual verification stays easy to run."],
                limit=6,
            )
            risks_or_blockers = self._normalize_bullets(
                risks_or_blockers
                + ["Recent browser-facing work still has a pending manual verification ask."],
                limit=4,
            )

        return self._normalize_response(
            ProjectManagerResponse(
                phase=phase,
                summary_bullets=summary_bullets,
                recent_change_bullets=recent_change_bullets,
                active_focus_bullets=active_focus_bullets,
                risks_or_blockers=risks_or_blockers,
                decision="launch_followup_job",
                reason=reason,
                draft_task=draft_task,
                needs_manual_testing=baseline.needs_manual_testing,
                manual_test_checklist=manual_test_checklist,
                followup_questions=[],
                ui_layout_hint=ui_layout_hint,
                confidence=confidence,
            )
        )

    def _build_feedback_response(
        self,
        project: SavedProject,
        *,
        baseline: ProjectManagerResponse,
        recent_events: Sequence[ProjectManagerEvent],
        latest_feedback: ProjectOperatorFeedback,
    ) -> ProjectManagerResponse:
        latest_summary = recent_events[0].summary if recent_events else {}
        summary_bullets = self._normalize_bullets(
            self._feedback_summary_bullets([latest_feedback]) + baseline.summary_bullets,
            limit=4,
        )
        recent_change_bullets = self._normalize_bullets(
            self._feedback_recent_change_bullets([latest_feedback]) + baseline.recent_change_bullets,
            limit=4,
        )
        risks_or_blockers = self._normalize_bullets(
            self._feedback_risk_bullets([latest_feedback]) + baseline.risks_or_blockers,
            limit=4,
        )
        screenshot_note = (
            f"Screenshot or artifact reference: {latest_feedback.screenshot_reference}."
            if latest_feedback.screenshot_reference
            else None
        )
        if latest_feedback.outcome == "passed" and not latest_feedback.requires_followup:
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="release_ready" if recent_events else "polish",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Recent operator verification passed, so the current phase looks stable.",
                        "Only launch another coding pass if there is a new scoped improvement to make.",
                    ],
                    risks_or_blockers=risks_or_blockers,
                    decision="mark_complete",
                    reason="The latest operator feedback says the recent browser or manual-test flow passed without requiring follow-up work.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[],
                    ui_layout_hint="polish",
                    confidence="high",
                )
            )

        if latest_feedback.outcome == "blocked":
            return self._normalize_response(
                ProjectManagerResponse(
                    phase="blocked",
                    summary_bullets=summary_bullets,
                    recent_change_bullets=recent_change_bullets,
                    active_focus_bullets=[
                        "Resolve the operator-reported blocker before launching another coding pass.",
                        "If the blocker is environmental or permissions-related, use Doctor and project defaults to recover first.",
                    ],
                    risks_or_blockers=self._normalize_bullets(
                        risks_or_blockers + ([screenshot_note] if screenshot_note else []),
                        limit=4,
                    ),
                    decision="wait_for_operator",
                    reason="The latest operator feedback reports the project flow is blocked and needs intervention before another task should run.",
                    draft_task=None,
                    needs_manual_testing=False,
                    manual_test_checklist=[],
                    followup_questions=[
                        "Is the blocker environmental, or should the next pass fix a product issue in the repo?",
                    ],
                    ui_layout_hint="debugging",
                    confidence="high",
                )
            )

        execution_mode = "safe_changes" if latest_feedback.area in {"layout", "actions", "project-manager"} else "coding_pass"
        reason = (
            "The latest operator feedback reported a failed verification and requested follow-up work."
            if latest_feedback.outcome == "failed"
            else "The latest operator feedback was mixed, so the manager drafted a focused follow-up task."
        )
        manual_test_checklist = baseline.manual_test_checklist if baseline.needs_manual_testing else []
        if latest_feedback.requires_followup:
            manual_test_checklist = self._normalize_bullets(
                manual_test_checklist + ["Re-run the operator flow that produced this feedback after the follow-up task lands."],
                limit=6,
            )
        return self._normalize_response(
            ProjectManagerResponse(
                phase="debugging" if latest_feedback.outcome == "failed" else "polish",
                summary_bullets=summary_bullets,
                recent_change_bullets=recent_change_bullets,
                active_focus_bullets=[
                    "Treat the latest operator observation as the source of truth for the next pass.",
                    "Fix the reported issue in the smallest safe scope, then return with clear verification notes.",
                    "Preserve the existing AI Telescreen architecture while addressing the observed problem.",
                ],
                risks_or_blockers=self._normalize_bullets(
                    risks_or_blockers + ([screenshot_note] if screenshot_note else []),
                    limit=4,
                ),
                decision="launch_followup_job",
                reason=reason,
                draft_task=self._build_feedback_draft_task(
                    project,
                    latest_summary=latest_summary,
                    latest_feedback=latest_feedback,
                    execution_mode=execution_mode,
                ),
                needs_manual_testing=bool(latest_feedback.requires_followup or baseline.needs_manual_testing),
                manual_test_checklist=manual_test_checklist,
                followup_questions=[],
                ui_layout_hint="debugging" if latest_feedback.outcome == "failed" else "polish",
                confidence="high",
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
        autonomy_mode = self._normalize_autonomy_mode(project.autonomy_mode)
        return (
            f"Saved defaults currently point at {backend} / {provider}, base branch {branch}, using {workspace_mode} mode, with {autonomy_mode} autonomy."
        )

    def _feedback_summary_bullets(
        self,
        recent_feedback: Sequence[ProjectOperatorFeedback],
    ) -> List[str]:
        if not recent_feedback:
            return []
        latest_feedback = recent_feedback[0]
        area = latest_feedback.area or "operator flow"
        severity = latest_feedback.severity or "normal"
        bullets = [
            f"Latest operator feedback reported {latest_feedback.outcome} results in the {area} area at {severity} severity.",
            latest_feedback.notes,
        ]
        if latest_feedback.screenshot_reference:
            bullets.append(f"Screenshot or artifact reference: {latest_feedback.screenshot_reference}")
        return self._normalize_bullets(bullets, limit=3)

    def _guidance_summary_bullets(self, project_guidance: Sequence[str]) -> List[str]:
        if not project_guidance:
            return []
        latest_guidance = project_guidance[0]
        return self._normalize_bullets(
            [f"Saved project guidance: {latest_guidance}"],
            limit=1,
        )

    def _feedback_recent_change_bullets(
        self,
        recent_feedback: Sequence[ProjectOperatorFeedback],
    ) -> List[str]:
        bullets: List[str] = []
        for feedback in recent_feedback[:3]:
            area = feedback.area or "operator flow"
            severity = feedback.severity or "normal"
            suffix = " and requested follow-up" if feedback.requires_followup else ""
            bullets.append(
                f"Operator reported {feedback.outcome} results in {area} ({severity}){suffix}: {feedback.notes}"
            )
        return self._normalize_bullets(bullets, limit=4)

    def _feedback_risk_bullets(
        self,
        recent_feedback: Sequence[ProjectOperatorFeedback],
    ) -> List[str]:
        risks: List[str] = []
        for feedback in recent_feedback[:3]:
            if feedback.outcome in {"failed", "mixed", "blocked"}:
                risks.append(feedback.notes)
            if feedback.requires_followup:
                risks.append("Operator feedback explicitly requested a follow-up task.")
            if feedback.screenshot_reference:
                risks.append(f"Reference captured: {feedback.screenshot_reference}")
        return self._normalize_bullets(risks, limit=4)

    def _latest_operator_request(
        self,
        recent_messages: Sequence[ProjectManagerMessage],
    ) -> Optional[Dict[str, Optional[str]]]:
        for message in reversed(recent_messages):
            if message.role != "operator":
                continue
            if str(message.metadata.get("message_type") or "ask") != "ask":
                continue
            return {
                "content": message.content,
                "urgency": str(message.metadata.get("urgency") or ""),
                "backend_preference": str(message.metadata.get("backend_preference") or ""),
                "execution_mode": str(message.metadata.get("execution_mode") or ""),
            }
        return None

    def _latest_operator_request_time(
        self,
        recent_messages: Sequence[ProjectManagerMessage],
    ) -> Optional[datetime]:
        for message in reversed(recent_messages):
            if message.role == "operator" and str(message.metadata.get("message_type") or "ask") == "ask":
                return message.created_at
        return None

    def _latest_operator_feedback(
        self,
        recent_feedback: Sequence[ProjectOperatorFeedback],
    ) -> Optional[ProjectOperatorFeedback]:
        return recent_feedback[0] if recent_feedback else None

    def _normalize_feedback_outcome(self, outcome: Optional[str]) -> str:
        value = str(outcome or "").strip().lower()
        if value not in self.VALID_FEEDBACK_OUTCOMES:
            raise ValueError("Feedback outcome must be passed, failed, mixed, or blocked.")
        return value

    def _normalize_feedback_area(self, area: Optional[str]) -> Optional[str]:
        value = str(area or "").strip().lower()
        if not value:
            return None
        if value not in self.VALID_FEEDBACK_AREAS:
            raise ValueError(
                "Feedback area must be layout, actions, settings, diagnostics, integrations, project-manager, or other."
            )
        return value

    def _normalize_urgency(self, urgency: Optional[str]) -> str:
        value = str(urgency or "normal").strip().lower()
        return value if value in {"low", "normal", "high", "critical"} else "normal"

    def _normalize_backend_preference(self, backend_preference: Optional[str]) -> Optional[str]:
        value = str(backend_preference or "").strip()
        if not value or value == "auto":
            return None
        return value

    def _normalize_execution_mode_hint(self, execution_mode: Optional[str]) -> Optional[str]:
        value = str(execution_mode or "").strip()
        if not value:
            return None
        return value if value in self.VALID_EXECUTION_MODES else None

    def _preferred_backend(
        self,
        project: SavedProject,
        *,
        baseline: ProjectManagerResponse,
        latest_summary: Dict[str, Any],
        backend_preference: Optional[str],
    ) -> str:
        if backend_preference:
            return backend_preference
        if baseline.draft_task and baseline.draft_task.backend:
            return baseline.draft_task.backend
        return str(latest_summary.get("backend") or project.default_backend or "messages_api")

    def _preferred_provider(
        self,
        project: SavedProject,
        *,
        baseline: ProjectManagerResponse,
        latest_summary: Dict[str, Any],
        preferred_backend: str,
    ) -> Optional[str]:
        if baseline.draft_task and baseline.draft_task.provider:
            provider = baseline.draft_task.provider
        else:
            provider = str(latest_summary.get("provider") or project.default_provider or "").strip() or None
        expected_provider = infer_provider(preferred_backend)
        if provider and provider.strip().lower() == expected_provider:
            return provider
        return expected_provider

    def _is_exploration_request(self, lowered_message: str) -> bool:
        return any(
            phrase in lowered_message
            for phrase in (
                "review the codebase",
                "read through the codebase",
                "find something to improve",
                "look for something to improve",
                "look for the next best",
                "next best ux fix",
                "next best ui fix",
                "start improving this",
                "start improving the project",
                "review this project",
                "review this repo",
                "audit this project",
            )
        )

    def _phase_from_request(
        self,
        *,
        baseline: ProjectManagerResponse,
        operator_message: str,
        explicit_plan: bool,
        explicit_debug: bool,
        explicit_polish: bool,
    ) -> str:
        lowered = operator_message.lower()
        if self._is_exploration_request(lowered):
            return "planning"
        if explicit_debug:
            return "debugging"
        if explicit_plan and "implement" not in lowered and "fix" not in lowered:
            return "planning"
        if explicit_polish:
            return "polish"
        if baseline.phase in {"blocked", "release_ready"}:
            return "implementation"
        return baseline.phase if baseline.phase in self.VALID_PHASES else "implementation"

    def _layout_hint_for_phase(self, phase: str) -> str:
        if phase in self.VALID_LAYOUT_HINTS:
            return phase
        return "planning"

    def _infer_execution_mode_from_request(
        self,
        operator_message: str,
        baseline: ProjectManagerResponse,
        latest_summary: Dict[str, Any],
    ) -> str:
        lowered = operator_message.lower()
        if self._is_exploration_request(lowered):
            return "read_only"
        if any(token in lowered for token in ("read-only", "read only", "audit", "review", "what to do next", "plan")):
            return "read_only"
        if any(token in lowered for token in ("polish", "cleanup", "wasted space", "layout", "spacing", "format", "tooltip", "badge")):
            return "safe_changes"
        if baseline.draft_task and baseline.draft_task.execution_mode:
            return baseline.draft_task.execution_mode
        return self._infer_execution_mode(latest_summary)

    def _build_active_focus_bullets(
        self,
        *,
        operator_message: str,
        baseline: ProjectManagerResponse,
        execution_mode: str,
        phase: str,
    ) -> List[str]:
        mode_label = {
            "read_only": "Keep this pass read-only and end with a concise next-step plan.",
            "safe_changes": "Keep the changes targeted and safe instead of rewriting working flows.",
            "coding_pass": "Carry the task through implementation and verification in one focused pass.",
        }[execution_mode]
        bullets = [
            self._request_focus_bullet(operator_message, phase=phase),
            mode_label,
            "Preserve the existing AI Telescreen architecture and call out any manual verification still needed.",
        ]
        if phase == "debugging":
            bullets.insert(1, "Start by reproducing the current blocker before making changes.")
        elif phase == "planning":
            bullets.insert(1, "Translate the current project state into one concrete recommended next task.")
        elif phase == "polish":
            bullets.insert(1, "Prioritize readability, layout quality, and operator-facing clarity.")
        if baseline.active_focus_bullets:
            bullets.extend(baseline.active_focus_bullets[:1])
        return self._normalize_bullets(bullets, limit=4)

    def _build_operator_draft_task(
        self,
        project: SavedProject,
        *,
        baseline: ProjectManagerResponse,
        latest_summary: Dict[str, Any],
        operator_message: str,
        preferred_backend: str,
        preferred_provider: Optional[str],
        urgency: str,
        execution_mode: str,
    ) -> ProjectManagerDraftTask:
        priority_map = {"low": -1, "normal": 0, "high": 1, "critical": 2}
        context_line = baseline.reason or (baseline.summary_bullets[0] if baseline.summary_bullets else "")
        scope = [
            "work in the prepared project workspace",
            "preserve the current architecture",
            "make an incremental pass only",
        ]
        if execution_mode == "read_only":
            goal = f"Review the current {project.name} project context and recommend the best next step."
            do_items = [
                f"focus on this operator request: {operator_message}",
                f"use this manager context: {context_line}",
                "inspect the repo read-only and identify the highest-impact next improvement",
                "end with manual-testing notes or blockers if they matter",
            ]
            do_not = [
                "make code changes",
                "rewrite the project plan from scratch",
            ]
        elif "debug" in operator_message.lower() or "fix" in operator_message.lower():
            goal = f"Debug the next scoped issue for {project.name}."
            do_items = [
                f"focus on this operator request: {operator_message}",
                f"use this manager context: {context_line}",
                "reproduce the current issue before changing code",
                "implement the smallest safe fix and end with verification notes",
            ]
            do_not = [
                "broad rewrite",
                "unrelated cleanup",
            ]
        else:
            goal = f"Implement the next scoped pass for {project.name}."
            do_items = [
                f"focus on this operator request: {operator_message}",
                f"use this manager context: {context_line}",
                "make the requested change in the smallest safe scope",
                "end with concise verification notes and any manual-testing ask",
            ]
            do_not = [
                "broad rewrite",
                "unrelated changes",
            ]
        prompt = self._build_compact_task_prompt(
            goal=goal,
            scope=scope,
            do_items=do_items,
            do_not_items=do_not,
            execution_mode=execution_mode,
        )
        rationale_bullets = [
            "This draft was derived from a direct operator request on the project page.",
            f"The manager kept the recommendation advisory and scoped it for {execution_mode.replace('_', ' ')} work.",
        ]
        if urgency in {"high", "critical"}:
            rationale_bullets.append(f"The operator marked this request as {urgency}, so the draft priority was raised.")
        if baseline.needs_manual_testing:
            rationale_bullets.append("Recent browser-facing work still has a manual verification ask, so the next pass should stay scoped.")
        return self._normalize_draft_task(
            ProjectManagerDraftTask(
                prompt=prompt,
                backend=preferred_backend,
                task_type=str(latest_summary.get("task_type") or "code"),
                priority=priority_map.get(urgency, 0),
                use_git_worktree=bool(project.default_use_git_worktree),
                base_branch=project.default_base_branch,
                provider=preferred_provider,
                execution_mode=execution_mode,
                rationale_bullets=rationale_bullets,
                derived_from_operator_message=operator_message,
            )
        )

    def _build_feedback_draft_task(
        self,
        project: SavedProject,
        *,
        latest_summary: Dict[str, Any],
        latest_feedback: ProjectOperatorFeedback,
        execution_mode: str,
    ) -> ProjectManagerDraftTask:
        area = latest_feedback.area or "operator flow"
        do_items = [
            f"address this operator feedback: {latest_feedback.notes}",
            f"treat the observed area as {area}",
            "fix the reported issue in the smallest safe scope",
            "end with concise verification notes and any remaining manual-testing ask",
        ]
        if latest_feedback.screenshot_reference:
            do_items.insert(1, f"use this screenshot or artifact reference: {latest_feedback.screenshot_reference}")
        prompt = self._build_compact_task_prompt(
            goal=f"Follow up on recent operator feedback for {project.name}.",
            scope=[
                "work in the prepared project workspace",
                "preserve the current architecture",
                f"treat the latest feedback outcome as {latest_feedback.outcome}",
            ],
            do_items=do_items,
            do_not_items=[
                "broad rewrite",
                "ignore the operator-observed issue",
            ],
            execution_mode=execution_mode,
        )
        rationale_bullets = [
            "This draft is derived from direct operator feedback captured on the project page.",
            "The manager is reacting to what the operator actually observed instead of only prior job outcomes.",
        ]
        if latest_feedback.requires_followup:
            rationale_bullets.append("The operator explicitly requested a follow-up task.")
        if latest_feedback.screenshot_reference:
            rationale_bullets.append("A screenshot or artifact reference was provided to keep the next pass grounded.")
        priority = 0
        if latest_feedback.severity == "high":
            priority = 1
        elif latest_feedback.severity == "critical":
            priority = 2
        elif latest_feedback.severity == "low":
            priority = -1
        return self._normalize_draft_task(
            ProjectManagerDraftTask(
                prompt=prompt,
                backend=str(latest_summary.get("backend") or project.default_backend or "messages_api"),
                task_type=str(latest_summary.get("task_type") or "code"),
                priority=priority,
                use_git_worktree=bool(project.default_use_git_worktree),
                base_branch=project.default_base_branch,
                provider=str(latest_summary.get("provider") or project.default_provider or "").strip() or None,
                execution_mode=execution_mode,
                rationale_bullets=rationale_bullets,
                derived_from_operator_message=latest_feedback.notes,
            )
        )

    def _summary_bullets(
        self,
        project: SavedProject,
        *,
        recent_events: Sequence[ProjectManagerEvent],
        rolling_summary: Optional[str],
        project_guidance: Sequence[str],
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
        bullets.extend(self._guidance_summary_bullets(project_guidance))
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

    def _replace_state(self, state: ProjectManagerState, **updates: Any) -> ProjectManagerState:
        return replace(state, **updates)

    def _guidance_text_from_response(self, response: ProjectManagerResponse) -> str:
        parts = [response.reason]
        if response.summary_bullets:
            parts.append(response.summary_bullets[0])
        if response.active_focus_bullets:
            parts.append(response.active_focus_bullets[0])
        return self._truncate(" ".join(part for part in parts if part), 220) or response.reason or "Saved project guidance."

    def _normalize_autonomy_mode(self, autonomy_mode: Optional[str]) -> str:
        value = str(autonomy_mode or "minimal").strip().lower()
        return value if value in self.VALID_AUTONOMY_MODES else "minimal"

    def _normalize_workflow_state(self, workflow_state: Optional[str]) -> str:
        value = str(workflow_state or "idle").strip().lower()
        return value if value in self.VALID_WORKFLOW_STATES else "idle"

    def _refresh_workflow_state(
        self,
        project: SavedProject,
        *,
        previous: Optional[ProjectManagerState],
        latest_response: ProjectManagerResponse,
        needs_manual_testing: bool,
    ) -> str:
        workflow_state = self._normalize_workflow_state(previous.workflow_state if previous else "idle")
        active_job_id = previous.active_job_id if previous else None
        if active_job_id:
            try:
                active_job = self.repository.get_job(active_job_id)
            except KeyError:
                active_job = None
            if active_job is not None and active_job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING_RETRY}:
                return "running_current_task"
            workflow_state = "idle" if workflow_state == "running_current_task" else workflow_state
        if needs_manual_testing:
            return "blocked_on_manual_test"
        if self._normalize_autonomy_mode(project.autonomy_mode) == "minimal":
            if latest_response.draft_task and latest_response.decision == "launch_followup_job":
                return "awaiting_confirmation"
            return "idle"
        if workflow_state == "awaiting_continue_decision":
            return workflow_state
        return workflow_state

    def _workflow_helper_text(
        self,
        *,
        autonomy_mode: str,
        workflow_state: str,
        active_job_id: Optional[str],
        auto_tasks_run_count: int,
    ) -> str:
        if workflow_state == "running_current_task":
            if active_job_id:
                return (
                    f"The manager launched a supervised task for this project: {active_job_id}. "
                    "With a worker active, it will report back after the job finishes."
                )
            return "The manager launched the current supervised task and will report back after the coding-agent result is ingested."
        if workflow_state == "awaiting_continue_decision":
            return "The manager finished the current supervised step and is waiting for you to decide whether it should keep going."
        if workflow_state == "blocked_on_manual_test":
            return "The manager is paused until manual testing or operator judgment clears the next step."
        if workflow_state == "awaiting_confirmation":
            if autonomy_mode == "minimal":
                return "Minimal autonomy keeps the manager advisory-only and asks before every task."
            if autonomy_mode == "partial":
                return "Partial autonomy recommends the next supervised step and waits for your go-ahead before it launches work."
            return "The manager drafted the next step and is waiting for operator confirmation before it launches more work."
        if autonomy_mode == "full" and auto_tasks_run_count:
            return "The manager can keep iterating on its own, but it will still stop for manual testing, ambiguity, repeated failure, or the session task limit."
        if autonomy_mode == "partial":
            return "Partial autonomy runs one supervised step at a time, then comes back to you before it continues."
        return "The manager keeps a compact project memory and will wait for the next operator instruction."

    def _build_memory_summary(
        self,
        *,
        project_guidance: Sequence[str],
        rolling_summary: Optional[str],
        rolling_facts: Dict[str, Any],
    ) -> str:
        parts = [
            "The Project Manager keeps one compact project memory. Operator prompts, manager replies, job outcomes, feedback, and saved guidance all feed into it."
        ]
        if project_guidance:
            parts.append(f"Saved project guidance kept ready: {len(project_guidance)}.")
        if rolling_facts.get("compacted_event_count"):
            parts.append(f"Older outcomes summarized: {rolling_facts.get('compacted_event_count', 0)}.")
        if rolling_facts.get("operator_feedback_count"):
            parts.append(f"Older feedback summarized: {rolling_facts.get('operator_feedback_count', 0)}.")
        if rolling_summary:
            parts.append("Recent highlights are already compressed into the rolling summary.")
        return self._truncate(" ".join(parts), 280) or ""

    def _apply_autonomy_display_snapshot(
        self,
        display_snapshot: Dict[str, Any],
        *,
        project: SavedProject,
        workflow_state: str,
        active_job_id: Optional[str],
        auto_tasks_run_count: int,
        project_guidance: Sequence[str],
        last_guidance_saved_at: Optional[datetime],
        rolling_summary: Optional[str],
        rolling_facts: Dict[str, Any],
    ) -> None:
        autonomy_mode = self._normalize_autonomy_mode(project.autonomy_mode)
        display_snapshot["autonomy_mode"] = autonomy_mode
        display_snapshot["workflow_state"] = workflow_state
        display_snapshot["workflow_label"] = workflow_state.replace("_", " ")
        display_snapshot["workflow_helper_text"] = self._workflow_helper_text(
            autonomy_mode=autonomy_mode,
            workflow_state=workflow_state,
            active_job_id=active_job_id,
            auto_tasks_run_count=auto_tasks_run_count,
        )
        display_snapshot["active_job_id"] = active_job_id
        display_snapshot["auto_tasks_run_count"] = auto_tasks_run_count
        display_snapshot["project_guidance"] = list(project_guidance[: self.recent_guidance_limit])
        display_snapshot["last_guidance_saved_at"] = (
            last_guidance_saved_at.isoformat() if last_guidance_saved_at else None
        )
        display_snapshot["memory_summary"] = self._build_memory_summary(
            project_guidance=project_guidance,
            rolling_summary=rolling_summary,
            rolling_facts=rolling_facts,
        )
        if workflow_state == "running_current_task":
            display_snapshot["reply_question"] = "I’m on it now and I’ll report back after the result is in."
            display_snapshot["conversation_reply"] = " ".join(
                part
                for part in [
                    display_snapshot.get("reply_lead"),
                    display_snapshot.get("reply_why"),
                    display_snapshot.get("reply_question"),
                ]
                if part
            ).strip()
        if workflow_state == "awaiting_continue_decision":
            display_snapshot["reply_question"] = "I finished the current step. Want me to keep going?"
            display_snapshot["conversation_reply"] = " ".join(
                part
                for part in [
                    display_snapshot.get("reply_lead"),
                    display_snapshot.get("reply_why"),
                    display_snapshot.get("reply_question"),
                ]
                if part
            ).strip()
        if workflow_state == "blocked_on_manual_test":
            display_snapshot["reply_lead"] = "I’m paused until manual testing is complete."
            display_snapshot["reply_question"] = "Leave feedback here when the check is done."
            display_snapshot["conversation_reply"] = " ".join(
                part
                for part in [
                    display_snapshot.get("reply_lead"),
                    display_snapshot.get("reply_why"),
                    display_snapshot.get("reply_question"),
                ]
                if part
            ).strip()

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
            prompt=self._truncate(draft_task.prompt, 1200, preserve_newlines=True) or "",
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
        reply = self._build_conversational_reply(response)
        draft_preview = self._build_draft_preview(response.draft_task)
        status_badges = self._build_display_badges(response)
        return {
            "headline": reply["lead"],
            "reply_lead": reply["lead"],
            "reply_why": reply["why"],
            "reply_question": reply["question"],
            "conversation_reply": reply["full_text"],
            "recommendation_title": reply["recommendation_title"],
            "recommendation_label": reply["recommendation_label"],
            "phase": response.phase,
            "decision": response.decision,
            "confidence": response.confidence,
            "ui_layout_hint": response.ui_layout_hint,
            "primary_sections": primary_sections,
            "secondary_sections": secondary_sections,
            "has_draft_task": response.draft_task is not None,
            "needs_manual_testing": response.needs_manual_testing,
            "draft_preview": draft_preview,
            "status_badges": status_badges,
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
        return self._build_compact_task_prompt(
            goal=f"Debug the latest failed task for {project.name}.",
            scope=[
                "work in the prepared project workspace",
                "preserve the current architecture",
                "keep the fix incremental",
            ],
            do_items=[
                f"start from this recent task: {recent_prompt}",
                f"investigate this failure: {failure_reason}",
                "reproduce the issue before making changes",
                "implement a focused fix and end with concise verification notes",
            ],
            do_not_items=[
                "broad rewrite",
                "unrelated cleanup",
            ],
            execution_mode="coding_pass",
        )

    def _build_followup_prompt(
        self,
        project: SavedProject,
        summary: Dict[str, Any],
    ) -> str:
        recent_prompt = summary.get("prompt") or "recent project task"
        followup_reason = summary.get("followup_reason") or "continue the project from the latest successful result"
        return self._build_compact_task_prompt(
            goal=f"Continue the next scoped project pass for {project.name}.",
            scope=[
                "work in the prepared project workspace",
                "preserve the current architecture",
                "build on the most recent successful result",
            ],
            do_items=[
                f"start from this recent task: {recent_prompt}",
                f"follow this next-step reason: {followup_reason}",
                "take the next focused coding step",
                "end with concise verification notes and any manual-testing ask",
            ],
            do_not_items=[
                "broad rewrite",
                "restart the work from scratch",
            ],
            execution_mode=self._infer_execution_mode(summary),
        )

    def _truncate(self, value: Optional[str], limit: int, *, preserve_newlines: bool = False) -> Optional[str]:
        if not value:
            return value
        text = str(value).strip()
        if preserve_newlines:
            text = "\n".join(line.rstrip() for line in text.splitlines())
            while "\n\n\n" in text:
                text = text.replace("\n\n\n", "\n\n")
        else:
            text = text.replace("\n", " ")
        if len(text) <= limit:
            return text
        return f"{text[: limit - 1].rstrip()}…"

    def _operator_request_summary_bullet(
        self,
        operator_message: str,
        *,
        phase: str,
        execution_mode: str,
    ) -> str:
        mode_text = {
            "read_only": "read-only review",
            "safe_changes": "small safe pass",
            "coding_pass": "coding pass",
        }.get(execution_mode, "next pass")
        return (
            f"The latest operator request points to a {phase.replace('_', ' ')}-focused {mode_text}: "
            f"{self._truncate(operator_message, 140)}"
        )

    def _request_focus_bullet(self, operator_message: str, *, phase: str) -> str:
        phase_prefix = {
            "planning": "Turn the latest request into one concrete next step",
            "debugging": "Keep the next pass centered on the reported issue",
            "polish": "Keep the next pass centered on the operator-facing cleanup",
        }.get(phase, "Keep the next pass centered on the operator request")
        return f"{phase_prefix}: {self._truncate(operator_message, 140)}"

    def _build_compact_task_prompt(
        self,
        *,
        goal: str,
        scope: Sequence[str],
        do_items: Sequence[str],
        do_not_items: Sequence[str],
        execution_mode: str,
    ) -> str:
        mode_hint = {
            "read_only": "stay read-only",
            "safe_changes": "keep changes safe and tightly scoped",
            "coding_pass": "carry the work through implementation and verification",
        }.get(execution_mode, "keep the work tightly scoped")
        lines = [
            "Goal:",
            goal,
            "",
            "Scope:",
            *[f"- {item}" for item in self._normalize_bullets([*scope, mode_hint], limit=5)],
            "",
            "Do:",
            *[f"- {item}" for item in self._normalize_bullets(do_items, limit=5)],
            "",
            "Do not:",
            *[f"- {item}" for item in self._normalize_bullets(do_not_items, limit=4)],
            "",
            "Output style:",
            "- concise",
            "- report only PLAN / CHANGED / RESULT / TESTS / BLOCKERS / NEXT",
        ]
        return "\n".join(lines)

    def _build_conversational_reply(self, response: ProjectManagerResponse) -> Dict[str, str]:
        draft_task = response.draft_task
        execution_mode = draft_task.execution_mode if draft_task and draft_task.execution_mode else None
        if response.decision == "launch_followup_job":
            if execution_mode == "read_only":
                lead = "Sounds good. I’d start with a read-only review."
                title = "Read-only review"
            elif response.phase == "debugging":
                lead = "I found a focused debugging step to run next."
                title = "Focused debugging pass"
            elif execution_mode == "safe_changes":
                lead = "I found one small safe pass worth running next."
                title = "Small safe pass"
            else:
                lead = "I found the next scoped coding pass."
                title = "Next coding pass"
            question = "Want me to run it?" if draft_task else "Want me to refine it first?"
        elif response.decision == "request_manual_test":
            lead = "I’m pausing here until manual testing is complete."
            title = "Manual verification"
            question = "Want the checklist right here?"
        elif response.decision == "mark_complete":
            lead = "This looks stable enough to treat the current phase as done."
            title = "Current phase looks complete"
            question = "Want to store that as guidance or spin up one more pass?"
        elif response.decision == "needs_clarification":
            lead = "Before I launch work, I’d tighten the scope a bit."
            title = "Clarify the next pass"
            question = response.followup_questions[0] if response.followup_questions else "What single outcome matters most next?"
        else:
            if response.phase == "blocked":
                lead = "I’m holding here until the current blocker is cleared."
                title = "Blocked for now"
            else:
                lead = "I’m holding here until the current workflow is resolved."
                title = "Waiting on the current workflow"
            question = response.followup_questions[0] if response.followup_questions else ""
        why = response.reason or (response.summary_bullets[0] if response.summary_bullets else "")
        full_text = " ".join(part for part in [lead, why, question] if part).strip()
        return {
            "lead": lead,
            "why": why,
            "question": question,
            "full_text": full_text,
            "recommendation_title": title,
            "recommendation_label": title,
        }

    def _build_draft_preview(
        self,
        draft_task: Optional[ProjectManagerDraftTask],
    ) -> Optional[Dict[str, Any]]:
        if draft_task is None:
            return None
        badges = [draft_task.backend, draft_task.task_type, f"priority {draft_task.priority}"]
        if draft_task.execution_mode:
            badges.append(draft_task.execution_mode)
        if draft_task.provider:
            badges.append(draft_task.provider)
        if draft_task.base_branch:
            badges.append(f"base {draft_task.base_branch}")
        if draft_task.use_git_worktree:
            badges.append("worktree")
        summary = {
            "read_only": "Read-only review draft",
            "safe_changes": "Small safe coding draft",
            "coding_pass": "Scoped coding draft",
        }.get(draft_task.execution_mode or "", "Recommended draft task")
        return {
            "summary": summary,
            "badges": badges,
            "prompt_preview": self._truncate(draft_task.prompt, 220),
        }

    def _build_display_badges(self, response: ProjectManagerResponse) -> List[str]:
        badges = [response.phase.replace("_", " ")]
        if response.draft_task is not None:
            badges.append("draft ready")
        if response.needs_manual_testing:
            badges.append("manual testing needed")
        if response.confidence == "high":
            badges.append("high confidence")
        return badges[:4]

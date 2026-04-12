"""Domain models shared by repositories, services, and backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderName(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class BackendName(str, Enum):
    MESSAGES_API = "messages_api"
    AGENT_SDK = "agent_sdk"
    CLAUDE_CODE_CLI = "claude_code_cli"
    MESSAGE_BATCHES = "message_batches"
    CODEX_CLI = "codex_cli"


class RetryDisposition(str, Enum):
    RETRY = "retry"
    FAIL = "fail"
    CANCEL = "cancel"


@dataclass
class EnqueueJobRequest:
    backend: str
    task_type: str
    prompt: str
    provider: Optional[str] = None
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 5
    idempotency_key: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    privacy_mode: Optional[bool] = None


@dataclass
class Job:
    id: str
    created_at: datetime
    updated_at: datetime
    status: JobStatus
    provider: str
    backend: str
    task_type: str
    priority: int
    prompt: str
    metadata: Dict[str, Any]
    attempt_count: int
    next_retry_at: Optional[datetime]
    last_error_code: Optional[str]
    last_error_message: Optional[str]
    max_attempts: int
    idempotency_key: Optional[str]
    model: Optional[str] = None
    workspace_path: Optional[str] = None
    cancel_requested: bool = False
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None


@dataclass
class JobRun:
    id: str
    job_id: str
    started_at: datetime
    ended_at: Optional[datetime]
    provider: str
    backend: str
    request_payload: Dict[str, Any]
    response_summary: Dict[str, Any]
    usage: Dict[str, Any]
    headers: Dict[str, Any]
    error: Dict[str, Any]
    exit_reason: Optional[str]


@dataclass
class ConversationState:
    job_id: str
    system_prompt: Optional[str]
    message_history: List[Dict[str, Any]] = field(default_factory=list)
    compact_summary: Optional[str] = None
    tool_context: Dict[str, Any] = field(default_factory=dict)
    last_checkpoint_at: Optional[datetime] = None


@dataclass
class ArtifactRecord:
    id: Optional[str]
    job_id: str
    created_at: datetime
    kind: str
    path: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulerEventRecord:
    id: Optional[str]
    job_id: str
    timestamp: datetime
    event_type: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobStreamEvent:
    id: str
    job_id: str
    provider: str
    backend: str
    timestamp: datetime
    event_type: str
    phase: Optional[str]
    message: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryDecision:
    disposition: RetryDisposition
    reason: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retry_at: Optional[datetime] = None
    retry_after_seconds: Optional[int] = None
    headers: Dict[str, Any] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendResult:
    status: JobStatus
    output: Optional[str]
    updated_state: Optional[ConversationState]
    usage: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[ArtifactRecord] = field(default_factory=list)
    needs_followup: bool = False
    followup_reason: Optional[str] = None
    request_payload: Dict[str, Any] = field(default_factory=dict)
    response_summary: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    exit_reason: Optional[str] = None


@dataclass
class BatchSubmissionResult:
    batch_id: str
    upstream_batch_id: str
    custom_id_map: Dict[str, str]
    request_payload: Dict[str, Any]


@dataclass
class BatchPollResult:
    upstream_batch_id: str
    status: str
    completed_custom_ids: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    failed_custom_ids: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    still_pending_custom_ids: List[str] = field(default_factory=list)
    headers: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SavedProject:
    id: str
    name: str
    repo_path: str
    default_backend: Optional[str]
    default_provider: Optional[str]
    default_base_branch: Optional[str]
    default_use_git_worktree: bool
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    autonomy_mode: str = "minimal"


@dataclass
class ProjectManagerRecommendation:
    decision: str
    reason: str
    next_task: Optional[Dict[str, Any]] = None
    manual_test_checklist: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "next_task": self.next_task,
            "manual_test_checklist": list(self.manual_test_checklist),
        }

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]) -> Optional["ProjectManagerRecommendation"]:
        if not payload:
            return None
        return cls(
            decision=str(payload.get("decision") or "wait_for_operator"),
            reason=str(payload.get("reason") or ""),
            next_task=payload.get("next_task") if isinstance(payload.get("next_task"), dict) else None,
            manual_test_checklist=[
                str(item)
                for item in (payload.get("manual_test_checklist") or [])
                if str(item).strip()
            ],
        )

    @classmethod
    def from_response(cls, response: Optional["ProjectManagerResponse"]) -> Optional["ProjectManagerRecommendation"]:
        if response is None:
            return None
        return cls(
            decision=response.decision,
            reason=response.reason,
            next_task=response.draft_task.to_dict() if response.draft_task is not None else None,
            manual_test_checklist=list(response.manual_test_checklist),
        )


@dataclass
class ProjectManagerDraftTask:
    prompt: str
    backend: str
    task_type: str
    priority: int
    use_git_worktree: bool
    base_branch: Optional[str] = None
    provider: Optional[str] = None
    execution_mode: Optional[str] = None
    rationale_bullets: List[str] = field(default_factory=list)
    derived_from_operator_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "backend": self.backend,
            "task_type": self.task_type,
            "priority": self.priority,
            "use_git_worktree": self.use_git_worktree,
            "base_branch": self.base_branch,
            "provider": self.provider,
            "execution_mode": self.execution_mode,
            "rationale_bullets": list(self.rationale_bullets),
            "derived_from_operator_message": self.derived_from_operator_message,
        }

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]) -> Optional["ProjectManagerDraftTask"]:
        if not payload:
            return None
        return cls(
            prompt=str(payload.get("prompt") or ""),
            backend=str(payload.get("backend") or "messages_api"),
            task_type=str(payload.get("task_type") or "code"),
            priority=int(payload.get("priority") or 0),
            use_git_worktree=bool(payload.get("use_git_worktree")),
            base_branch=str(payload.get("base_branch")).strip() if payload.get("base_branch") not in (None, "") else None,
            provider=str(payload.get("provider")).strip() if payload.get("provider") not in (None, "") else None,
            execution_mode=(
                str(payload.get("execution_mode")).strip()
                if payload.get("execution_mode") not in (None, "")
                else None
            ),
            rationale_bullets=[str(item) for item in (payload.get("rationale_bullets") or []) if str(item).strip()],
            derived_from_operator_message=str(payload.get("derived_from_operator_message") or ""),
        )


@dataclass
class ProjectManagerResponse:
    phase: str
    summary_bullets: List[str] = field(default_factory=list)
    recent_change_bullets: List[str] = field(default_factory=list)
    active_focus_bullets: List[str] = field(default_factory=list)
    risks_or_blockers: List[str] = field(default_factory=list)
    decision: str = "wait_for_operator"
    reason: str = ""
    draft_task: Optional[ProjectManagerDraftTask] = None
    needs_manual_testing: bool = False
    manual_test_checklist: List[str] = field(default_factory=list)
    followup_questions: List[str] = field(default_factory=list)
    ui_layout_hint: str = "planning"
    confidence: str = "medium"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "summary_bullets": list(self.summary_bullets),
            "recent_change_bullets": list(self.recent_change_bullets),
            "active_focus_bullets": list(self.active_focus_bullets),
            "risks_or_blockers": list(self.risks_or_blockers),
            "decision": self.decision,
            "reason": self.reason,
            "draft_task": self.draft_task.to_dict() if self.draft_task is not None else None,
            "needs_manual_testing": self.needs_manual_testing,
            "manual_test_checklist": list(self.manual_test_checklist),
            "followup_questions": list(self.followup_questions),
            "ui_layout_hint": self.ui_layout_hint,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]) -> Optional["ProjectManagerResponse"]:
        if not payload:
            return None
        return cls(
            phase=str(payload.get("phase") or "planning"),
            summary_bullets=[str(item) for item in (payload.get("summary_bullets") or []) if str(item).strip()],
            recent_change_bullets=[
                str(item) for item in (payload.get("recent_change_bullets") or []) if str(item).strip()
            ],
            active_focus_bullets=[
                str(item) for item in (payload.get("active_focus_bullets") or []) if str(item).strip()
            ],
            risks_or_blockers=[
                str(item) for item in (payload.get("risks_or_blockers") or []) if str(item).strip()
            ],
            decision=str(payload.get("decision") or "wait_for_operator"),
            reason=str(payload.get("reason") or ""),
            draft_task=ProjectManagerDraftTask.from_dict(
                payload.get("draft_task") if isinstance(payload.get("draft_task"), dict) else None
            ),
            needs_manual_testing=bool(payload.get("needs_manual_testing")),
            manual_test_checklist=[
                str(item) for item in (payload.get("manual_test_checklist") or []) if str(item).strip()
            ],
            followup_questions=[
                str(item) for item in (payload.get("followup_questions") or []) if str(item).strip()
            ],
            ui_layout_hint=str(payload.get("ui_layout_hint") or "planning"),
            confidence=str(payload.get("confidence") or "medium"),
        )


@dataclass
class ProjectManagerEvent:
    id: str
    project_id: str
    created_at: datetime
    event_type: str
    source_job_id: Optional[str] = None
    attempt_number: Optional[int] = None
    outcome_status: Optional[str] = None
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectManagerMessage:
    id: str
    project_id: str
    role: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None


@dataclass
class ProjectOperatorFeedback:
    id: str
    project_id: str
    outcome: str
    notes: str
    severity: Optional[str] = None
    area: Optional[str] = None
    screenshot_reference: Optional[str] = None
    requires_followup: bool = False
    created_at: Optional[datetime] = None


@dataclass
class ProjectManagerState:
    project_id: str
    current_phase: str
    summary: str
    stable_project_facts: Dict[str, Any] = field(default_factory=dict)
    testing_status: str = "not_requested"
    needs_manual_testing: bool = False
    rolling_summary: Optional[str] = None
    rolling_facts: Dict[str, Any] = field(default_factory=dict)
    latest_response: Optional[ProjectManagerResponse] = None
    display_snapshot: Dict[str, Any] = field(default_factory=dict)
    latest_recommendation: Optional[ProjectManagerRecommendation] = None
    latest_recommendation_type: Optional[str] = None
    latest_recommendation_reason: Optional[str] = None
    workflow_state: str = "idle"
    active_autonomy_session_id: Optional[str] = None
    active_job_id: Optional[str] = None
    auto_tasks_run_count: int = 0
    project_guidance: List[str] = field(default_factory=list)
    last_guidance_saved_at: Optional[datetime] = None
    last_job_ingested_at: Optional[datetime] = None
    last_compacted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class ProjectManagerSnapshot:
    state: ProjectManagerState
    recent_events: List[ProjectManagerEvent] = field(default_factory=list)
    recent_messages: List[ProjectManagerMessage] = field(default_factory=list)
    recent_feedback: List[ProjectOperatorFeedback] = field(default_factory=list)


@dataclass
class ProjectManagerSessionStatus:
    autonomy_mode: str
    workflow_state: str
    session_headline: str
    session_detail: str
    auto_task_count: int = 0
    waiting_on_operator: bool = False
    waiting_on_worker: bool = False
    needs_manual_testing: bool = False
    active_autonomy_session_id: Optional[str] = None
    active_managed_job_id: Optional[str] = None
    active_managed_job_status: Optional[str] = None
    active_managed_job_headline: Optional[str] = None
    last_manager_activity_at: Optional[datetime] = None
    last_completed_managed_job_id: Optional[str] = None
    last_completed_managed_job_status: Optional[str] = None
    last_failed_managed_job_id: Optional[str] = None
    last_failed_managed_job_status: Optional[str] = None
    current_recommendation: Optional[str] = None
    next_prompt: Optional[str] = None

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

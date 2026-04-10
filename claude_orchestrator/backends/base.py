"""Backend abstraction shared by supported provider integrations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ..config import AppConfig
from ..models import BackendResult, BatchPollResult, BatchSubmissionResult, ConversationState, Job, RetryDecision


@dataclass
class BackendContext:
    config: AppConfig
    workspace_root: Path
    worker_id: str


@runtime_checkable
class BackendAdapter(Protocol):
    """Common interface for durable job execution backends."""

    name: str

    async def submit(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        ...

    async def continue_job(self, job: Job, state: ConversationState, context: BackendContext) -> BackendResult:
        ...

    def classify_error(self, error: Exception, headers: Optional[Dict[str, str]] = None) -> RetryDecision:
        ...

    def can_resume(self, job: Job, state: ConversationState) -> bool:
        ...


@runtime_checkable
class BatchCapableBackend(BackendAdapter, Protocol):
    """Optional protocol for backends that can submit grouped jobs."""

    async def submit_batch(
        self,
        jobs: Sequence[Tuple[Job, ConversationState]],
        context: BackendContext,
    ) -> BatchSubmissionResult:
        ...

    async def poll_batch(
        self,
        upstream_batch_id: str,
        custom_id_map: Dict[str, str],
        context: BackendContext,
    ) -> BatchPollResult:
        ...

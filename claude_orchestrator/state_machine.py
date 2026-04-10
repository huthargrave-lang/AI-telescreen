"""Centralized job-state transition rules."""

from __future__ import annotations

from typing import Dict, Set

from .models import JobStatus

ALLOWED_TRANSITIONS: Dict[JobStatus, Set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.QUEUED,
        JobStatus.WAITING_RETRY,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_RETRY: {
        JobStatus.QUEUED,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.FAILED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.COMPLETED: set(),
    JobStatus.CANCELLED: set(),
}


def ensure_transition(current: JobStatus, target: JobStatus) -> None:
    """Raise if a job transition is not allowed."""

    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid transition: {current.value} -> {target.value}")

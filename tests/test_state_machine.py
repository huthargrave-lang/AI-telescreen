from __future__ import annotations

import pytest

from claude_orchestrator.models import JobStatus
from claude_orchestrator.state_machine import ensure_transition


def test_valid_transitions_are_allowed():
    ensure_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    ensure_transition(JobStatus.RUNNING, JobStatus.WAITING_RETRY)
    ensure_transition(JobStatus.RUNNING, JobStatus.COMPLETED)
    ensure_transition(JobStatus.FAILED, JobStatus.QUEUED)


def test_self_transition_is_allowed_for_requeue_bookkeeping():
    ensure_transition(JobStatus.WAITING_RETRY, JobStatus.WAITING_RETRY)


def test_invalid_transition_raises():
    with pytest.raises(ValueError):
        ensure_transition(JobStatus.COMPLETED, JobStatus.QUEUED)

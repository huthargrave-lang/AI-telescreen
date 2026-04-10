from __future__ import annotations

from claude_orchestrator.models import RetryDecision, RetryDisposition
from claude_orchestrator.repository import JobRepository
from claude_orchestrator.services.orchestrator import OrchestratorService
from datetime import timedelta

from claude_orchestrator.timeutils import to_iso8601, utcnow
from tests.helpers import build_test_orchestrator, create_job


class ResumeBackend:
    name = "messages_api"

    async def submit(self, job, state, context):  # pragma: no cover - not used here.
        raise AssertionError("submit should not be called")

    async def continue_job(self, job, state, context):  # pragma: no cover - not used here.
        raise AssertionError("continue should not be called")

    def classify_error(self, error, headers=None):
        return RetryDecision(RetryDisposition.FAIL, "unused")

    def can_resume(self, job, state):
        return True


class RestartRetryBackend(ResumeBackend):
    def can_resume(self, job, state):
        return False


def _expire_running_job(repository: JobRepository, job_id: str) -> None:
    stale_time = utcnow() - timedelta(hours=1)
    with repository.db.connect() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
            (
                to_iso8601(stale_time),
                to_iso8601(stale_time),
                job_id,
            ),
        )


def test_stale_running_job_is_requeued_when_backend_can_resume(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path, backends={"messages_api": ResumeBackend()})
    job = create_job(orchestrator)
    claimed = repository.claim_job(job.id, "worker-1")
    assert claimed is not None
    _expire_running_job(repository, job.id)

    recovered = orchestrator.recover_stale_jobs()

    assert job.id in recovered
    assert repository.get_job(job.id).status.value == "queued"


def test_stale_running_job_waits_for_retry_when_resume_is_not_possible(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path, backends={"messages_api": RestartRetryBackend()})
    job = create_job(orchestrator)
    claimed = repository.claim_job(job.id, "worker-1")
    assert claimed is not None
    _expire_running_job(repository, job.id)

    recovered = orchestrator.recover_stale_jobs()
    updated = repository.get_job(job.id)

    assert job.id in recovered
    assert updated.status.value == "waiting_retry"
    assert updated.next_retry_at is not None

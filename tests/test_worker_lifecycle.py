from __future__ import annotations

import asyncio

from claude_orchestrator.models import JobStatus
from claude_orchestrator.services.worker import WorkerService
from tests.helpers import SleepBackend, build_test_orchestrator, create_job


def test_lease_heartbeat_prevents_false_stale_recovery(tmp_path):
    backend = SleepBackend(0.2)
    orchestrator, repository, config = build_test_orchestrator(tmp_path, backends={"messages_api": backend})
    config.worker.lease_seconds = 0.1
    config.worker.heartbeat_interval_seconds = 0.02

    job = create_job(orchestrator)
    claimed = repository.claim_job(job.id, "worker-1", lease_seconds=config.effective_lease_seconds())
    assert claimed is not None

    async def scenario():
        task = asyncio.create_task(orchestrator.process_claimed_job(claimed, "worker-1"))
        await backend.started.wait()
        await asyncio.sleep(0.14)
        assert orchestrator.recover_stale_jobs() == []
        await task

    asyncio.run(scenario())

    assert repository.get_job(job.id).status == JobStatus.COMPLETED


def test_worker_shutdown_interrupts_running_job_and_stops_new_claims(tmp_path):
    backend = SleepBackend(1.0)
    orchestrator, repository, config = build_test_orchestrator(tmp_path, backends={"messages_api": backend})
    config.worker.max_concurrency = 1
    config.worker.poll_interval_seconds = 0.01
    config.worker.shutdown_drain_timeout_seconds = 0.05
    config.worker.shutdown_poll_interval_seconds = 0.01
    config.worker.lease_seconds = 0.1
    config.worker.heartbeat_interval_seconds = 0.02

    job_one = create_job(orchestrator, prompt="first")
    job_two = create_job(orchestrator, prompt="second")
    worker = WorkerService(repository, orchestrator)

    async def scenario():
        task = asyncio.create_task(worker.run_forever())
        await backend.started.wait()
        worker.shutdown()
        await task

    asyncio.run(scenario())

    assert backend.cancelled is True
    assert repository.get_job(job_one.id).status == JobStatus.WAITING_RETRY
    assert repository.get_job(job_two.id).status == JobStatus.QUEUED

"""Worker loop and scheduler coordination."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Dict, List, Optional, Set

from ..models import Job
from ..repository import JobRepository
from .orchestrator import OrchestratorService


class WorkerService:
    """Claims runnable jobs, enforces concurrency, and drives retry polling."""

    def __init__(self, repository: JobRepository, orchestrator: OrchestratorService) -> None:
        self.repository = repository
        self.orchestrator = orchestrator
        self.worker_id = f"worker-{uuid.uuid4()}"
        self._shutdown = False

    async def run_forever(self, *, run_once: bool = False) -> None:
        self.orchestrator.recover_stale_jobs()
        active: Set[asyncio.Task] = set()
        active_backends: Counter = Counter()
        while not self._shutdown:
            self.orchestrator.retry_due()
            await self.orchestrator.poll_open_batches(self.worker_id)

            finished = {task for task in active if task.done()}
            for task in finished:
                active.remove(task)
                backend_name = getattr(task, "backend_name", None)
                if backend_name:
                    active_backends[backend_name] -= 1
                    if active_backends[backend_name] <= 0:
                        del active_backends[backend_name]
                try:
                    await task
                except Exception:
                    # Individual job failures are already persisted per job; keep the worker alive.
                    pass

            while len(active) < self.orchestrator.config.worker.max_concurrency:
                job = self._claim_next_job(active_backends)
                if job is None:
                    break
                if job.backend == "message_batches":
                    jobs = self._claim_batch_group(job)
                    task = asyncio.create_task(self.orchestrator.submit_message_batch(jobs, self.worker_id))
                    task.backend_name = job.backend  # type: ignore[attr-defined]
                    active.add(task)
                    active_backends[job.backend] += 1
                    continue
                task = asyncio.create_task(self.orchestrator.process_claimed_job(job, self.worker_id))
                task.backend_name = job.backend  # type: ignore[attr-defined]
                active.add(task)
                active_backends[job.backend] += 1

            if run_once and not active:
                break
            await asyncio.sleep(self.orchestrator.config.worker.poll_interval_seconds)

    def shutdown(self) -> None:
        self._shutdown = True

    def _claim_next_job(self, active_backends: Counter) -> Optional[Job]:
        candidates = self.repository.list_runnable_jobs(limit=self.orchestrator.config.worker.max_concurrency * 5)
        for candidate in candidates:
            limit = self.orchestrator.config.worker.per_backend_concurrency.get(
                candidate.backend,
                self.orchestrator.config.worker.max_concurrency,
            )
            if active_backends[candidate.backend] >= limit:
                continue
            claimed = self.repository.claim_job(candidate.id, self.worker_id)
            if claimed:
                return claimed
        return None

    def _claim_batch_group(self, anchor: Job) -> List[Job]:
        group = [anchor]
        limit = min(
            self.orchestrator.config.worker.max_batch_group_size,
            self.orchestrator.config.backends.message_batches.max_batch_size,
        )
        for candidate in self.repository.list_batch_candidates(anchor, limit=limit):
            if candidate.id == anchor.id:
                continue
            claimed = self.repository.claim_job(candidate.id, self.worker_id)
            if claimed:
                group.append(claimed)
            if len(group) >= limit:
                break
        return group

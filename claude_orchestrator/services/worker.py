"""Worker loop and scheduler coordination."""

from __future__ import annotations

import asyncio
import signal
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
        self._shutdown_requested = False
        self._wake_event = asyncio.Event()

    async def run_forever(self, *, run_once: bool = False) -> None:
        self.orchestrator.recover_stale_jobs()
        active: Set[asyncio.Task] = set()
        active_backends: Counter = Counter()
        signal_handlers = self._install_signal_handlers()
        shutdown_deadline: Optional[float] = None
        try:
            while True:
                await self._collect_finished(active, active_backends)
                if self._shutdown_requested:
                    if shutdown_deadline is None:
                        shutdown_deadline = asyncio.get_running_loop().time() + self.orchestrator.config.worker.shutdown_drain_timeout_seconds
                    if not active:
                        break
                    if asyncio.get_running_loop().time() >= shutdown_deadline:
                        await self._cancel_active_tasks(active, active_backends)
                        break
                    await self._sleep_or_wakeup(self.orchestrator.config.worker.shutdown_poll_interval_seconds)
                    continue

                self.orchestrator.retry_due()
                await self.orchestrator.poll_open_batches(self.worker_id)

                while len(active) < self.orchestrator.config.worker.max_concurrency and not self._shutdown_requested:
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
                await self._sleep_or_wakeup(self.orchestrator.config.worker.poll_interval_seconds)
        finally:
            self._remove_signal_handlers(signal_handlers)

    def shutdown(self) -> None:
        self._shutdown_requested = True
        self._wake_event.set()

    def _claim_next_job(self, active_backends: Counter) -> Optional[Job]:
        candidates = self.repository.list_runnable_jobs(
            limit=self.orchestrator.config.worker.max_concurrency * 5,
            continuation_priority_bonus=self.orchestrator.config.worker.continuation_priority_bonus,
        )
        for candidate in candidates:
            if candidate.backend not in self.orchestrator.backends:
                continue
            limit = self.orchestrator.config.worker.per_backend_concurrency.get(
                candidate.backend,
                self.orchestrator.config.worker.max_concurrency,
            )
            if active_backends[candidate.backend] >= limit:
                continue
            claimed = self.repository.claim_job(
                candidate.id,
                self.worker_id,
                lease_seconds=self.orchestrator.config.effective_lease_seconds(),
            )
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
            claimed = self.repository.claim_job(
                candidate.id,
                self.worker_id,
                lease_seconds=self.orchestrator.config.effective_lease_seconds(),
            )
            if claimed:
                group.append(claimed)
            if len(group) >= limit:
                break
        return group

    async def _collect_finished(self, active: Set[asyncio.Task], active_backends: Counter) -> None:
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
            except asyncio.CancelledError:
                pass
            except Exception:
                # Individual job failures are already persisted per job; keep the worker alive.
                pass

    async def _cancel_active_tasks(self, active: Set[asyncio.Task], active_backends: Counter) -> None:
        for task in list(active):
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await self._collect_finished(active, active_backends)

    def _install_signal_handlers(self) -> List[tuple[int, object]]:
        loop = asyncio.get_running_loop()
        installed: List[tuple[int, object]] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.shutdown)
                installed.append((signum, self.shutdown))
            except (NotImplementedError, RuntimeError):
                continue
        return installed

    def _remove_signal_handlers(self, handlers: List[tuple[int, object]]) -> None:
        loop = asyncio.get_running_loop()
        for signum, _ in handlers:
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                continue

    async def _sleep_or_wakeup(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        finally:
            self._wake_event.clear()

from __future__ import annotations

import asyncio

from claude_orchestrator.backends.message_batches import MessageBatchesBackend
from tests.helpers import build_test_orchestrator, create_job


class FakeBatchesApi:
    def __init__(self):
        self.requests = []
        self.custom_ids = []
        self.retrieve_calls = 0

    async def create(self, requests):
        self.requests.append(requests)
        self.custom_ids = [request["custom_id"] for request in requests]
        return {"id": "batch-upstream-1"}

    async def retrieve(self, upstream_batch_id):
        assert upstream_batch_id == "batch-upstream-1"
        self.retrieve_calls += 1
        if self.retrieve_calls == 1:
            return {
                "status": "in_progress",
                "results": [
                    {
                        "custom_id": self.custom_ids[0],
                        "result": {
                            "message": {
                                "content": [{"type": "text", "text": "ok"}],
                                "usage": {"input_tokens": 1, "output_tokens": 2},
                                "stop_reason": "end_turn",
                            }
                        },
                    },
                    {
                        "custom_id": self.custom_ids[1],
                        "result": {
                            "type": "error",
                            "error": "boom",
                        },
                    },
                ],
            }
        return {
            "status": "completed",
            "results": [
                {
                    "custom_id": self.custom_ids[2],
                    "result": {
                        "message": {
                            "content": [{"type": "text", "text": "later ok"}],
                            "usage": {"input_tokens": 2, "output_tokens": 3},
                            "stop_reason": "end_turn",
                        }
                    },
                }
            ],
        }


class FakeMessagesApi:
    def __init__(self):
        self.batches = FakeBatchesApi()


class FakeClient:
    def __init__(self):
        self.messages = FakeMessagesApi()


def test_partial_batch_results_fan_back_into_individual_jobs(tmp_path):
    fake_client = FakeClient()
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    orchestrator.backends["message_batches"] = MessageBatchesBackend(orchestrator.config, client_factory=lambda: fake_client)

    job_one = create_job(orchestrator, backend="message_batches", prompt="one", model="test-model")
    job_two = create_job(orchestrator, backend="message_batches", prompt="two", model="test-model")
    job_three = create_job(orchestrator, backend="message_batches", prompt="three", model="test-model")

    claimed_jobs = [
        repository.claim_job(job_one.id, "worker-1"),
        repository.claim_job(job_two.id, "worker-1"),
        repository.claim_job(job_three.id, "worker-1"),
    ]

    asyncio.run(orchestrator.submit_message_batch([job for job in claimed_jobs if job is not None], "worker-1"))
    asyncio.run(orchestrator.poll_open_batches("worker-1"))
    assert repository.get_job(job_three.id).status.value == "waiting_retry"

    asyncio.run(orchestrator.poll_open_batches("worker-1"))

    assert repository.get_job(job_one.id).status.value == "completed"
    assert repository.get_job(job_two.id).status.value == "failed"
    assert repository.get_job(job_three.id).status.value == "completed"

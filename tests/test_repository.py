from __future__ import annotations

from tests.helpers import build_test_orchestrator, create_job


def test_sqlite_persistence_round_trip(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    created = create_job(orchestrator, prompt="persist me")
    loaded = repository.get_job(created.id)
    state = repository.get_conversation_state(created.id)

    assert loaded.prompt == "persist me"
    assert state.job_id == created.id
    assert state.message_history == []


def test_duplicate_execution_is_prevented(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    job = create_job(orchestrator)

    first_claim = repository.claim_job(job.id, "worker-1")
    second_claim = repository.claim_job(job.id, "worker-2")

    assert first_claim is not None
    assert second_claim is None
    assert repository.get_job(job.id).attempt_count == 1

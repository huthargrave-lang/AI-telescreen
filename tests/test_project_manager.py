from __future__ import annotations

import asyncio

from claude_orchestrator.models import RetryDecision, RetryDisposition
from tests.helpers import build_test_orchestrator


class FailingBackend:
    name = "messages_api"

    async def submit(self, job, state, context):
        raise RuntimeError("UI smoke test is still failing")

    async def continue_job(self, job, state, context):
        return await self.submit(job, state, context)

    def classify_error(self, error, headers=None):
        return RetryDecision(
            disposition=RetryDisposition.FAIL,
            reason="ui_regression_detected",
            error_code="ui_regression_detected",
            error_message=str(error),
        )

    def can_resume(self, job, state):
        return False


def test_project_manager_state_is_created_for_saved_project(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="AI Telescreen Repo",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="primary project",
    )

    snapshot = orchestrator.get_project_manager_snapshot(project.id)
    loaded = repository.get_project_manager_state(project.id)

    assert loaded is not None
    assert snapshot.state.current_phase == "planning"
    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_recommendation is not None
    assert snapshot.state.latest_recommendation.decision == "wait_for_operator"
    assert snapshot.state.latest_response.phase == "planning"
    assert snapshot.state.latest_response.ui_layout_hint == "planning"
    assert snapshot.state.display_snapshot["ui_layout_hint"] == "planning"
    assert loaded.stable_project_facts["repo_path"] == str(tmp_path.resolve())


def test_project_manager_ingests_completed_jobs_and_requests_manual_testing(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Browser UX",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="browser-first project",
    )
    job = orchestrator.launch_job_from_project(
        project.id,
        prompt="Refine the dashboard job table layout and browser badges.",
        task_type="code",
    )

    processed = asyncio.run(orchestrator.run_job_now(job.id, worker_id="worker-project-manager"))
    snapshot = orchestrator.get_project_manager_snapshot(project.id)

    assert processed.status.value == "completed"
    assert snapshot.state.current_phase == "polish"
    assert snapshot.state.needs_manual_testing is True
    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_recommendation is not None
    assert snapshot.state.latest_recommendation.decision == "request_manual_test"
    assert snapshot.state.latest_response.decision == "request_manual_test"
    assert snapshot.state.latest_response.manual_test_checklist
    assert snapshot.state.latest_response.ui_layout_hint == "polish"
    assert snapshot.recent_events[0].outcome_status == "completed"
    assert snapshot.recent_events[0].summary["manual_testing_needed"] is True
    assert repository.get_project_manager_state(project.id) is not None


def test_project_manager_ingests_failed_jobs_and_recommends_followup(tmp_path):
    orchestrator, _, _ = build_test_orchestrator(
        tmp_path,
        backends={"messages_api": FailingBackend()},
    )
    project = orchestrator.create_saved_project(
        name="Failure Case",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )
    job = orchestrator.launch_job_from_project(
        project.id,
        prompt="Fix the broken browser regression.",
        task_type="code",
    )

    processed = asyncio.run(orchestrator.run_job_now(job.id, worker_id="worker-project-failure"))
    snapshot = orchestrator.get_project_manager_snapshot(project.id)

    assert processed.status.value == "failed"
    assert snapshot.state.current_phase == "debugging"
    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_recommendation is not None
    assert snapshot.state.latest_recommendation.decision == "launch_followup_job"
    assert snapshot.state.latest_response.draft_task is not None
    assert snapshot.state.latest_response.ui_layout_hint == "debugging"
    assert "failed" in snapshot.state.summary.lower()
    assert snapshot.recent_events[0].summary["error_summary"] == "UI smoke test is still failing"


def test_project_manager_compacts_older_events(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Compaction",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )

    for index in range(8):
        job = orchestrator.launch_job_from_project(
            project.id,
            prompt=f"Implement backend cleanup step {index}",
            task_type="code",
        )
        asyncio.run(orchestrator.run_job_now(job.id, worker_id=f"worker-{index}"))

    snapshot = orchestrator.get_project_manager_snapshot(project.id)
    stored_events = repository.list_project_manager_events(project.id, limit=None)

    assert len(snapshot.recent_events) <= 5
    assert len(stored_events) <= 6
    assert snapshot.state.last_compacted_at is not None
    assert snapshot.state.rolling_summary is not None
    assert "Older manager memory" in snapshot.state.rolling_summary
    assert snapshot.state.display_snapshot["primary_sections"]


def test_project_manager_submit_message_creates_structured_response_and_history(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Interactive Manager",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=True,
        notes="interactive planning",
    )

    snapshot = orchestrator.submit_project_manager_message(
        project.id,
        "Review the codebase and tell me what to do next.",
        urgency="normal",
        backend_preference="messages_api",
        execution_mode="read_only",
    )
    stored_messages = repository.list_project_manager_messages(project.id, limit=None)

    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_response.decision == "launch_followup_job"
    assert snapshot.state.latest_response.draft_task is not None
    assert snapshot.state.latest_response.draft_task.execution_mode == "read_only"
    assert snapshot.state.latest_response.draft_task.backend == "messages_api"
    assert [message.role for message in snapshot.recent_messages][-2:] == ["operator", "manager"]
    assert len(stored_messages) == 2
    assert stored_messages[0].role == "manager"
    assert stored_messages[1].role == "operator"


def test_project_manager_message_history_is_bounded(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Bounded History",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )

    for index in range(7):
        snapshot = orchestrator.submit_project_manager_message(
            project.id,
            f"Plan follow-up pass {index}",
            urgency="normal",
            execution_mode="read_only",
        )

    stored_messages = repository.list_project_manager_messages(project.id, limit=None)

    assert len(stored_messages) <= 10
    assert snapshot.recent_messages[-1].role == "manager"
    assert any("Plan follow-up pass 6" in message.content for message in snapshot.recent_messages)


def test_project_manager_save_advisory_records_message(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Advisory Save",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )

    orchestrator.submit_project_manager_message(
        project.id,
        "Plan the next UI cleanup pass.",
        urgency="high",
        execution_mode="safe_changes",
    )
    snapshot = orchestrator.save_project_manager_advisory(project.id)
    stored_messages = repository.list_project_manager_messages(project.id, limit=None)

    assert snapshot.state.latest_response is not None
    assert stored_messages[0].metadata["message_type"] == "advisory_save"
    assert stored_messages[0].role == "operator"

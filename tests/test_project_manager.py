from __future__ import annotations

import asyncio
from pathlib import Path

from claude_orchestrator.models import BackendResult, ConversationState, JobStatus, RetryDecision, RetryDisposition
from claude_orchestrator.timeutils import utcnow
from tests.helpers import CompletedBackend, build_test_orchestrator


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


class FollowupBackend:
    name = "messages_api"

    async def submit(self, job, state, context):
        updated_state = ConversationState(
            job_id=job.id,
            system_prompt=state.system_prompt,
            message_history=state.message_history + [{"role": "assistant", "content": "implemented follow-up step"}],
            compact_summary="implemented follow-up step",
            tool_context=dict(state.tool_context),
            last_checkpoint_at=utcnow(),
        )
        return BackendResult(
            status=JobStatus.COMPLETED,
            output="implemented follow-up step",
            updated_state=updated_state,
            request_payload={"job_id": job.id},
            response_summary={"summary": "implemented one focused project step"},
            needs_followup=True,
            followup_reason="The next likely cleanup is the job action area.",
            exit_reason="completed",
        )

    async def continue_job(self, job, state, context):
        return await self.submit(job, state, context)

    def classify_error(self, error, headers=None):
        return RetryDecision(
            disposition=RetryDisposition.FAIL,
            reason="followup_backend_error",
            error_code="followup_backend_error",
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
    assert snapshot.state.display_snapshot["reply_lead"] == "Sounds good. I’d start with a read-only review."
    assert "PLAN / CHANGED / RESULT / TESTS / BLOCKERS / NEXT" in snapshot.state.latest_response.draft_task.prompt
    assert "Goal:\n" in snapshot.state.latest_response.draft_task.prompt
    assert [message.role for message in snapshot.recent_messages][-2:] == ["operator", "manager"]
    assert len(stored_messages) == 2
    assert stored_messages[0].role == "manager"
    assert stored_messages[1].role == "operator"


def test_project_manager_minimal_autonomy_waits_for_confirmation(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Minimal Autonomy",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="minimal",
        notes=None,
    )

    snapshot = orchestrator.submit_project_manager_message(
        project.id,
        "Find something to improve.",
        urgency="normal",
        execution_mode="safe_changes",
    )

    assert snapshot.state.workflow_state == "awaiting_confirmation"
    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_response.draft_task is not None
    assert repository.list_project_jobs(project.id, limit=10) == []
    assert "asks before every task" in snapshot.state.display_snapshot["workflow_helper_text"]


def test_project_manager_partial_autonomy_launches_then_waits_for_continue(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(
        tmp_path,
        backends={"messages_api": FollowupBackend()},
    )
    project = orchestrator.create_saved_project(
        name="Partial Autonomy",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="partial",
        notes=None,
    )

    snapshot = orchestrator.submit_project_manager_message(
        project.id,
        "Find something to improve.",
        urgency="normal",
        execution_mode="safe_changes",
    )
    assert snapshot.state.workflow_state == "awaiting_confirmation"
    assert repository.list_project_jobs(project.id, limit=10) == []

    auto_job = orchestrator.launch_job_from_project_manager_draft(project.id)
    processed = asyncio.run(orchestrator.run_job_now(auto_job.id, worker_id="worker-partial-autonomy"))
    refreshed = orchestrator.get_project_manager_snapshot(project.id)

    assert auto_job.metadata["project_manager_auto_launched"] is True
    assert auto_job.metadata["project_manager_launch_source"] == "operator_confirmed"
    assert processed.status == JobStatus.COMPLETED
    assert refreshed.state.workflow_state == "awaiting_continue_decision"
    assert refreshed.state.latest_response is not None
    assert refreshed.state.latest_response.decision == "launch_followup_job"
    assert refreshed.state.latest_response.draft_task is not None
    assert refreshed.state.auto_tasks_run_count == 1
    assert refreshed.state.display_snapshot["reply_question"] == "I finished the current step. Want me to keep going?"


def test_project_manager_full_autonomy_launches_next_step_until_stop_condition(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(
        tmp_path,
        backends={"messages_api": FollowupBackend()},
    )
    project = orchestrator.create_saved_project(
        name="Full Autonomy",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="full",
        notes=None,
    )

    snapshot = orchestrator.submit_project_manager_message(
        project.id,
        "Find something to improve.",
        urgency="normal",
        execution_mode="safe_changes",
    )
    first_job = repository.list_project_jobs(project.id, limit=10)[0]
    asyncio.run(orchestrator.run_job_now(first_job.id, worker_id="worker-full-autonomy"))
    refreshed = orchestrator.get_project_manager_snapshot(project.id)
    project_jobs = repository.list_project_jobs(project.id, limit=10)

    assert snapshot.state.workflow_state == "running_current_task"
    assert refreshed.state.workflow_state in {"running_current_task", "awaiting_confirmation"}
    assert refreshed.state.auto_tasks_run_count >= 2
    assert len(project_jobs) >= 2
    assert project_jobs[0].id != first_job.id
    assert project_jobs[0].metadata["project_manager_auto_launched"] is True


def test_project_manager_full_autonomy_auto_starts_read_only_exploration_with_auto_agent(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(
        tmp_path,
        backends={"codex_cli": CompletedBackend(), "messages_api": CompletedBackend()},
    )
    project = orchestrator.create_saved_project(
        name="Exploration Autonomy",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="full",
        notes=None,
    )

    snapshot = orchestrator.submit_project_manager_message(
        project.id,
        "Review the codebase and tell me what to do next.",
        urgency="normal",
        backend_preference="auto",
    )
    project_jobs = repository.list_project_jobs(project.id, limit=10)

    assert snapshot.state.workflow_state == "running_current_task"
    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_response.draft_task is not None
    assert snapshot.state.latest_response.draft_task.execution_mode == "read_only"
    assert snapshot.state.latest_response.draft_task.backend == "codex_cli"
    assert project_jobs[0].backend == "codex_cli"
    assert project_jobs[0].metadata["execution_mode"] == "read_only"


def test_project_manager_followup_message_does_not_launch_overlapping_task(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="No Overlap",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="full",
        notes=None,
    )

    first = orchestrator.submit_project_manager_message(
        project.id,
        "Find something to improve.",
        urgency="normal",
        execution_mode="safe_changes",
    )
    second = orchestrator.submit_project_manager_message(
        project.id,
        "Also watch for wasted space on the page.",
        urgency="normal",
        execution_mode="safe_changes",
    )
    project_jobs = repository.list_project_jobs(project.id, limit=10)

    assert first.state.workflow_state == "running_current_task"
    assert second.state.workflow_state == "running_current_task"
    assert len(project_jobs) == 1


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
    assert stored_messages[0].metadata["message_type"] == "guidance_save"
    assert stored_messages[0].role == "operator"
    assert snapshot.state.project_guidance
    assert snapshot.state.last_guidance_saved_at is not None


def test_project_manager_guidance_is_compacted_into_memory(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Guidance Memory",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="minimal",
        notes=None,
    )

    for index in range(6):
        orchestrator.submit_project_manager_message(
            project.id,
            f"Plan the next focused cleanup pass {index}.",
            urgency="normal",
            execution_mode="safe_changes",
        )
        snapshot = orchestrator.save_project_manager_advisory(project.id)

    loaded = repository.get_project_manager_state(project.id)

    assert loaded is not None
    assert len(loaded.project_guidance) <= 4
    assert int(loaded.rolling_facts.get("saved_guidance_count", 0)) >= 1
    assert "compact project memory" in snapshot.state.display_snapshot["memory_summary"].lower()


def test_project_manager_feedback_passed_marks_project_complete(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Manual Test Pass",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="feedback pass coverage",
    )
    job = orchestrator.launch_job_from_project(
        project.id,
        prompt="Refine the dashboard browser layout and badge summaries.",
        task_type="code",
    )
    asyncio.run(orchestrator.run_job_now(job.id, worker_id="worker-feedback-pass"))

    snapshot = orchestrator.submit_project_operator_feedback(
        project.id,
        outcome="passed",
        notes="Queued job controls and browser layout looked correct in the browser.",
        severity="normal",
        area="layout",
        screenshot_reference="/tmp/browser-check.png",
        requires_followup=False,
    )
    stored_feedback = repository.list_project_operator_feedback(project.id, limit=None)

    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_response.decision == "mark_complete"
    assert snapshot.state.display_snapshot["reacting_to_operator_feedback"] is True
    assert snapshot.recent_feedback[0].outcome == "passed"
    assert stored_feedback[0].screenshot_reference == "/tmp/browser-check.png"


def test_project_manager_feedback_failed_creates_followup_draft(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Feedback Follow-up",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="feedback follow-up coverage",
    )

    snapshot = orchestrator.submit_project_operator_feedback(
        project.id,
        outcome="failed",
        notes="The project page still wastes space around the manager cards and the action row feels cramped.",
        severity="high",
        area="project-manager",
        screenshot_reference="/tmp/manager-gap.png",
        requires_followup=True,
    )
    stored_feedback = repository.list_project_operator_feedback(project.id, limit=None)

    assert snapshot.state.latest_response is not None
    assert snapshot.state.latest_response.decision == "launch_followup_job"
    assert snapshot.state.latest_response.draft_task is not None
    assert snapshot.state.latest_response.draft_task.execution_mode == "safe_changes"
    assert "manager-gap.png" in snapshot.state.latest_response.draft_task.prompt
    assert "Output style:" in snapshot.state.latest_response.draft_task.prompt
    assert stored_feedback[0].requires_followup is True
    assert stored_feedback[0].area == "project-manager"


def test_project_manager_feedback_history_is_bounded(tmp_path):
    orchestrator, repository, _ = build_test_orchestrator(tmp_path)
    project = orchestrator.create_saved_project(
        name="Bounded Feedback",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes=None,
    )

    for index in range(10):
        snapshot = orchestrator.submit_project_operator_feedback(
            project.id,
            outcome="mixed",
            notes=f"Feedback cycle {index}",
            severity="normal",
            area="layout",
            requires_followup=bool(index % 2),
        )

    stored_feedback = repository.list_project_operator_feedback(project.id, limit=None)

    assert len(stored_feedback) <= 8
    assert len(snapshot.recent_feedback) <= 5
    assert snapshot.recent_feedback[0].notes == "Feedback cycle 9"


def test_repo_agents_md_exists_and_calls_for_concise_codex_output():
    agents_path = Path(__file__).resolve().parents[1] / "AGENTS.md"
    contents = agents_path.read_text(encoding="utf-8")

    assert agents_path.exists()
    assert "Build AI Telescreen as a browser-first project cockpit for coding agents." in contents
    assert "PLAN" in contents
    assert "CHANGED" in contents
    assert "RESULT" in contents
    assert "TESTS" in contents
    assert "BLOCKERS" in contents
    assert "NEXT" in contents

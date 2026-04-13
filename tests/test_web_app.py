from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

import pytest

from claude_orchestrator.backends.messages_api import MessagesApiBackend
from claude_orchestrator.bootstrap import bootstrap
from claude_orchestrator.diagnostics import BackendSmokeTestResult, DiagnosticCheck, DiagnosticsReport
from claude_orchestrator.repository import JobRepository
from claude_orchestrator.models import BackendResult, ConversationState, EnqueueJobRequest, JobStatus, RetryDecision, RetryDisposition
from claude_orchestrator.services.orchestrator import OrchestratorService
from claude_orchestrator.timeutils import utcnow
from claude_orchestrator.web.app import build_app
from tests.helpers import CompletedBackend

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")
TestClient = testclient.TestClient


@pytest.fixture(autouse=True)
def _stub_messages_api_backend(monkeypatch):
    backend = CompletedBackend()

    async def fake_submit(self, job, state, context):
        return await backend.submit(job, state, context)

    async def fake_continue(self, job, state, context):
        return await backend.continue_job(job, state, context)

    monkeypatch.setattr(MessagesApiBackend, "submit", fake_submit)
    monkeypatch.setattr(MessagesApiBackend, "continue_job", fake_continue)


def _short_id(value: str, *, prefix: int = 8, suffix: int = 4) -> str:
    if len(value) <= prefix + suffix + 1:
        return value
    return f"{value[:prefix]}…{value[-suffix:]}"


class _FakeCodexProcess:
    def __init__(self, *, returncode: int = 0, stdout_chunks: list[bytes] | None = None) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.pid = 5432
        self.returncode: int | None = None
        self._returncode = returncode
        self._stdout_chunks = stdout_chunks or []
        self._done = asyncio.Event()
        self._feed_task = asyncio.create_task(self._feed())

    async def _feed(self) -> None:
        for chunk in self._stdout_chunks:
            self.stdout.feed_data(chunk)
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = self._returncode
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode if self.returncode is not None else self._returncode

    def terminate(self) -> None:
        self.returncode = -15
        self._feed_task.cancel()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def kill(self) -> None:
        self.returncode = -9
        self._feed_task.cancel()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()


class _FailingManagerBackend:
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


class _FollowupManagerBackend:
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


def _write_codex_test_config(config_path: Path, sqlite_name: str = "claude-orchestrator.db") -> None:
    config_path.write_text(
        "\n".join(
            [
                'default_backend = "codex_cli"',
                f'workspace_root = "{config_path.parent / "workspaces"}"',
                "",
                "[storage]",
                f'sqlite_path = "{sqlite_name}"',
                "",
                "[logging]",
                f'json_log_path = "{config_path.parent / "logs" / "orchestrator.jsonl"}"',
                "",
                "[backends.codex_cli]",
                "enabled = true",
                'executable = "codex"',
                "args = []",
                'command_template = ["python", "{prompt_file}"]',
                "timeout_seconds = 1800",
                "smoke_test_timeout_seconds = 15",
                'auth_mode = "auto"',
                "use_git_worktree = false",
                "max_output_bytes = 1048576",
                "preview_characters = 500",
            ]
        ),
        encoding="utf-8",
    )


def test_web_app_loads_templates_outside_repo_root(tmp_path, monkeypatch):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    app = build_app(root=tmp_path)
    client = TestClient(app)

    dashboard = client.get("/")
    status = client.get("/api/status")

    assert dashboard.status_code == 200
    assert "AI Telescreen" in dashboard.text
    assert "Create Job" in dashboard.text
    assert "Doctor" in dashboard.text
    assert "Run Queued Jobs Once" in dashboard.text
    assert status.status_code == 200
    assert "counts" in status.json()


def test_web_create_job_retry_cancel_and_duplicate_flow(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created = client.post(
        "/jobs",
        data={
            "prompt": "inspect repo",
            "backend": "messages_api",
            "provider": "",
            "task_type": "code",
            "priority": "2",
            "max_attempts": "4",
            "repo_path": str(tmp_path),
            "base_branch": "main",
        },
        follow_redirects=False,
    )
    job_id = Path(urlparse(created.headers["location"]).path).name
    job = context.repository.get_job(job_id)
    claimed = context.repository.claim_job(job.id, "worker-web", lease_seconds=30)
    assert claimed is not None
    context.repository.update_job_status(
        job.id,
        target_status=JobStatus.WAITING_RETRY,
        current_status=JobStatus.RUNNING,
    )

    duplicated = client.post(f"/jobs/{job.id}/duplicate", follow_redirects=False)
    duplicate_job_id = Path(urlparse(duplicated.headers["location"]).path).name
    cancel_response = client.post(f"/jobs/{duplicate_job_id}/cancel", follow_redirects=False)
    retry_response = client.post(f"/jobs/{job.id}/retry", follow_redirects=False)
    dashboard = client.get("/")

    assert created.status_code == 303
    assert job.metadata["repo_path"] == str(tmp_path.resolve())
    assert job.metadata.get("use_git_worktree") is not True
    assert cancel_response.status_code == 303
    assert retry_response.status_code == 303
    assert context.repository.get_job(duplicate_job_id).status == JobStatus.CANCELLED
    assert context.repository.get_job(job.id).status == JobStatus.QUEUED
    assert "Duplicate" in dashboard.text
    assert "Create Job" in dashboard.text


def test_web_job_actions_are_state_aware(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    queued = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="queued"))
    running = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="running"))
    failed = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="failed"))
    waiting_retry = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="retry later"))
    completed = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="done"))
    cancelled = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="cancelled"))

    assert context.repository.claim_job(running.id, "worker-1", lease_seconds=30) is not None
    failed_claim = context.repository.claim_job(failed.id, "worker-1", lease_seconds=30)
    waiting_claim = context.repository.claim_job(waiting_retry.id, "worker-1", lease_seconds=30)
    completed_claim = context.repository.claim_job(completed.id, "worker-1", lease_seconds=30)
    assert failed_claim is not None and waiting_claim is not None and completed_claim is not None
    context.repository.update_job_status(failed.id, target_status=JobStatus.FAILED, current_status=JobStatus.RUNNING)
    context.repository.update_job_status(
        waiting_retry.id,
        target_status=JobStatus.WAITING_RETRY,
        current_status=JobStatus.RUNNING,
    )
    context.repository.update_job_status(
        completed.id,
        target_status=JobStatus.COMPLETED,
        current_status=JobStatus.RUNNING,
    )
    orchestrator.cancel(cancelled.id)

    queued_page = client.get(f"/jobs/{queued.id}")
    running_page = client.get(f"/jobs/{running.id}")
    failed_page = client.get(f"/jobs/{failed.id}")
    waiting_page = client.get(f"/jobs/{waiting_retry.id}")
    completed_page = client.get(f"/jobs/{completed.id}")
    cancelled_page = client.get(f"/jobs/{cancelled.id}")
    jobs_partial = client.get("/partials/jobs")

    assert '>Run Now<' in queued_page.text
    assert '>Cancel<' in queued_page.text
    assert '>Delete<' in queued_page.text
    assert '>Retry<' not in queued_page.text
    assert f'/jobs/{queued.id}/run-now' in jobs_partial.text
    assert f'/jobs/{queued.id}/retry' not in jobs_partial.text

    assert '>Cancel<' in running_page.text
    assert '>Delete<' not in running_page.text
    assert '>Run Now<' not in running_page.text

    assert '>Retry<' in failed_page.text
    assert '>Delete<' in failed_page.text

    assert '>Retry Now<' in waiting_page.text
    assert '>Delete<' in waiting_page.text
    assert '>Cancel<' in waiting_page.text

    assert '>Duplicate<' in completed_page.text
    assert '>Delete<' in completed_page.text
    assert '>Cancel<' not in completed_page.text

    assert '>Delete<' in cancelled_page.text
    assert '>Duplicate<' in cancelled_page.text


def test_web_invalid_actions_fail_cleanly_and_delete_works(tmp_path, monkeypatch):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )

    async def fake_process_claimed_job(self, job, worker_id):
        self.repository.update_job_status(job.id, target_status=JobStatus.COMPLETED, current_status=JobStatus.RUNNING)
        return self.repository.get_job(job.id)

    monkeypatch.setattr(OrchestratorService, "process_claimed_job", fake_process_claimed_job)

    app = build_app(root=tmp_path)
    client = TestClient(app)

    queued = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="queued"))
    running = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="running"))
    failed = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="failed"))
    assert context.repository.claim_job(running.id, "worker-1", lease_seconds=30) is not None
    failed_claim = context.repository.claim_job(failed.id, "worker-1", lease_seconds=30)
    assert failed_claim is not None
    context.repository.update_job_status(failed.id, target_status=JobStatus.FAILED, current_status=JobStatus.RUNNING)

    invalid_retry = client.post(f"/jobs/{queued.id}/retry", follow_redirects=False)
    invalid_delete = client.post(f"/jobs/{running.id}/delete", follow_redirects=False)
    cancel_queued = client.post(f"/jobs/{queued.id}/cancel", follow_redirects=False)
    delete_failed = client.post(f"/jobs/{failed.id}/delete", follow_redirects=False)

    run_now_job = orchestrator.enqueue(EnqueueJobRequest(backend="messages_api", task_type="code", prompt="run now"))
    run_now = client.post(f"/jobs/{run_now_job.id}/run-now", follow_redirects=False)

    assert invalid_retry.status_code == 303
    assert "error=" in invalid_retry.headers["location"]
    assert invalid_delete.status_code == 303
    assert "error=" in invalid_delete.headers["location"]
    assert cancel_queued.status_code == 303
    assert context.repository.get_job(queued.id).status == JobStatus.CANCELLED
    assert delete_failed.status_code == 303
    with pytest.raises(KeyError):
        context.repository.get_job(failed.id)
    assert run_now.status_code == 303
    assert context.repository.get_job(run_now_job.id).status == JobStatus.COMPLETED


def test_web_projects_create_launch_and_prefill(tmp_path):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Main Repo",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "notes": "operator default",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name

    new_job_page = client.get(f"/jobs/new?project_id={project_id}")
    launch = client.post(
        f"/projects/{project_id}/launch",
        data={"prompt": "launch from project", "task_type": "code", "priority": "1", "max_attempts": "3"},
        follow_redirects=False,
    )
    launched_job_id = Path(urlparse(launch.headers["location"]).path).name
    launched_job = context.repository.get_job(launched_job_id)
    projects_page = client.get("/projects")
    project_detail = client.get(f"/projects/{project_id}")

    assert created_project.status_code == 303
    assert 'value="messages_api"' in new_job_page.text
    assert f'value="{tmp_path.resolve()}"' in new_job_page.text
    assert "Using defaults from project" in new_job_page.text
    assert launch.status_code == 303
    assert launched_job.metadata["project_id"] == project_id
    assert launched_job.metadata["repo_path"] == str(tmp_path.resolve())
    assert launched_job.metadata.get("use_git_worktree") is not True
    assert projects_page.status_code == 200
    assert "Main Repo" in projects_page.text
    assert project_detail.status_code == 200
    assert "Launch Job" in project_detail.text


def test_web_doctor_page_and_api(tmp_path, monkeypatch):
    report = DiagnosticsReport(
        config_path=str(tmp_path / "claude-orchestrator.toml"),
        default_backend="codex_cli",
        enabled_backends=["codex_cli", "messages_api"],
        workspace_root=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "data" / "claude-orchestrator.db"),
        checks=[
            DiagnosticCheck(
                name="codex_cli",
                ok=True,
                status="configured",
                summary="Codex CLI is configured.",
                details={"resolved_executable": "/usr/bin/codex"},
            )
        ],
        warnings=[
            "No coding-oriented backend is currently enabled.",
            "Legacy codex_cli.command_template is still configured, but runtime jobs now ignore it unless use_legacy_command_template=true.",
        ],
        smoke_tests={
            "codex_cli": BackendSmokeTestResult(
                backend="codex_cli",
                provider="openai",
                ok=True,
                status="ok",
                summary="Codex smoke passed.",
                details={"version": "codex 1.2.3"},
                warnings=[
                    "Legacy codex_cli.command_template is still configured, but runtime jobs now ignore it unless use_legacy_command_template=true."
                ],
            )
        },
    )

    monkeypatch.setattr(
        OrchestratorService,
        "collect_diagnostics",
        lambda self, run_smoke_tests=True: report,
    )

    app = build_app(root=tmp_path)
    client = TestClient(app)

    doctor = client.get("/doctor")
    doctor_api = client.get("/api/doctor")

    assert doctor.status_code == 200
    assert "Codex Smoke Test" in doctor.text
    assert "Codex smoke passed." in doctor.text
    assert "runtime jobs now ignore it unless use_legacy_command_template=true" in doctor.text
    assert doctor_api.status_code == 200
    assert doctor_api.json()["smoke_tests"]["codex_cli"]["status"] == "ok"


def test_web_project_edit_and_recent_launch_history(tmp_path):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Main Repo",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "autonomy_mode": "partial",
            "notes": "before edit",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name

    edited = client.post(
        f"/projects/{project_id}/edit",
        data={
            "name": "Main Repo Edited",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "release",
            "autonomy_mode": "full",
            "notes": "after edit",
        },
        follow_redirects=False,
    )
    launched = client.post(
        f"/projects/{project_id}/launch",
        data={"prompt": "launch from edited project", "task_type": "code", "priority": "1", "max_attempts": "3"},
        follow_redirects=False,
    )
    launched_job_id = Path(urlparse(launched.headers["location"]).path).name
    detail = client.get(f"/projects/{project_id}")
    project = context.repository.get_saved_project(project_id)

    assert edited.status_code == 303
    assert launched.status_code == 303
    assert project.name == "Main Repo Edited"
    assert project.default_base_branch == "release"
    assert project.default_use_git_worktree is False
    assert project.autonomy_mode == "full"
    assert "Recent Launches" in detail.text
    assert _short_id(launched_job_id) in detail.text
    assert f'title="{launched_job_id}"' in detail.text
    assert "Edit Project" in detail.text
    assert "Project Manager autonomy" in detail.text


def test_web_tables_render_compact_ids_and_paths(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends=context.backends,
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    long_repo_path = tmp_path / "alpha" / "beta" / "gamma" / "delta" / "epsilon"
    long_repo_path.mkdir(parents=True)
    project_response = client.post(
        "/projects",
        data={
            "name": "Deep Project",
            "repo_path": str(long_repo_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "release",
            "default_use_git_worktree": "on",
            "notes": "Long path coverage",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303

    job = orchestrator.enqueue(
        EnqueueJobRequest(
            backend="messages_api",
            task_type="code",
            prompt="formatting coverage",
            metadata={"repo_path": str(long_repo_path), "base_branch": "main"},
        )
    )
    job = context.repository.get_job(job.id)

    projects_page = client.get("/projects")
    jobs_partial = client.get("/partials/jobs")
    job_detail = client.get(f"/jobs/{job.id}")

    expected_project_path = f"…/{'/'.join(long_repo_path.parts[-4:])}"

    assert projects_page.status_code == 200
    assert expected_project_path in projects_page.text
    assert f'title="{long_repo_path}"' in projects_page.text
    assert "messages_api" in projects_page.text
    assert "anthropic" in projects_page.text
    assert "base release" in projects_page.text
    assert "worktree" in projects_page.text

    assert jobs_partial.status_code == 200
    assert _short_id(job.id) in jobs_partial.text
    assert f'title="{job.id}"' in jobs_partial.text
    assert job.workspace_path is not None
    assert f'title="{job.workspace_path}"' in jobs_partial.text
    assert "path-chip" in jobs_partial.text

    assert job_detail.status_code == 200
    assert job.id in job_detail.text
    assert "Summary" in job_detail.text
    assert "Current Activity" in job_detail.text
    assert "Details" in job_detail.text
    assert "History" in job_detail.text
    assert "Artifacts" in job_detail.text
    assert "Technical" in job_detail.text
    assert job_detail.text.count('<details class="panel disclosure"') >= 4


def test_web_new_codex_job_run_now_uses_direct_runtime_even_with_stale_template(tmp_path, monkeypatch):
    config_path = tmp_path / "claude-orchestrator.toml"
    _write_codex_test_config(config_path)
    captured: list[list[str]] = []

    monkeypatch.setattr("shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        return _FakeCodexProcess(stdout_chunks=[b"done\n"])

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    app = build_app(root=tmp_path, config_path=config_path)
    client = TestClient(app)

    created = client.post(
        "/jobs",
        data={
            "prompt": "ship fix",
            "backend": "codex_cli",
            "provider": "openai",
            "task_type": "code",
            "priority": "0",
            "max_attempts": "3",
        },
        follow_redirects=False,
    )
    job_id = Path(urlparse(created.headers["location"]).path).name
    run_now = client.post(f"/jobs/{job_id}/run-now", follow_redirects=False)
    context = bootstrap(tmp_path, config_path=config_path)

    assert created.status_code == 303
    assert run_now.status_code == 303
    assert captured[0] == ["/usr/bin/codex", "exec", "ship fix"]
    assert context.repository.get_job(job_id).status == JobStatus.COMPLETED


def test_web_project_launch_codex_run_now_uses_direct_runtime_even_with_stale_template(tmp_path, monkeypatch):
    config_path = tmp_path / "claude-orchestrator.toml"
    _write_codex_test_config(config_path, sqlite_name="project-launch.db")
    captured: list[list[str]] = []

    monkeypatch.setattr("shutil.which", lambda executable: f"/usr/bin/{executable}")

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        return _FakeCodexProcess(stdout_chunks=[b"done\n"])

    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.asyncio.create_subprocess_exec", fake_exec)

    app = build_app(root=tmp_path, config_path=config_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Codex Repo",
            "repo_path": str(tmp_path),
            "default_backend": "codex_cli",
            "default_provider": "openai",
            "default_base_branch": "main",
            "notes": "Codex launch defaults",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name
    launch = client.post(
        f"/projects/{project_id}/launch",
        data={"prompt": "project launch fix", "task_type": "code", "priority": "1", "max_attempts": "4"},
        follow_redirects=False,
    )
    job_id = Path(urlparse(launch.headers["location"]).path).name
    run_now = client.post(f"/jobs/{job_id}/run-now", follow_redirects=False)
    context = bootstrap(tmp_path, config_path=config_path)
    launched_job = context.repository.get_job(job_id)

    assert created_project.status_code == 303
    assert launch.status_code == 303
    assert run_now.status_code == 303
    assert captured[0] == ["/usr/bin/codex", "exec", "project launch fix"]
    assert launched_job.metadata["project_id"] == project_id
    assert launched_job.status == JobStatus.COMPLETED


def test_project_detail_renders_project_manager_summary(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Manager UI",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="browser project",
    )
    job = orchestrator.launch_job_from_project(
        project.id,
        prompt="Refine the dashboard browser layout and badge summaries.",
        task_type="code",
    )
    asyncio.run(orchestrator.run_job_now(job.id, worker_id="worker-manager-ui"))

    detail = client.get(f"/projects/{project.id}")

    assert detail.status_code == 200
    assert "Project Manager" in detail.text
    assert "Live workspace" in detail.text
    assert "Coding Agent" in detail.text
    assert "Live handoff" in detail.text
    assert "Manual verification" in detail.text
    assert "manual testing needed" in detail.text
    assert "Show details" in detail.text
    assert "Details" in detail.text
    assert "History" in detail.text
    assert "Leave feedback" in detail.text
    assert "Advanced" in detail.text
    assert detail.text.count('<details class="panel disclosure"') >= 4
    assert "No new task yet" in detail.text
    assert _short_id(job.id) in detail.text


def test_project_detail_project_manager_composer_submits_and_renders_history(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Composer Project",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="composer coverage",
    )

    detail = client.get(f"/projects/{project.id}")
    submitted = client.post(
        f"/projects/{project.id}/manager/messages",
        data={
            "message": "Review the codebase and tell me what to do next.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "read_only",
        },
        follow_redirects=True,
    )

    assert detail.status_code == 200
    assert "Talk to Project Manager" in detail.text
    assert "Ask Project Manager" in detail.text
    assert "Options" in detail.text
    assert submitted.status_code == 200
    assert "Live workspace" in submitted.text
    assert "Store as Project Guidance" in submitted.text
    assert "History" in submitted.text
    assert "Leave feedback" in submitted.text
    assert "Run it" in submitted.text
    assert "Review the codebase and tell me what to do next." in submitted.text
    assert "read-only review" in submitted.text
    assert "Show details" in submitted.text
    assert '<option value="" selected>Auto</option>' in submitted.text


def test_project_manager_composer_uses_coding_agent_language(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Composer Labels",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="label coverage",
    )

    detail = client.get(f"/projects/{project.id}")

    assert detail.status_code == 200
    assert "Planner: Project Manager" in detail.text
    assert "Coding Agent" in detail.text
    assert "Auto (available, prefers Codex)" in detail.text
    assert "Claude (use the best available Claude executor)" in detail.text
    assert "Backend Preference" not in detail.text
    assert '<option value="" selected>Auto</option>' in detail.text


def test_project_manager_session_shows_waiting_on_worker_for_queued_task(tmp_path, monkeypatch):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    def never_claim(self, *args, **kwargs):
        return None

    project = client.post(
        "/projects",
        data={
            "name": "Queued Manager Session",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "autonomy_mode": "full",
            "notes": "queued session coverage",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(project.headers["location"]).path).name

    monkeypatch.setattr(JobRepository, "claim_job", never_claim)
    submitted = client.post(
        f"/projects/{project_id}/manager/messages",
        data={
            "message": "Find something to improve.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )
    auto_job = context.repository.list_project_jobs(project_id, limit=10)[0]

    assert submitted.status_code == 200
    assert "Task queued, waiting for worker" in submitted.text
    assert "Task was created but could not start immediately because immediate execution is unavailable in this context." in submitted.text
    assert "I handed off the current step." in submitted.text
    assert _short_id(auto_job.id) in submitted.text
    assert f'href="/jobs/{auto_job.id}"' in submitted.text


def test_project_manager_starts_full_autonomy_task_immediately(tmp_path):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Running Manager Session",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "autonomy_mode": "full",
            "notes": "immediate start coverage",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name
    submitted = client.post(
        f"/projects/{project_id}/manager/messages",
        data={
            "message": "Find something to improve.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )
    auto_job = context.repository.list_project_jobs(project_id, limit=10)[0]
    events = context.repository.get_scheduler_events(auto_job.id)

    assert submitted.status_code == 200
    assert "The Project Manager handed the task to the coding agent and started it immediately." in submitted.text
    assert auto_job.status == JobStatus.COMPLETED
    assert any(event.event_type == "job_run_now_requested" for event in events)
    assert _short_id(auto_job.id) in submitted.text


def test_project_manager_followup_and_advisory_flow_in_browser(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Follow-up Project",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="follow-up coverage",
    )
    client.post(
        f"/projects/{project.id}/manager/messages",
        data={
            "message": "Plan the next UI cleanup pass.",
            "urgency": "high",
            "backend_preference": "auto",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )

    followup = client.get(f"/projects/{project.id}?manager_followup=1")
    followup_job_form = client.get(f"/jobs/new?project_id={project.id}&manager_followup=1")
    advisory = client.post(
        f"/projects/{project.id}/manager/save-advisory",
        follow_redirects=True,
    )

    assert followup.status_code == 200
    assert "Continue the current AI Telescreen project-manager discussion" in followup.text
    assert '<option value="" selected>Auto</option>' in followup.text
    assert followup_job_form.status_code == 200
    assert '<option value="">Auto</option>' in followup_job_form.text
    assert advisory.status_code == 200
    assert "Stored the latest recommendation as project guidance in the manager" in advisory.text
    assert "guidance saved" in advisory.text
    assert "compact memory" in advisory.text


def test_project_manager_followup_while_waiting_on_worker_explains_why_no_new_task_started(tmp_path, monkeypatch):
    context = bootstrap(tmp_path)
    app = build_app(root=tmp_path)
    client = TestClient(app)

    created_project = client.post(
        "/projects",
        data={
            "name": "Waiting Explanation",
            "repo_path": str(tmp_path),
            "default_backend": "messages_api",
            "default_provider": "anthropic",
            "default_base_branch": "main",
            "autonomy_mode": "full",
            "notes": "waiting explanation coverage",
        },
        follow_redirects=False,
    )
    project_id = Path(urlparse(created_project.headers["location"]).path).name

    def never_claim(self, *args, **kwargs):
        return None

    monkeypatch.setattr(JobRepository, "claim_job", never_claim)

    client.post(
        f"/projects/{project_id}/manager/messages",
        data={
            "message": "Find something to improve.",
            "urgency": "normal",
            "coding_agent": "claude",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )

    followup = client.post(
        f"/projects/{project_id}/manager/messages",
        data={
            "message": "Also watch for wasted space on the page.",
            "urgency": "normal",
            "coding_agent": "claude",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )

    assert followup.status_code == 200
    assert "Task was created but could not start immediately because immediate execution is unavailable in this context." in followup.text
    assert "I handed off the current step." in followup.text
    assert len(context.repository.list_project_jobs(project_id, limit=10)) == 1


def test_project_manager_partial_autonomy_browser_flow_shows_continue_prompt(tmp_path, monkeypatch):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": _FollowupManagerBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)
    followup_backend = _FollowupManagerBackend()

    async def fake_submit(self, job, state, context):
        return await followup_backend.submit(job, state, context)

    async def fake_continue(self, job, state, context):
        return await followup_backend.continue_job(job, state, context)

    monkeypatch.setattr(MessagesApiBackend, "submit", fake_submit)
    monkeypatch.setattr(MessagesApiBackend, "continue_job", fake_continue)

    project = orchestrator.create_saved_project(
        name="Partial Manager Flow",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="partial",
        notes="partial autonomy browser flow",
    )

    submitted = client.post(
        f"/projects/{project.id}/manager/messages",
        data={
            "message": "Find something to improve.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )
    launched = client.post(f"/projects/{project.id}/manager/launch-draft", follow_redirects=True)
    auto_job = context.repository.list_project_jobs(project.id, limit=10)[0]
    detail = client.get(f"/projects/{project.id}")
    job_detail = client.get(f"/jobs/{auto_job.id}")
    continued = client.post(f"/projects/{project.id}/manager/continue", follow_redirects=False)

    assert submitted.status_code == 200
    assert "waiting for your approval before it launches work" in submitted.text
    assert "Run it" in submitted.text
    assert launched.status_code == 200
    assert "The Project Manager handed the latest task to the coding agent and started it immediately." in launched.text
    assert detail.status_code == 200
    assert "Waiting on your decision" in detail.text
    assert "Keep Going" in detail.text
    assert "Want me to keep going?" in detail.text
    assert auto_job.status == JobStatus.COMPLETED
    assert job_detail.status_code == 200
    assert "Keep Going" in job_detail.text
    assert f'/projects/{project.id}/manager/continue' in job_detail.text
    assert continued.status_code == 303


def test_project_manager_session_shows_manual_test_pause(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Manual Test Session",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        autonomy_mode="partial",
        notes="manual-test session coverage",
    )

    client.post(
        f"/projects/{project.id}/manager/messages",
        data={
            "message": "Tighten the browser layout and action badges.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )
    client.post(f"/projects/{project.id}/manager/launch-draft", follow_redirects=True)
    auto_job = context.repository.list_project_jobs(project.id, limit=10)[0]
    orchestrator.project_manager.update_workflow_state(
        project.id,
        workflow_state="blocked_on_manual_test",
        active_job_id=None,
        active_autonomy_session_id=str(auto_job.metadata.get("project_manager_autonomy_session_id") or "") or None,
        auto_tasks_run_count=1,
    )

    detail = client.get(f"/projects/{project.id}")

    assert detail.status_code == 200
    assert "Waiting for manual testing" in detail.text
    assert "manual test needed" in detail.text
    assert _short_id(auto_job.id) in detail.text


def test_project_manager_launch_task_from_interactive_response(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Interactive Launch",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="interactive launch",
    )
    client.post(
        f"/projects/{project.id}/manager/messages",
        data={
            "message": "Focus on the wasted space in this page.",
            "urgency": "normal",
            "backend_preference": "messages_api",
            "execution_mode": "safe_changes",
        },
        follow_redirects=True,
    )

    new_job = client.get(f"/jobs/new?project_id={project.id}&manager_draft=1")
    launched = client.post(f"/projects/{project.id}/manager/launch-draft", follow_redirects=False)
    launched_job_id = context.repository.list_project_jobs(project.id, limit=10)[0].id
    launched_job = context.repository.get_job(launched_job_id)

    assert new_job.status_code == 200
    assert "Recommended task draft" in new_job.text
    assert "Goal:" in new_job.text
    assert "Output style:" in new_job.text
    assert launched.status_code == 303
    assert urlparse(launched.headers["location"]).path == f"/projects/{project.id}"
    assert launched_job.metadata["project_id"] == project.id
    assert launched_job.metadata["execution_mode"] == "safe_changes"
    assert launched_job.metadata["project_manager_auto_launched"] is True


def test_project_feedback_submission_renders_recent_feedback_and_updates_recommendation(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Feedback Browser",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="feedback browser coverage",
    )

    submitted = client.post(
        f"/projects/{project.id}/feedback",
        data={
            "outcome": "failed",
            "notes": "Run Now worked, but the project page still wastes space around the manager cards.",
            "severity": "high",
            "area": "project-manager",
            "screenshot_reference": "/tmp/project-page-gap.png",
            "requires_followup": "on",
        },
        follow_redirects=True,
    )

    assert submitted.status_code == 200
    assert "Leave feedback" in submitted.text
    assert "Operator Feedback" in submitted.text
    assert "reacting to operator feedback" in submitted.text
    assert "project-page-gap.png" in submitted.text
    assert "follow-up requested" in submitted.text
    assert "Run it" in submitted.text


def test_project_feedback_validation_error_renders_cleanly(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": CompletedBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Feedback Validation",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="validation coverage",
    )

    submitted = client.post(
        f"/projects/{project.id}/feedback",
        data={
            "outcome": "mixed",
            "notes": "",
            "severity": "normal",
        },
        follow_redirects=True,
    )

    assert submitted.status_code == 400
    assert "Operator feedback notes are required." in submitted.text


def test_project_manager_draft_prefills_new_job_form_and_launches(tmp_path):
    context = bootstrap(tmp_path)
    orchestrator = OrchestratorService(
        root=context.root,
        config=context.config,
        repository=context.repository,
        backends={"messages_api": _FailingManagerBackend()},
    )
    app = build_app(root=tmp_path)
    client = TestClient(app)

    project = orchestrator.create_saved_project(
        name="Manager Drafts",
        repo_path=str(tmp_path),
        default_backend="messages_api",
        default_provider="anthropic",
        default_base_branch="main",
        default_use_git_worktree=False,
        notes="manager draft project",
    )
    job = orchestrator.launch_job_from_project(
        project.id,
        prompt="Fix the broken browser regression.",
        task_type="code",
    )
    asyncio.run(orchestrator.run_job_now(job.id, worker_id="worker-manager-draft"))

    new_job = client.get(f"/jobs/new?project_id={project.id}&manager_draft=1")
    launched = client.post(f"/projects/{project.id}/manager/launch-draft", follow_redirects=False)
    launched_job_id = context.repository.list_project_jobs(project.id, limit=10)[0].id
    launched_job = context.repository.get_job(launched_job_id)

    assert new_job.status_code == 200
    assert "Recommended task draft" in new_job.text
    assert "UI smoke test is still failing" in new_job.text
    assert "Output style:" in new_job.text
    assert launched.status_code == 303
    assert urlparse(launched.headers["location"]).path == f"/projects/{project.id}"
    assert launched_job.prompt != ""
    assert launched_job.metadata["project_id"] == project.id
    assert launched_job.metadata["execution_mode"] == "coding_pass"

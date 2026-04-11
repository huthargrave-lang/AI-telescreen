from __future__ import annotations

import subprocess
from pathlib import Path

from claude_orchestrator.backends.codex_cli import CodexCliBackend
from claude_orchestrator.diagnostics import BackendSmokeTestResult
from claude_orchestrator.services.orchestrator import OrchestratorService
from tests.helpers import build_test_orchestrator


def _build_orchestrator_with_codex(tmp_path: Path) -> OrchestratorService:
    orchestrator, _, config = build_test_orchestrator(tmp_path)
    config.backends.codex_cli.enabled = True
    config.backends.codex_cli.executable = "codex"
    config.backends.codex_cli.args = []
    orchestrator.backends["codex_cli"] = CodexCliBackend(config)
    return orchestrator


def test_codex_smoke_test_reports_missing_executable(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: None)

    result = orchestrator.run_codex_smoke_test()

    assert result.ok is False
    assert result.status == "invalid_config"
    assert "not found" in result.summary.lower()


def test_codex_smoke_test_reports_auth_failure(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="codex 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="login required: api key invalid\n")

    monkeypatch.setattr("claude_orchestrator.services.orchestrator.subprocess.run", fake_run)

    result = orchestrator.run_codex_smoke_test()

    assert result.ok is False
    assert result.status == "auth_error"
    assert result.details["version"] == "codex 1.2.3"


def test_codex_smoke_test_reports_success(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="codex 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout='{"type":"message","text":"AI Telescreen codex smoke test OK"}\n', stderr="")

    monkeypatch.setattr("claude_orchestrator.services.orchestrator.subprocess.run", fake_run)

    result = orchestrator.run_codex_smoke_test()

    assert result.ok is True
    assert result.status == "ok"
    assert calls[1][:3] == ["/usr/bin/codex", "exec", "--skip-git-repo-check"]


def test_collect_diagnostics_includes_codex_smoke_test(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    monkeypatch.setenv(orchestrator.config.backends.messages_api.api_key_env, "test-key")
    monkeypatch.setattr("claude_orchestrator.services.orchestrator.shutil.which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr("claude_orchestrator.services.orchestrator.validate_backend_config", lambda config: None)

    def fake_backend_smoke_test(_backend_name: str):
        from claude_orchestrator.diagnostics import BackendSmokeTestResult

        return BackendSmokeTestResult(
            backend="codex_cli",
            provider="openai",
            ok=True,
            status="ok",
            summary="Codex smoke passed.",
            details={"resolved_executable": "/usr/bin/codex"},
        )

    monkeypatch.setattr(orchestrator, "run_backend_smoke_test", fake_backend_smoke_test)
    monkeypatch.setattr(orchestrator, "run_codex_smoke_test", lambda: fake_backend_smoke_test("codex_cli"))

    report = orchestrator.collect_diagnostics(run_smoke_tests=True)

    assert report.ok is True
    assert report.smoke_tests["codex_cli"].status == "ok"
    assert any(check.name == "codex_cli" for check in report.checks)


def test_collect_diagnostics_warns_about_stale_legacy_codex_command_template(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    orchestrator.config.backends.codex_cli.command_template = ["python", "{prompt_file}"]
    monkeypatch.setenv(orchestrator.config.backends.messages_api.api_key_env, "test-key")
    monkeypatch.setattr("claude_orchestrator.services.orchestrator.shutil.which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr("claude_orchestrator.services.orchestrator.validate_backend_config", lambda config: None)
    monkeypatch.setattr(
        orchestrator,
        "run_codex_smoke_test",
        lambda: BackendSmokeTestResult(
            backend="codex_cli",
            provider="openai",
            ok=True,
            status="ok",
            summary="Codex smoke passed.",
            details={"resolved_executable": "/usr/bin/codex"},
            warnings=[
                "Legacy codex_cli.command_template is still configured, but runtime jobs now ignore it unless use_legacy_command_template=true."
            ],
        ),
    )

    report = orchestrator.collect_diagnostics(run_smoke_tests=True)
    codex_check = next(check for check in report.checks if check.name == "codex_cli")

    assert codex_check.details["runtime_mode"] == "direct_prompt"
    assert codex_check.details["legacy_command_template_configured"] == "true"
    assert any("ignore it" in warning.lower() for warning in report.warnings)


def test_codex_smoke_test_warns_when_legacy_runtime_mode_is_enabled(monkeypatch, tmp_path):
    orchestrator = _build_orchestrator_with_codex(tmp_path)
    orchestrator.config.backends.codex_cli.use_legacy_command_template = True
    orchestrator.config.backends.codex_cli.command_template = ["{executable}", "{prompt_file}"]
    monkeypatch.setattr("claude_orchestrator.backends.codex_cli.shutil.which", lambda executable: f"/usr/bin/{executable}")

    def fake_run(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="codex 1.2.3\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="AI Telescreen codex smoke test OK\n", stderr="")

    monkeypatch.setattr("claude_orchestrator.services.orchestrator.subprocess.run", fake_run)

    result = orchestrator.run_codex_smoke_test()

    assert result.ok is True
    assert result.details["runtime_mode"] == "legacy_command_template"
    assert any("does not validate legacy command_template runtime execution" in warning for warning in result.warnings)

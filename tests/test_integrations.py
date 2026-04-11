from __future__ import annotations

from claude_orchestrator.integrations import discover_workspace_integrations


def test_discover_workspace_integrations_reads_project_claude_settings(tmp_path):
    workspace = tmp_path / "workspace"
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        """
        {
          "mcpServers": {
            "github": {
              "command": "npx",
              "args": ["@modelcontextprotocol/server-github"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    summary = discover_workspace_integrations(workspace, user_config_paths=[])

    assert summary.status() == "integration-enabled"
    assert summary.config_paths == [str(settings_path.resolve())]
    assert len(summary.capabilities) == 1
    assert summary.capabilities[0].name == "github"
    assert summary.capabilities[0].source == "project_mcp"
    assert summary.capabilities[0].kind == "github_tool"


def test_discover_workspace_integrations_distinguishes_project_and_user_sources(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    project_mcp = repo / ".mcp.json"
    project_mcp.write_text(
        """
        {
          "servers": {
            "notion": {
              "url": "https://mcp.example.test/notion"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    user_settings = tmp_path / "home" / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(
        """
        {
          "mcpServers": {
            "github": {
              "command": "npx",
              "args": ["@modelcontextprotocol/server-github"]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    summary = discover_workspace_integrations(
        workspace,
        repo_path=repo,
        user_config_paths=[user_settings],
    )

    assert {capability.source for capability in summary.capabilities} == {"project_mcp", "user_mcp"}
    assert "project integrations" in summary.status_labels()
    assert "user integrations" in summary.status_labels()
    assert summary.repo_path == str(repo.resolve())


def test_discover_workspace_integrations_notes_malformed_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    malformed = workspace / ".mcp.json"
    malformed.write_text("{not-json", encoding="utf-8")

    summary = discover_workspace_integrations(workspace, user_config_paths=[])

    assert summary.status() == "local-only"
    assert summary.capabilities == []
    assert any("Malformed JSON" in note for note in summary.notes)

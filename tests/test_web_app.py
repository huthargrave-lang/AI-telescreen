from __future__ import annotations

import pytest

from claude_orchestrator.web.app import build_app

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")
TestClient = testclient.TestClient


def test_web_app_loads_templates_outside_repo_root(tmp_path, monkeypatch):
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    app = build_app(root=tmp_path)
    client = TestClient(app)

    dashboard = client.get("/")
    status = client.get("/api/status")

    assert dashboard.status_code == 200
    assert "claude-orchestrator" in dashboard.text
    assert status.status_code == 200
    assert "counts" in status.json()

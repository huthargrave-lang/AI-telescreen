"""Workspace integration discovery and summary helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

PROJECT_CLAUDE_SETTINGS = Path(".claude/settings.json")
PROJECT_MCP_CONFIG = Path(".mcp.json")
DEFAULT_USER_CONFIG_PATHS = (
    Path("~/.claude/settings.json").expanduser(),
    Path("~/.mcp.json").expanduser(),
)
LOCAL_TOOL_NAMES = {
    "bash",
    "edit",
    "glob",
    "grep",
    "ls",
    "read",
    "task",
    "todowrite",
    "write",
}


@dataclass
class IntegrationCapability:
    name: str
    source: str
    kind: str
    enabled: bool = True
    description: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "enabled": self.enabled,
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "IntegrationCapability":
        return cls(
            name=str(payload.get("name") or ""),
            source=str(payload.get("source") or "workspace_config"),
            kind=str(payload.get("kind") or "mcp_server"),
            enabled=bool(payload.get("enabled", True)),
            description=payload.get("description"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class WorkspaceIntegrationSummary:
    workspace_path: str
    repo_path: Optional[str]
    capabilities: List[IntegrationCapability] = field(default_factory=list)
    config_paths: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_path": self.workspace_path,
            "repo_path": self.repo_path,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "config_paths": list(self.config_paths),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorkspaceIntegrationSummary":
        return cls(
            workspace_path=str(payload.get("workspace_path") or ""),
            repo_path=payload.get("repo_path"),
            capabilities=[
                IntegrationCapability.from_dict(item)
                for item in payload.get("capabilities") or []
                if isinstance(item, dict)
            ],
            config_paths=[str(path) for path in payload.get("config_paths") or []],
            notes=[str(note) for note in payload.get("notes") or []],
        )

    def enabled_capabilities(self) -> List[IntegrationCapability]:
        return [capability for capability in self.capabilities if capability.enabled]

    def external_capabilities(self) -> List[IntegrationCapability]:
        return [
            capability
            for capability in self.enabled_capabilities()
            if capability.kind in {"mcp_server", "github_tool", "remote_tool"}
        ]

    def project_capabilities(self) -> List[IntegrationCapability]:
        return [
            capability
            for capability in self.enabled_capabilities()
            if capability.source in {"project_mcp", "workspace_config"}
        ]

    def user_capabilities(self) -> List[IntegrationCapability]:
        return [
            capability
            for capability in self.enabled_capabilities()
            if capability.source == "user_mcp"
        ]

    def status(self) -> str:
        return "integration-enabled" if self.external_capabilities() else "local-only"

    def status_labels(self) -> List[str]:
        labels: List[str] = []
        external_capabilities = self.external_capabilities()
        if not external_capabilities:
            return ["local-only"]
        if any(capability.source == "workspace_config" for capability in external_capabilities):
            labels.append("workspace integrations")
        if any(capability.source == "project_mcp" for capability in external_capabilities):
            labels.append("project integrations")
        if any(capability.source == "user_mcp" for capability in external_capabilities):
            labels.append("user integrations")
        if external_capabilities and all(capability.kind == "github_tool" for capability in external_capabilities):
            labels.append("GitHub tools only")
        else:
            labels.append("external tools enabled")
        return labels


def build_integration_metadata_summary(summary: WorkspaceIntegrationSummary) -> Dict[str, Any]:
    enabled_capabilities = summary.enabled_capabilities()
    external_capabilities = summary.external_capabilities()
    return {
        "status": summary.status(),
        "labels": summary.status_labels(),
        "config_paths": list(summary.config_paths),
        "capability_names": [capability.name for capability in enabled_capabilities],
        "capability_count": len(enabled_capabilities),
        "external_capability_count": len(external_capabilities),
        "project_capability_count": len(summary.project_capabilities()),
        "user_capability_count": len(summary.user_capabilities()),
        "notes": list(summary.notes),
    }


def parse_project_claude_settings(path: Path) -> List[IntegrationCapability]:
    capabilities, _ = _parse_claude_settings(path, source="project_mcp")
    return capabilities


def parse_mcp_config(path: Path, *, source: str = "project_mcp") -> List[IntegrationCapability]:
    capabilities, _ = _parse_mcp_settings(path, source=source)
    return capabilities


def discover_workspace_integrations(
    workspace_path: Path,
    repo_path: Optional[Path] = None,
    *,
    user_config_paths: Optional[Sequence[Path]] = None,
) -> WorkspaceIntegrationSummary:
    workspace_root = workspace_path.expanduser().resolve()
    repo_root = repo_path.expanduser().resolve() if repo_path else None
    summary = WorkspaceIntegrationSummary(
        workspace_path=str(workspace_root),
        repo_path=str(repo_root) if repo_root else None,
    )
    seen_paths: set[str] = set()

    for root, source in _iter_workspace_scan_roots(workspace_root, repo_root):
        for relative_path, parser in (
            (PROJECT_CLAUDE_SETTINGS, _parse_claude_settings),
            (PROJECT_MCP_CONFIG, _parse_mcp_settings),
        ):
            config_path = root / relative_path
            if not config_path.exists():
                continue
            resolved_path = str(config_path.resolve())
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            summary.config_paths.append(resolved_path)
            capabilities, notes = parser(config_path, source=source)
            summary.capabilities.extend(capabilities)
            summary.notes.extend(notes)

    paths_to_check = DEFAULT_USER_CONFIG_PATHS if user_config_paths is None else tuple(user_config_paths)
    for config_path in paths_to_check:
        path = Path(config_path).expanduser()
        if not path.exists():
            continue
        resolved_path = str(path.resolve())
        if resolved_path in seen_paths:
            continue
        seen_paths.add(resolved_path)
        summary.config_paths.append(resolved_path)
        parser = _parse_claude_settings if path.name == "settings.json" else _parse_mcp_settings
        capabilities, notes = parser(path, source="user_mcp")
        summary.capabilities.extend(capabilities)
        summary.notes.extend(notes)

    summary.capabilities = _dedupe_capabilities(summary.capabilities)
    summary.config_paths = list(dict.fromkeys(summary.config_paths))
    summary.notes = list(dict.fromkeys(summary.notes))
    return summary


def _iter_workspace_scan_roots(workspace_root: Path, repo_root: Optional[Path]) -> Iterable[Tuple[Path, str]]:
    if repo_root and repo_root != workspace_root:
        yield workspace_root, "workspace_config"
        yield repo_root, "project_mcp"
        return
    yield workspace_root, "project_mcp"


def _parse_claude_settings(path: Path, *, source: str) -> Tuple[List[IntegrationCapability], List[str]]:
    payload, notes = _load_json_object(path)
    if payload is None:
        return [], notes

    capabilities: List[IntegrationCapability] = []
    capabilities.extend(_extract_server_capabilities(payload.get("mcpServers"), source=source))

    mcp_section = payload.get("mcp")
    if isinstance(mcp_section, dict):
        capabilities.extend(_extract_server_capabilities(mcp_section.get("servers"), source=source))
        capabilities.extend(_extract_server_capabilities(mcp_section.get("mcpServers"), source=source))

    for key in ("integrations", "tools", "allowedTools"):
        capabilities.extend(_extract_tool_capabilities(payload.get(key), source=source))

    return _dedupe_capabilities(capabilities), notes


def _parse_mcp_settings(path: Path, *, source: str) -> Tuple[List[IntegrationCapability], List[str]]:
    payload, notes = _load_json_object(path)
    if payload is None:
        return [], notes

    capabilities: List[IntegrationCapability] = []
    capabilities.extend(_extract_server_capabilities(payload.get("mcpServers"), source=source))
    capabilities.extend(_extract_server_capabilities(payload.get("servers"), source=source))

    mcp_section = payload.get("mcp")
    if isinstance(mcp_section, dict):
        capabilities.extend(_extract_server_capabilities(mcp_section.get("servers"), source=source))

    return _dedupe_capabilities(capabilities), notes


def _load_json_object(path: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"Could not read integration config {path}: {exc}"]
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, [f"Malformed JSON in integration config {path}: {exc.msg}"]
    if not isinstance(payload, dict):
        notes.append(f"Integration config {path} did not contain a top-level JSON object.")
        return None, notes
    return payload, notes


def _extract_server_capabilities(value: Any, *, source: str) -> List[IntegrationCapability]:
    capabilities: List[IntegrationCapability] = []
    for name, details in _iter_named_entries(value):
        capability = _build_capability(name, details, source=source, default_kind="mcp_server")
        if capability is not None:
            capabilities.append(capability)
    return capabilities


def _extract_tool_capabilities(value: Any, *, source: str) -> List[IntegrationCapability]:
    capabilities: List[IntegrationCapability] = []
    for name, details in _iter_named_entries(value):
        capability = _build_capability(name, details, source=source, default_kind="tool")
        if capability is None or capability.kind == "local_tool":
            continue
        capabilities.append(capability)
    return capabilities


def _iter_named_entries(value: Any) -> Iterable[Tuple[str, Any]]:
    if isinstance(value, dict):
        for name, details in value.items():
            yield str(name), details
        return
    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            if isinstance(item, str):
                yield item, {"name": item}
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("server") or f"entry_{index}"
                yield str(name), item


def _build_capability(
    name: str,
    details: Any,
    *,
    source: str,
    default_kind: str,
) -> Optional[IntegrationCapability]:
    payload = details if isinstance(details, dict) else {"value": details}
    kind = _infer_capability_kind(name, payload, default_kind=default_kind)
    enabled = not bool(payload.get("disabled")) and payload.get("enabled", True) is not False
    description = payload.get("description") if isinstance(payload.get("description"), str) else None
    if not name.strip():
        return None
    return IntegrationCapability(
        name=name.strip(),
        source=source,
        kind=kind,
        enabled=enabled,
        description=description,
        metadata=_safe_capability_metadata(payload),
    )


def _infer_capability_kind(name: str, payload: Dict[str, Any], *, default_kind: str) -> str:
    lowered_name = name.strip().lower()
    joined = " ".join(
        str(part)
        for part in (
            lowered_name,
            payload.get("command") or "",
            payload.get("transport") or "",
            payload.get("type") or "",
            payload.get("url") or "",
            payload.get("endpoint") or "",
            payload.get("description") or "",
        )
        if part
    ).lower()

    if "github" in joined:
        return "github_tool"
    if lowered_name in LOCAL_TOOL_NAMES:
        return "local_tool"
    if payload.get("url") or payload.get("endpoint"):
        return "remote_tool"
    if str(payload.get("transport") or "").lower() in {"http", "https", "sse", "websocket"}:
        return "remote_tool"
    if any(token in joined for token in ("slack", "jira", "notion", "linear", "google", "remote", "web")):
        return "remote_tool"
    if default_kind == "tool":
        return "local_tool"
    return default_kind


def _safe_capability_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    transport = payload.get("transport") or payload.get("type")
    if transport:
        metadata["transport"] = str(transport)
    command = payload.get("command") or payload.get("cmd")
    if command:
        metadata["command"] = Path(str(command)).name
    args = payload.get("args")
    if isinstance(args, list):
        metadata["args_count"] = len(args)
    env = payload.get("env")
    if isinstance(env, dict) and env:
        metadata["env_keys"] = sorted(str(key) for key in env.keys())
    url = payload.get("url") or payload.get("endpoint")
    if url:
        parsed = urlparse(str(url))
        metadata["host"] = parsed.netloc or parsed.path
    return metadata


def _dedupe_capabilities(capabilities: Sequence[IntegrationCapability]) -> List[IntegrationCapability]:
    deduped: List[IntegrationCapability] = []
    seen: set[Tuple[str, str, str]] = set()
    for capability in capabilities:
        key = (capability.name, capability.source, capability.kind)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(capability)
    return deduped

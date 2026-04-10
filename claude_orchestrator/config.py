"""Configuration loading and defaults for the local orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .compat import tomllib


@dataclass
class RetryPolicyConfig:
    base_delay_seconds: int = 15
    max_delay_seconds: int = 300
    max_attempts: int = 5
    jitter_ratio: float = 0.1


@dataclass
class LoggingConfig:
    level: str = "INFO"
    persist_payloads: bool = True
    json_log_path: str = "logs/claude-orchestrator.jsonl"


@dataclass
class StorageConfig:
    sqlite_path: str = "data/claude-orchestrator.db"


@dataclass
class WorkerConfig:
    max_concurrency: int = 4
    poll_interval_seconds: int = 5
    stale_after_seconds: int = 300
    max_batch_group_size: int = 25
    per_backend_concurrency: Dict[str, int] = field(
        default_factory=lambda: {
            "messages_api": 4,
            "message_batches": 1,
            "agent_sdk": 1,
            "claude_code_cli": 1,
        }
    )


@dataclass
class UiConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    refresh_seconds: int = 5


@dataclass
class PrivacyConfig:
    enabled: bool = False
    prompt_artifact_dir: str = "private"
    store_response_artifacts: bool = False


@dataclass
class MessagesApiBackendConfig:
    api_key_env: str = "ANTHROPIC_API_KEY"
    model: str = "claude-3-5-sonnet-latest"
    max_tokens: int = 2048
    timeout_seconds: int = 120
    base_url: Optional[str] = None


@dataclass
class MessageBatchesBackendConfig:
    enabled: bool = True
    poll_interval_seconds: int = 30
    max_batch_size: int = 20


@dataclass
class AgentSdkBackendConfig:
    enabled: bool = False
    api_key_env: str = "ANTHROPIC_API_KEY"
    workspace_root: str = "workspaces"
    allowed_tools: List[str] = field(default_factory=lambda: ["read", "write"])
    turn_limit: int = 20
    timeout_seconds: int = 900


@dataclass
class ClaudeCodeCliBackendConfig:
    enabled: bool = False
    executable: str = "claude"
    command_template: List[str] = field(default_factory=list)
    auth_check_command: List[str] = field(default_factory=list)
    hook_commands: List[str] = field(default_factory=list)
    timeout_seconds: int = 1800


@dataclass
class BackendsConfig:
    messages_api: MessagesApiBackendConfig = field(default_factory=MessagesApiBackendConfig)
    message_batches: MessageBatchesBackendConfig = field(default_factory=MessageBatchesBackendConfig)
    agent_sdk: AgentSdkBackendConfig = field(default_factory=AgentSdkBackendConfig)
    claude_code_cli: ClaudeCodeCliBackendConfig = field(default_factory=ClaudeCodeCliBackendConfig)


@dataclass
class AppConfig:
    default_backend: str = "messages_api"
    model: str = "claude-3-5-sonnet-latest"
    workspace_root: str = "workspaces"
    retry: RetryPolicyConfig = field(default_factory=RetryPolicyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    backends: BackendsConfig = field(default_factory=BackendsConfig)

    def sqlite_path(self, root: Path) -> Path:
        return _resolve_path(root, self.storage.sqlite_path)

    def workspace_path(self, root: Path) -> Path:
        return _resolve_path(root, self.workspace_root)

    def json_log_path(self, root: Path) -> Path:
        return _resolve_path(root, self.logging.json_log_path)


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def _merge_dataclass(instance: Any, overrides: Mapping[str, Any]) -> Any:
    for key, value in overrides.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, Mapping):
            _merge_dataclass(current, value)
        else:
            setattr(instance, key, value)
    return instance


def load_config(path: Optional[Path] = None) -> AppConfig:
    """Load configuration from TOML, falling back to defaults."""

    config = AppConfig()
    if path is None:
        default_path = Path.cwd() / "claude-orchestrator.toml"
        path = default_path if default_path.exists() else None
    if path and path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        _merge_dataclass(config, data)
    if config.model and not config.backends.messages_api.model:
        config.backends.messages_api.model = config.model
    return config


def render_example_config() -> str:
    """Return a complete example config."""

    return """default_backend = "messages_api"
model = "claude-3-5-sonnet-latest"
workspace_root = "workspaces"

[retry]
base_delay_seconds = 15
max_delay_seconds = 300
max_attempts = 5
jitter_ratio = 0.1

[logging]
level = "INFO"
persist_payloads = true
json_log_path = "logs/claude-orchestrator.jsonl"

[storage]
sqlite_path = "data/claude-orchestrator.db"

[worker]
max_concurrency = 4
poll_interval_seconds = 5
stale_after_seconds = 300
max_batch_group_size = 25

[worker.per_backend_concurrency]
messages_api = 4
message_batches = 1
agent_sdk = 1
claude_code_cli = 1

[ui]
host = "127.0.0.1"
port = 8000
refresh_seconds = 5

[privacy]
enabled = false
prompt_artifact_dir = "private"
store_response_artifacts = false

[backends.messages_api]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-3-5-sonnet-latest"
max_tokens = 2048
timeout_seconds = 120

[backends.message_batches]
enabled = true
poll_interval_seconds = 30
max_batch_size = 20

[backends.agent_sdk]
enabled = false
api_key_env = "ANTHROPIC_API_KEY"
workspace_root = "workspaces"
allowed_tools = ["read", "write"]
turn_limit = 20
timeout_seconds = 900

[backends.claude_code_cli]
enabled = false
executable = "claude"
command_template = []
auth_check_command = []
hook_commands = []
timeout_seconds = 1800
"""

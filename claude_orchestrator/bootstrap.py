"""Shared bootstrap helpers for CLI, workers, and the web UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .backends.factory import build_backend_registry
from .config import AppConfig, load_config
from .db import Database
from .logging_utils import configure_logging
from .repository import JobRepository


@dataclass
class ApplicationContext:
    root: Path
    config: AppConfig
    db: Database
    repository: JobRepository
    backends: Dict[str, object]


def bootstrap(root: Path, config_path: Optional[Path] = None) -> ApplicationContext:
    """Load config, initialize SQLite, and build backend registry."""

    config = load_config(config_path)
    db = Database(config.sqlite_path(root))
    db.initialize()
    configure_logging(config.logging.level, config.json_log_path(root))
    repository = JobRepository(db)
    backends = build_backend_registry(config)
    return ApplicationContext(
        root=root,
        config=config,
        db=db,
        repository=repository,
        backends=backends,
    )

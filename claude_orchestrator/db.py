"""SQLite database access and migration management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable, List

from importlib import resources


class Database:
    """Small SQLite wrapper with WAL mode and package-based migrations."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )
        self.apply_migrations()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def apply_migrations(self) -> None:
        with self.connect() as connection:
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, sql in self._iter_migrations():
                if version in applied:
                    continue
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )

    def _iter_migrations(self) -> Iterable[tuple[str, str]]:
        migration_root = resources.files("claude_orchestrator.migrations")
        files = sorted(
            (item for item in migration_root.iterdir() if item.name.endswith(".sql")),
            key=lambda item: item.name,
        )
        for item in files:
            yield item.name.removesuffix(".sql"), item.read_text(encoding="utf-8")

"""Structured logging helpers with JSON output and redaction."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from .redaction import redact_value
from .timeutils import utcnow


class JsonFormatter(logging.Formatter):
    """Simple JSON formatter suitable for local operator logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if isinstance(record.args, dict):
            payload["context"] = redact_value(record.args)
        extra_context = getattr(record, "context", None)
        if extra_context is not None:
            payload["context"] = redact_value(extra_context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact_value(payload), sort_keys=True)


def configure_logging(level: str, json_log_path: Path) -> None:
    """Configure root logging once for CLI and worker processes."""

    json_log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())
    root.addHandler(stream_handler)

    file_handler = logging.FileHandler(json_log_path)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

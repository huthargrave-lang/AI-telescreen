"""Diagnostics models for doctor pages and backend smoke tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BackendSmokeTestResult:
    backend: str
    provider: str
    ok: bool
    status: str
    summary: str
    details: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "backend": self.backend,
            "provider": self.provider,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "details": dict(self.details),
            "warnings": list(self.warnings),
        }


@dataclass
class DiagnosticCheck:
    name: str
    ok: bool
    status: str
    summary: str
    details: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass
class DiagnosticsReport:
    app_name: str = "AI Telescreen"
    config_path: Optional[str] = None
    default_backend: str = ""
    enabled_backends: List[str] = field(default_factory=list)
    workspace_root: Optional[str] = None
    database_path: Optional[str] = None
    checks: List[DiagnosticCheck] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    smoke_tests: Dict[str, BackendSmokeTestResult] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and all(
            result.ok for result in self.smoke_tests.values()
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "app_name": self.app_name,
            "ok": self.ok,
            "config_path": self.config_path,
            "default_backend": self.default_backend,
            "enabled_backends": list(self.enabled_backends),
            "workspace_root": self.workspace_root,
            "database_path": self.database_path,
            "checks": [check.to_dict() for check in self.checks],
            "warnings": list(self.warnings),
            "smoke_tests": {
                name: result.to_dict() for name, result in self.smoke_tests.items()
            },
        }

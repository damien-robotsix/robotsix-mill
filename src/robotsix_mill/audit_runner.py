"""Audit runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from .config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    AuditPassResult,
    PERIODIC_PASS_CONFIGS,
    _clone_token,
    run_periodic_pass,
)


def run_audit_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> AuditPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["audit"],
        settings=settings,
    )

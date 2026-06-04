"""Completeness-check runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    CompletenessCheckPassResult,
    PERIODIC_PASS_CONFIGS,
    run_periodic_pass,
)


def run_completeness_check_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> CompletenessCheckPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["completeness_check"],
        settings=settings,
    )

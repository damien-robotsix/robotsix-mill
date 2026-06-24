"""Frontend-sync runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    PeriodicPassResult,
    run_periodic_pass,
)


def run_frontend_sync_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> PeriodicPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["frontend_sync"],
        settings=settings,
    )

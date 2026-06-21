"""BC-check runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings
from .periodic_runner import (
    BcCheckPassResult,
    PERIODIC_PASS_CONFIGS,
    run_periodic_pass,
)


def run_bc_check_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> BcCheckPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["bc_check"],
        settings=settings,
    )

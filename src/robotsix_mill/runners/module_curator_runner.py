"""Module curator runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings
from .periodic_runner import (
    ModuleCuratorPassResult,
    PERIODIC_PASS_CONFIGS,
    run_periodic_pass,
)


def run_module_curator_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> ModuleCuratorPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["module_curator"],
        settings=settings,
    )

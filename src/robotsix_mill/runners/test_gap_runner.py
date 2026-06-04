"""Test-gap runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    TestGapPassResult,
    run_periodic_pass,
)


def run_test_gap_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> TestGapPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["test_gap"],
        settings=settings,
    )

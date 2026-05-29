"""Copy-paste runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from .config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    CopyPastePassResult,
    PERIODIC_PASS_CONFIGS,
    run_periodic_pass,
)


def run_copy_paste_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> CopyPastePassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["copy_paste"],
        settings=settings,
    )

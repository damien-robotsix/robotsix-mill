"""Forge-parity runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from typing import cast

from ..config import RepoConfig, Settings  # noqa: F401 — Settings kept for monkeypatch seam
from .periodic_runner import (
    ForgeParityPassResult,
    PERIODIC_PASS_CONFIGS,
    _clone_token,
    run_periodic_pass,
)


def run_forge_parity_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> ForgeParityPassResult:
    settings = Settings()
    return cast(
        ForgeParityPassResult,
        run_periodic_pass(
            session_id,
            repo_config,
            config=PERIODIC_PASS_CONFIGS["forge_parity"],
            settings=settings,
        ),
    )

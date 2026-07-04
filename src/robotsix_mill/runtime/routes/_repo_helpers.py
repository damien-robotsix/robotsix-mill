"""Shared helpers for repo_id → board_id resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from ...config import ReposRegistry


def _resolve_board_id(repo_id: str | None, repos: ReposRegistry) -> str:
    """Resolve a *repo_id* to a *board_id*.

    *repos* is the ``ReposRegistry`` instance (``request.app.state.repos``).

    Returns the *board_id* or raises ``HTTPException`` 400 when the
    repo is unknown or when *repo_id* is required but missing.
    """
    if repo_id == "meta":
        return "meta"
    if repo_id:
        if repo_id not in repos.repos:
            sorted_keys = sorted(repos.repos.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
            )
        return repos.repos[repo_id].repo_id
    if len(repos.repos) == 1:
        return next(iter(repos.repos.values())).repo_id
    sorted_keys = sorted(repos.repos.keys())
    raise HTTPException(
        status_code=400,
        detail=f"repo_id is required when multiple repos are configured. "
        f"Available repos: {sorted_keys}",
    )

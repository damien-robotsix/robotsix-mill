"""Operational monitoring + traces routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import (
    get_run_registry,
    get_settings,
    get_worker,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/runs")
def list_runs(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> list[dict]:
    """Return recent background-run entries (newest first).

    ``?repo_id=X`` filters to runs associated with that repo.
    When omitted, returns all (current behaviour preserved).
    """
    entries = registry.list_all()
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            # Filter entries that carry a repo_id matching the request.
            # Empty repo_id is treated as "applies to any repo" — covers
            # legacy entries filed before per-repo tagging landed plus
            # global runs from periodic agents that don't carry a
            # repo_id today. Strict equality on a non-empty filter would
            # hide every pre-wiring run in single-repo deployments.
            entries = [
                e
                for e in entries
                if e.get("repo_id") == repo_id or not e.get("repo_id")
            ]
    return entries


@router.get("/active")
def list_active(
    repo_id: str | None = None,
    request: Request = None,
    worker=Depends(get_worker),
) -> list[dict]:
    """Return tickets currently being processed by a pipeline stage.

    ``?repo_id=X`` filters to active tickets belonging to that repo.
    When omitted, returns all (current behaviour preserved).
    """
    active = [
        {"ticket_id": tid, "stage": info["stage"], "started_at": info["started_at"]}
        for tid, info in worker._active.items()
    ]
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            target_board = repos.repos[repo_id].board_id
            # Look up each active ticket's board_id from the service
            filtered = []
            for item in active:
                ticket = worker.ctx.service.get(item["ticket_id"])
                if ticket and ticket.board_id == target_board:
                    filtered.append(item)
            active = filtered
    return active


@router.get("/traces/recent")
def list_recent_traces(
    limit: int = 10,
    min_cost: float | None = None,
    max_cost: float | None = None,
    settings=Depends(get_settings),
) -> list[dict]:
    """Return recent Langfuse traces, filtered by cost and limited in
    count.  *limit* is clamped to 1–50; *min_cost* and *max_cost* are
    inclusive USD filters on ``totalCost``."""
    from ...langfuse_client import list_recent_traces as _list_recent

    limit = max(1, min(limit, 50))
    traces = _list_recent(
        settings,
        limit=limit,
        min_cost=min_cost,
        max_cost=max_cost,
    )
    return [
        {
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "timestamp": t.get("timestamp", ""),
            "sessionId": t.get("sessionId"),
            "totalCost": t.get("totalCost"),
            "userId": t.get("userId"),
        }
        for t in traces
    ]

"""Operational monitoring + traces routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import (
    get_run_registry,
    get_settings,
    get_worker,
)

from ...config import Settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["Traces"])


@router.get("/runs")
def list_runs(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> list[dict]:
    """Return recent background-run entries (newest first).

    ``?repo_id=X`` returns X's runs. Without it (or ``?repo_id=all``) the
    aggregate view UNIONS every per-repo registry. Periodic runs (audit,
    bc_check, health, …) are recorded into the per-repo registry, not the
    lead repo's, so reading only the default registry would hide them on
    the all-repos board even though they show on the per-repo board.
    """
    registries: dict = getattr(request.app.state, "run_registries", None) or {}

    if repo_id is None or repo_id == "all":
        seen: set = set()
        merged: list[dict] = []
        for reg in list(registries.values()) or [registry]:
            for e in reg.list_all():
                eid = e.get("id")
                if eid is not None and eid in seen:
                    continue
                if eid is not None:
                    seen.add(eid)
                merged.append(e)
        merged.sort(key=lambda e: e.get("started_at") or "", reverse=True)
        return merged

    # Specific repo: validate, then read THAT repo's registry (the Depends
    # already resolved it from the repo_id query param).
    # The synthetic meta board is a valid board id even though it is not a
    # registered repo — its runs live in the dedicated "meta" registry that
    # get_run_registry now resolves.
    repos = request.app.state.repos
    if repo_id != "meta" and repo_id not in repos.repos:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: "
            f"{sorted(repos.repos.keys())}",
        )
    # Empty repo_id on an entry == "applies to any repo" (legacy/global runs).
    return [
        e
        for e in registry.list_all()
        if e.get("repo_id") == repo_id or not e.get("repo_id")
    ]


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
        elif repo_id != "meta" and repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            # The synthetic meta board is a valid board id even though it is
            # not a registered repo; filter on its board_id directly.
            target_board = (
                "meta" if repo_id == "meta" else repos.repos[repo_id].board_id
            )
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
    inclusive USD filters on ``totalCost``.

    Each trace now includes an ``observationSummary`` with per-trace
    token counts, model, tool-call list, and error/warning counts so
    fleet-level cost analysis can attribute spend without fetching every
    trace individually."""
    from ...langfuse.client import list_recent_traces as _list_recent
    from ...langfuse.client import trace_observation_summary

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
            "observationSummary": trace_observation_summary(t),
        }
        for t in traces
    ]


@router.get("/traces/{trace_id}")
def get_trace_detail(
    trace_id: str,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return full Langfuse trace detail including all observations.

    Callers that need the complete prompt/completion bodies, per-
    observation token usage, or raw cost-details should use this
    endpoint (one call per trace) rather than ``/traces/recent``,
    which only returns aggregated summaries.
    """
    from ...langfuse.client import fetch_trace_detail

    detail = fetch_trace_detail(settings, trace_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Trace {trace_id!r} not found, or Langfuse is unconfigured / unreachable.",
        )
    return detail

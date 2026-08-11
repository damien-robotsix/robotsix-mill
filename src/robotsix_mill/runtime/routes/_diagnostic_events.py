"""``GET /diagnostic-events`` — diagnostic event store HTTP surface.

Exposes the per-board JSONL diagnostic event stores so operators and
health checks can observe the feedback loops without shell access.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ...agents.runners.diagnostic_events import (
    DiagnosticEvent,
    list_diagnostic_events,
)
from ...config import ReposRegistry, Settings
from ..deps import get_settings

router = APIRouter(tags=["DiagnosticEvents"])


class DiagnosticEventsResponse(BaseModel):
    """Response body for ``GET /diagnostic-events``."""

    events: list[dict[str, Any]]
    category_counts: dict[str, int]


def _resolve_board_ids(
    board_id: str | None,
    repos: ReposRegistry,
) -> list[str]:
    """Resolve *board_id* to one or more concrete board ids.

    When *board_id* is ``None`` or ``"all"``, return every registered
    board id plus the synthetic ``"meta"`` board.
    """
    if board_id is None or board_id == "all":
        return ["meta", *[rc.board_id for rc in repos.repos.values()]]
    if board_id == "meta":
        return ["meta"]
    rc = repos.repos.get(board_id)
    if rc is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown board: {board_id!r}. "
                f"Known boards: {sorted(repos.repos.keys())}"
            ),
        )
    return [rc.board_id]


@router.get("/diagnostic-events", response_model=DiagnosticEventsResponse)
def get_diagnostic_events(
    request: Request,
    board_id: str | None = Query(
        None,
        description="Board id to filter by; omit or pass 'all' for every board.",
    ),
    category: str | None = Query(
        None,
        description="Optional category filter (e.g. 'CI_FAILURE').",
    ),
    since: str | None = Query(
        None,
        description=(
            "ISO-8601 UTC datetime; only return events with timestamp "
            "strictly after this instant."
        ),
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="Maximum events to return (1–1000, default 100).",
    ),
    settings: Settings = Depends(get_settings),
) -> DiagnosticEventsResponse:
    """Return diagnostic events, newest first, with per-category counts.

    Reads the per-board JSONL event stores.  When *board_id* is omitted
    or ``"all"``, events are merged from every registered board (plus the
    synthetic ``"meta"`` board).
    """
    repos: ReposRegistry = request.app.state.repos
    board_ids = _resolve_board_ids(board_id, repos)

    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError, TypeError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid ISO-8601 datetime: {since!r}",
            ) from None

    all_events: list[DiagnosticEvent] = []
    for bid in board_ids:
        all_events.extend(list_diagnostic_events(settings, bid, category=category))

    # Filter by since (strictly after).
    if since_dt is not None:
        all_events = [
            e
            for e in all_events
            if _parse_timestamp(e.timestamp) is not None
            and _parse_timestamp(e.timestamp) > since_dt  # type: ignore[operator]
        ]

    # Newest first.
    all_events.sort(
        key=lambda e: _parse_timestamp(e.timestamp) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    # Apply limit.
    limited = all_events[:limit]

    # Build per-category counts from ALL events (before limit), so a
    # health check can assert "CI_FAILURE events in the last 24h > 0"
    # with a single call.
    category_counts: dict[str, int] = dict(Counter(e.category for e in all_events))

    return DiagnosticEventsResponse(
        events=[
            {
                "category": e.category,
                "ticket_id": e.ticket_id,
                "repo_id": e.repo_id,
                "reason": e.reason,
                "normalized_key": e.normalized_key,
                "timestamp": e.timestamp,
            }
            for e in limited
        ],
        category_counts=category_counts,
    )


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string; return ``None`` on failure."""
    try:
        return datetime.fromisoformat(ts)
    except ValueError, TypeError:
        return None

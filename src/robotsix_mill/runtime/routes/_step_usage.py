"""``GET /metrics/step-usage`` — server-side aggregation of per-call usage.

Reads the local SQLite mirror populated by ``record_step_usage()`` and
returns compact stage×model aggregates without touching Langfuse (and
therefore without transferring any prompt payloads).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ..deps import get_settings
from ..step_usage_store import aggregate

router = APIRouter(tags=["Metrics"])


def _parse_iso(value: str, name: str) -> datetime:
    """Parse an ISO-8601 datetime, assuming UTC when no offset is given."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError, TypeError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO-8601 datetime for {name}: {value!r}",
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@router.get("/metrics/step-usage")
def get_step_usage(
    since: str | None = Query(
        None,
        description=(
            "ISO-8601 datetime lower bound (inclusive). "
            "Defaults to 24 hours before `until`."
        ),
    ),
    until: str | None = Query(
        None,
        description="ISO-8601 datetime upper bound (exclusive). Defaults to now.",
    ),
    stage: str | None = Query(None, description="Optional stage_name filter."),
    model: str | None = Query(None, description="Optional model_name filter."),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return compact stage×model step-usage aggregates for a time window.

    One call aggregates every mirrored record in ``[since, until)`` into
    per stage×model statistics (call count; sum/mean/p50/p95/max input
    and output tokens; cache-read share) — no prompt payloads are read
    or returned.
    """
    until_dt = _parse_iso(until, "until") if until else datetime.now(UTC)
    since_dt = _parse_iso(since, "since") if since else until_dt - timedelta(hours=24)
    if since_dt >= until_dt:
        raise HTTPException(
            status_code=400,
            detail="`since` must be earlier than `until`.",
        )
    # An empty-string query value (e.g. ``?stage=&model=``) means "no
    # filter", matching how empty ``since``/``until`` already fall back to
    # their defaults above.
    return aggregate(
        data_dir=settings.data_dir,
        since=since_dt,
        until=until_dt,
        stage=stage or None,
        model=model or None,
    )

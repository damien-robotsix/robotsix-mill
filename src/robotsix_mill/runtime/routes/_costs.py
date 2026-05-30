"""Cost-analysis route handlers — trend, by-agent, most-expensive ticket & trace."""

from __future__ import annotations

from fastapi import Depends, Request

from ..deps import get_service, get_settings
from . import _resolve_cost_repo
from . import router


@router.get("/costs/trend")
def cost_trend(
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_id: str | None = None,
    request: Request = None,
    settings=Depends(get_settings),
) -> dict:
    """Return cost bucketed by time for the sparkline chart.

    ``?lookback_hours=N`` is clamped to [1, 168].
    ``?max_tickets=N`` switches to ticket-count mode (last N distinct
    sessions) instead of the lookback window.
    ``?repo_id=X`` scopes the query to a single repo's Langfuse project.
    ``?repo_id=all`` aggregates across all registered repos.
    When omitted in single-repo mode, the sole repo is used.
    When omitted in multi-repo mode, returns 400.
    """
    from ...langfuse_client import aggregate_cost_trend

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    if max_tickets is not None:
        max_tickets = max(1, min(max_tickets, 1000))
    repo_config = _resolve_cost_repo(repo_id, request)
    if isinstance(repo_config, list):
        # "all" — aggregate across repos
        all_buckets: dict[str, dict] = {}
        for rc in repo_config:
            buckets = aggregate_cost_trend(
                settings,
                lookback_hours,
                max_tickets=max_tickets,
                repo_config=rc,
            )
            for b in buckets:
                key = b["ts"]
                if key not in all_buckets:
                    all_buckets[key] = {"ts": key, "total_cost": 0.0, "trace_count": 0}
                all_buckets[key]["total_cost"] += b["total_cost"]
                all_buckets[key]["trace_count"] += b["trace_count"]
        return {"buckets": sorted(all_buckets.values(), key=lambda x: x["ts"])}
    buckets = aggregate_cost_trend(
        settings,
        lookback_hours,
        max_tickets=max_tickets,
        repo_config=repo_config,
    )
    return {"buckets": buckets}


@router.get("/costs/by-agent")
def cost_by_agent(
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_id: str | None = None,
    request: Request = None,
    settings=Depends(get_settings),
) -> list[dict]:
    """Return cost aggregated by agent/stage name for recent Langfuse
    traces within *lookback_hours* (clamped 1–168).

    ``?max_tickets=N`` switches to ticket-count mode (last N distinct
    sessions) instead of the lookback window.

    ``?repo_id=X`` scopes to a single repo; ``?repo_id=all`` aggregates
    across all repos.  Omitted in single-repo mode defaults to the sole
    repo; omitted in multi-repo returns 400.
    """
    from ...langfuse_client import aggregate_cost_by_name

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    if max_tickets is not None:
        max_tickets = max(1, min(max_tickets, 1000))
    repo_config = _resolve_cost_repo(repo_id, request)
    if isinstance(repo_config, list):
        # "all" — aggregate across repos
        agg: dict[str, dict] = {}
        for rc in repo_config:
            entries = aggregate_cost_by_name(
                settings,
                lookback_hours,
                max_tickets=max_tickets,
                repo_config=rc,
            )
            for e in entries:
                name = e["name"]
                if name not in agg:
                    agg[name] = {"name": name, "total_cost": 0.0, "trace_count": 0}
                agg[name]["total_cost"] += e["total_cost"]
                agg[name]["trace_count"] += e["trace_count"]
        result = list(agg.values())
        result.sort(key=lambda x: x["total_cost"], reverse=True)
        return result
    return aggregate_cost_by_name(
        settings,
        lookback_hours,
        max_tickets=max_tickets,
        repo_config=repo_config,
    )


@router.get("/costs/most-expensive-ticket")
def most_expensive_ticket_endpoint(
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_id: str | None = None,
    request: Request = None,
    settings=Depends(get_settings),
    svc=Depends(get_service),
):
    """Return the ticket with the highest total LLM cost in the last
    *lookback_hours* (clamped 1–168).  Returns ``null`` when there is
    no data, tracing is disabled, or the session has no matching ticket
    in the database.

    ``?max_tickets=N`` switches to ticket-count mode (last N distinct
    sessions) instead of the lookback window.

    ``?repo_id=X`` scopes to a single repo; ``?repo_id=all`` aggregates
    across all repos (picks the single most expensive across all).
    """
    from ...langfuse_client import most_expensive_ticket

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    if max_tickets is not None:
        max_tickets = max(1, min(max_tickets, 1000))
    repo_config = _resolve_cost_repo(repo_id, request)
    if isinstance(repo_config, list):
        # "all" — find the most expensive across all repos
        best: dict | None = None
        for rc in repo_config:
            result = most_expensive_ticket(
                settings,
                lookback_hours,
                max_tickets=max_tickets,
                repo_config=rc,
            )
            if result and (best is None or result["total_cost"] > best["total_cost"]):
                best = result
        result = best
    else:
        result = most_expensive_ticket(
            settings,
            lookback_hours,
            max_tickets=max_tickets,
            repo_config=repo_config,
        )

    if result is None:
        return None

    session_id = result["session_id"]
    ticket = svc.get(session_id)
    if ticket is None:
        return None

    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "cost_usd": result["total_cost"],
    }


@router.get("/costs/most-expensive-trace")
def most_expensive_trace_endpoint(
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_id: str | None = None,
    request: Request = None,
    settings=Depends(get_settings),
):
    """Return the single most expensive trace in the last
    *lookback_hours* (clamped 1–168).  Returns ``null`` when there is
    no data or tracing is disabled.

    ``?max_tickets=N`` switches to ticket-count mode (last N distinct
    sessions) instead of the lookback window.

    ``?repo_id=X`` scopes to a single repo; ``?repo_id=all`` aggregates
    across all repos (picks the single most expensive across all).
    """
    from ...langfuse_client import most_expensive_trace

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    if max_tickets is not None:
        max_tickets = max(1, min(max_tickets, 1000))
    repo_config = _resolve_cost_repo(repo_id, request)
    if isinstance(repo_config, list):
        best: dict | None = None
        for rc in repo_config:
            result = most_expensive_trace(
                settings,
                lookback_hours,
                max_tickets=max_tickets,
                repo_config=rc,
            )
            if result and (best is None or result["total_cost"] > best["total_cost"]):
                best = result
        return best
    return most_expensive_trace(
        settings,
        lookback_hours,
        max_tickets=max_tickets,
        repo_config=repo_config,
    )

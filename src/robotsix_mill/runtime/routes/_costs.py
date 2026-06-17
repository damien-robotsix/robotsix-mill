"""Cost analytics routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_service, get_settings
from ._tickets import _repo_config_for_ticket

log = logging.getLogger(__name__)

router = APIRouter(tags=["Costs"])


def _resolve_cost_repo(repo_id: str | None, request: Request):
    """Resolve a ``RepoConfig`` (or a list of them for "all") for cost endpoints.

    Returns:
        - ``None`` when *repo_id* is omitted and there's exactly one
          repo (backward compat — uses global secrets).
        - A single ``RepoConfig`` when *repo_id* names a known repo.
        - A list of ``RepoConfig`` when *repo_id* is ``"all"``.
        - Raises 400 for unknown *repo_id* or when *repo_id* is omitted
          in multi-repo mode.
    """
    repos = request.app.state.repos
    if repo_id is None:
        if len(repos.repos) == 1:
            return None  # single-repo: backward compat (global Secrets)
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"repo_id is required when multiple repos are configured. "
            f"Available repos: {sorted_keys} (or use repo_id=all)",
        )
    if repo_id == "all":
        return list(repos.repos.values())
    if repo_id not in repos.repos:
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
        )
    return repos.repos[repo_id]


def _normalize_cost_params(
    lookback_hours: float,
    max_tickets: int | None,
    repo_id: str | None,
    request: Request,
):
    """Normalize and clamp cost query parameters shared across cost endpoints.

    Returns ``(lookback_hours, max_tickets, repo_config_or_list)``.
    """
    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    if max_tickets is not None:
        max_tickets = max(1, min(max_tickets, 1000))
    repo_config = _resolve_cost_repo(repo_id, request)
    return lookback_hours, max_tickets, repo_config


def _aggregate_across_repos(repo_config, aggregator_fn, merge_fn, initial_acc):
    """Aggregate results across one or more repos.

    Calls ``aggregator_fn(rc)`` for each repo in *repo_config* (or once
    for a single config / ``None``) and folds the results into
    *initial_acc* via ``merge_fn(acc, result)``.

    Args:
        repo_config: ``RepoConfig``, ``None``, or ``list[RepoConfig]``.
        aggregator_fn: ``(RepoConfig | None) -> T`` — called per repo.
        merge_fn: ``(Acc, T) -> Acc`` — folds each result into the
            accumulator.
        initial_acc: starting accumulator value.

    Returns:
        The final accumulator after processing all repos.
    """
    acc = initial_acc
    repos = repo_config if isinstance(repo_config, list) else [repo_config]
    for rc in repos:
        result = aggregator_fn(rc)
        acc = merge_fn(acc, result)
    return acc


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
    from ...langfuse.client import aggregate_cost_trend

    lookback_hours, max_tickets, repo_config = _normalize_cost_params(
        lookback_hours, max_tickets, repo_id, request
    )

    def _agg(rc):
        return aggregate_cost_trend(
            settings, lookback_hours, max_tickets=max_tickets, repo_config=rc
        )

    def _merge(acc, buckets):
        for b in buckets:
            key = b["ts"]
            if key not in acc:
                acc[key] = {"ts": key, "total_cost": 0.0, "trace_count": 0}
            acc[key]["total_cost"] += b["total_cost"]
            acc[key]["trace_count"] += b["trace_count"]
        return acc

    all_buckets = _aggregate_across_repos(repo_config, _agg, _merge, {})
    return {"buckets": sorted(all_buckets.values(), key=lambda x: x["ts"])}


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
    from ...langfuse.client import aggregate_cost_by_name

    lookback_hours, max_tickets, repo_config = _normalize_cost_params(
        lookback_hours, max_tickets, repo_id, request
    )

    def _agg(rc):
        return aggregate_cost_by_name(
            settings, lookback_hours, max_tickets=max_tickets, repo_config=rc
        )

    def _merge(acc, entries):
        for e in entries:
            name = e["name"]
            if name not in acc:
                acc[name] = {"name": name, "total_cost": 0.0, "trace_count": 0}
            acc[name]["total_cost"] += e["total_cost"]
            acc[name]["trace_count"] += e["trace_count"]
        return acc

    agg = _aggregate_across_repos(repo_config, _agg, _merge, {})
    result = list(agg.values())
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return result


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
    from ...langfuse.client import most_expensive_ticket

    lookback_hours, max_tickets, repo_config = _normalize_cost_params(
        lookback_hours, max_tickets, repo_id, request
    )

    def _agg(rc):
        return most_expensive_ticket(
            settings, lookback_hours, max_tickets=max_tickets, repo_config=rc
        )

    def _merge(acc, result):
        if result and (acc is None or result["total_cost"] > acc["total_cost"]):
            return result
        return acc

    result = _aggregate_across_repos(repo_config, _agg, _merge, None)

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
    from ...langfuse.client import most_expensive_trace

    lookback_hours, max_tickets, repo_config = _normalize_cost_params(
        lookback_hours, max_tickets, repo_id, request
    )

    def _agg(rc):
        return most_expensive_trace(
            settings, lookback_hours, max_tickets=max_tickets, repo_config=rc
        )

    def _merge(acc, result):
        if result and (acc is None or result["total_cost"] > acc["total_cost"]):
            return result
        return acc

    return _aggregate_across_repos(repo_config, _agg, _merge, None)


@router.get("/tickets/{ticket_id}/cost-breakdown")
def cost_breakdown(
    ticket_id: str,
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Per-trace cost breakdown for a ticket, used by the drawer to
    overlay agent-step costs on history rows.

    The Langfuse sessionId equals the ticket id, so a single
    `/api/public/traces?sessionId=<ticket>` query returns every agent
    invocation tied to the ticket. Each entry carries
    ``{name, cost, at, trace_id}`` ordered by timestamp; the drawer's
    renderHistoryHtml matches the entries to history events by inferred
    agent name + nearest-in-time-≤ pairing.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    from ...langfuse.client import session_traces

    rows = session_traces(settings, ticket_id, repo_config=repo_config)
    if rows is None:
        return {"available": False, "traces": []}
    return {"available": True, "traces": rows}

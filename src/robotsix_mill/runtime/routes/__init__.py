"""HTTP route handlers for the robotsix-mill management-plane API.

All endpoints are registered on the package-level ``router``.  Each
concern lives in its own private sub-module:

* ``_misc``   — health, Langfuse status, repos list, gates, board, /runs, /active
* ``_tickets`` — ticket CRUD / lifecycle routes
* ``_costs``   — cost-trend, by-agent, most-expensive endpoints
* ``_agents``  — background agent-run triggers (audit, health, survey, …)
* ``_traces``  — deep-review trace inspection routes
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ...forge import get_forge  # noqa: F401 — re-exported for backward compat

log = logging.getLogger(__name__)

router = APIRouter()


def _repo_config_for_ticket(ticket, repos):
    """Resolve the ``RepoConfig`` for *ticket*'s ``board_id``.

    Returns ``None`` when the ticket has no ``board_id`` or the
    registry has no match (legacy tickets, single-repo mode).
    """
    if not ticket.board_id:
        return None
    for rc in repos.repos.values():
        if rc.board_id == ticket.board_id:
            return rc
    return None


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


def _resolve_agent_run_repos(repo_id: str | None, request: Request) -> list:
    """Resolve *repo_id* to a list of ``RepoConfig`` (or ``None``) for
    agent-run routes.

    Returns a list so the caller can iterate in ``_run()``, one pass
    per repo.  A ``None`` element means single-repo backward compat
    (the runner uses global secrets / memory paths).
    """

    repos = request.app.state.repos
    if repo_id is None:
        if len(repos.repos) <= 1:
            return [None]  # single-repo backward compat
        # Multi-repo, no repo_id → fan out across all repos.
        return list(repos.repos.values())
    if repo_id == "all":
        return list(repos.repos.values())
    if repo_id not in repos.repos:
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
        )
    return [repos.repos[repo_id]]


# Import sub-modules to register their routes on ``router``.
# These have the side-effect of decorating ``router`` with endpoints.
from . import _agents  # noqa: E402, F401
from . import _costs  # noqa: E402, F401
from . import _misc  # noqa: E402, F401
from . import _tickets  # noqa: E402, F401
from . import _traces  # noqa: E402, F401

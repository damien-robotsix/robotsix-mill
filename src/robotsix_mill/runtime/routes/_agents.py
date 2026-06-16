"""GET /agents — per-repo enabled on-demand agent names.

The board's "🤖 Agents ▾" dropdown lists the periodic agents a human
can run on demand. On a repo-specific board the menu must show only the
agents actually enabled for that repo, which mirrors the worker's
``_periodic_supervisor`` resolution: a periodic workflow runs for a repo
iff the repo ships ``.robotsix-mill/periodic/<name>.yaml`` (presence =
enabled, unless the YAML sets ``enabled: false``) AND the fleet-wide
``Settings.<name>_periodic`` kill-switch is not ``False``.

This route is read-only and side-effect free — it never starts an agent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from ...agents.periodic_loader import discover_periodic_workflows
from ..deps import get_repos_registry

log = logging.getLogger(__name__)

router = APIRouter(tags=["Agents"])


@router.get("/agents")
def list_enabled_agents(
    request: Request,
    repo_id: str | None = Query(None),
    repos=Depends(get_repos_registry),
) -> list[str]:
    """Return the periodic-agent names enabled for *repo_id*.

    When *repo_id* is missing, ``"all"``, or unknown, an empty list is
    returned — the per-repo agent run endpoints each target a single
    repo, so the aggregate board has nothing meaningful to offer (the
    frontend hides the dropdown there anyway).
    """
    if not repo_id or repo_id == "all":
        return []
    repo_config = repos.repos.get(repo_id)
    if repo_config is None:
        return []

    settings = request.app.state.settings
    # Reuse the worker's clone-dir resolver so we read the SAME
    # ``.robotsix-mill/periodic/`` files the scheduler honours.
    worker = request.app.state.worker
    clone_dir = worker._find_config_clone_dir(repo_config)

    enabled: list[str] = []
    for wf in discover_periodic_workflows(clone_dir):
        if not wf.enabled:
            continue
        # Fleet-wide kill-switch (matches worker._periodic_supervisor).
        if getattr(settings, f"{wf.name}_periodic", True) is False:
            continue
        enabled.append(wf.name)
    return enabled

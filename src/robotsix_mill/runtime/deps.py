"""FastAPI ``Depends`` callables and standalone utilities for route handlers.

Replaces the closure helpers (``_svc``, ``_maybe_enqueue``, ``_with_cost``)
that were previously defined inside ``create_app()``.
"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request

from ..config import RepoConfig, ReposRegistry, Settings
from ..core.models import Ticket, TicketRead
from ..core.service import TicketService
from ..core.states import STAGE_FOR_STATE, State
from ..forge import get_forge
from .broadcaster import BoardBroadcaster
from .run_registry import RunRegistry
from .worker import Worker


def get_service(request: Request) -> TicketService:
    """Return the ``TicketService`` stored on app state during lifespan startup."""
    return request.app.state.service


def get_worker(request: Request) -> Worker:
    """Return the ``Worker`` stored on app state during lifespan startup."""
    return request.app.state.worker


def get_settings(request: Request) -> Settings:
    """Return the ``Settings`` stored on app state during lifespan startup."""
    return request.app.state.settings


def get_broadcaster(request: Request) -> BoardBroadcaster:
    """Return the ``BoardBroadcaster`` stored on app state during lifespan startup."""
    return request.app.state.broadcaster


def get_run_registry(
    request: Request, repo_id: str | None = Query(None)
) -> RunRegistry:
    """Return the per-repo ``RunRegistry`` (lifespan creates one per
    board). Routes that pass ``?repo_id=X`` get X's registry; routes
    without that query fall back to ``app.state.run_registry`` —
    today the lead repo's, so legacy callers still record somewhere.
    """
    registries: dict[str, RunRegistry] = getattr(
        request.app.state, "run_registries", {}
    )
    if repo_id:
        repos: ReposRegistry = request.app.state.repos
        rc = repos.repos.get(repo_id)
        if rc is not None and rc.board_id in registries:
            return registries[rc.board_id]
    return request.app.state.run_registry


def get_repos_registry(request: Request) -> ReposRegistry:
    """Return the ``ReposRegistry`` stored on app state during lifespan startup."""
    return request.app.state.repos


def get_repo_config_for(
    repo_id: str | None = Query(None),
    repos: ReposRegistry = None,
    request: Request = None,
) -> RepoConfig | None:
    """Resolve a ``RepoConfig`` from a ``repo_id`` query param.

    When *repo_id* is provided but unknown, raises 400 immediately.
    When omitted, returns ``None`` — the caller decides the fallback
    (e.g. "all repos" for list endpoints, or per-ticket lookup).
    """
    if repo_id is None:
        return None
    repos_registry = repos or request.app.state.repos
    if repo_id not in repos_registry.repos:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: "
            f"{sorted(repos_registry.repos.keys())}",
        )
    return repos_registry.repos[repo_id]


def maybe_enqueue(ticket: Ticket, worker: Worker) -> None:
    """Enqueue *ticket* on the worker if its state has a pipeline stage."""
    if ticket.state in STAGE_FOR_STATE:
        worker.enqueue(ticket.id)


def _origin_session_url(
    ticket: Ticket, settings: Settings, repo_config: RepoConfig | None = None
) -> str | None:
    """Return a Langfuse web-UI session URL for *ticket*'s origin session.

    Delegates to :func:`tracing._build_langfuse_url` with
    ``entity_type="sessions"``.  Returns ``None`` when any ingredient is
    missing — no broken links.
    """
    from .tracing import _build_langfuse_url

    origin = ticket.origin_session
    if not origin:
        return None
    return _build_langfuse_url(origin, "sessions", repo_config=repo_config)


def with_cost(
    ticket: Ticket,
    settings: Settings,
    *,
    blocking: bool = True,
    repo_config: RepoConfig | None = None,
) -> Ticket:
    """Populate ``cost_usd`` on *ticket* (in-place) from the Langfuse session.

    Cost is NOT persisted — it lives in Langfuse; this is read-time
    only.  Mutates and returns the same object.

    When ``blocking=False`` the lookup is cache-only — returns the
    cached value if present, else 0.0, and never hits the network. Use
    this for list endpoints like /tickets which the board polls every
    1s; otherwise N cold-cache tickets would issue N serial Langfuse
    HTTP calls and the response would take seconds.

    When *repo_config* is provided, its Langfuse credentials are used
    for the cost lookup (per-repo isolation).
    """
    from ..langfuse_client import session_cost, session_cost_cached

    if blocking:
        ticket.cost_usd = session_cost(settings, ticket.id, repo_config=repo_config)
    else:
        ticket.cost_usd = session_cost_cached(ticket.id)
    return ticket


_REVIEW_STATES: frozenset[State] = frozenset(
    {
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.HUMAN_ISSUE_APPROVAL,
        State.FIXING_CI,
        State.REBASING,
        State.READY,
        State.DONE,
    }
)


def _pr_url(
    ticket: Ticket,
    settings: Settings,
    repo_config: RepoConfig | None = None,
) -> str | None:
    """Return the PR/merge-request URL for *ticket* from the forge, or ``None``.

    Only calls the forge when the ticket is in a review-relevant state
    and has (or can infer) a branch name.  Failures are silent — the
    enrichment must never crash the read path.

    *repo_config* routes the forge to the ticket's actual repo —
    without it the global ``forge_remote_url`` is hit, which yields
    a 404/empty for any non-lead repo's PR.
    """
    if ticket.state not in _REVIEW_STATES:
        return None
    branch = ticket.branch or f"{settings.branch_prefix}{ticket.id}"
    if not branch:
        return None
    try:
        pr = get_forge(settings, repo_config=repo_config).pr_status(
            source_branch=branch
        )
    except RuntimeError:
        return None  # forge not configured
    except Exception:
        return None  # transient — no crash
    if pr and pr.get("url"):
        return str(pr["url"])
    return None


def enrich_ticket_read(
    ticket: Ticket,
    settings: Settings,
    service: TicketService,
    *,
    blocking_cost: bool = True,
    fetch_pr_url: bool = True,
    repo_config: RepoConfig | None = None,
) -> TicketRead:
    """Convert a :class:`Ticket` into a :class:`TicketRead`, populating
    ``cost_usd`` from Langfuse, computing ``origin_session_url``, and
    resolving unmet dependencies.

    ``blocking_cost=False`` makes the cost lookup cache-only — used by
    the polled /tickets list endpoint to avoid N serial Langfuse calls
    on a cold cache stalling the response past the next poll tick.

    ``fetch_pr_url=False`` skips the per-ticket forge ``pr_status``
    call entirely. Same problem class: with N review-state tickets in
    the list, N serial GitHub API calls would stall the response. The
    drawer (per-ticket GET) keeps the full lookup so the PR link is
    authoritative when the user actually opens a ticket.

    *repo_config* is passed through to Langfuse lookups and the
    origin-session URL builder so per-repo project data is used.
    When ``None`` the global ``Secrets`` singleton is consulted.
    """
    with_cost(ticket, settings, blocking=blocking_cost, repo_config=repo_config)

    # Compute cumulative cost for any ticket with descendants.
    cumulative: float | None = None
    children = service.list_children(ticket.id)
    if children:
        cum = service.cumulative_cost(
            ticket.id,
            settings,
            blocking=blocking_cost,
            repo_config=repo_config,
        )
        # Only expose cumulative when it's meaningfully larger than direct.
        if cum > ticket.cost_usd:
            cumulative = cum

    parent_title: str | None = None
    if ticket.parent_id:
        parent = service.get(ticket.parent_id)
        if parent:
            parent_title = parent.title

    # Resolve each declared dependency to {id, title, state} so the
    # drawer can render a readable list instead of opaque IDs.
    import json as _json

    dependencies: list[dict] = []
    if ticket.depends_on:
        try:
            dep_ids = _json.loads(ticket.depends_on)
        except ValueError, TypeError:
            dep_ids = []
        if isinstance(dep_ids, list):
            for dep_id in dep_ids:
                if not isinstance(dep_id, str):
                    continue
                dep = service.get(dep_id)
                dependencies.append(
                    {
                        "id": dep_id,
                        "title": dep.title if dep else None,
                        "state": dep.state.value if dep else None,
                    }
                )
    return TicketRead(
        id=ticket.id,
        title=ticket.title,
        state=ticket.state,
        kind=ticket.kind,
        branch=ticket.branch,
        parent_id=ticket.parent_id,
        parent_title=parent_title,
        source=ticket.source,
        origin_session=ticket.origin_session,
        origin_session_url=_origin_session_url(
            ticket, settings, repo_config=repo_config
        ),
        cost_usd=ticket.cost_usd,
        cumulative_cost=cumulative,
        depends_on=ticket.depends_on,
        unmet_deps=service.unmet_dependencies(ticket),
        dependencies=dependencies,
        pr_url=_pr_url(ticket, settings, repo_config=repo_config)
        if fetch_pr_url
        else None,
        retry_attempt=ticket.retry_attempt,
        last_transient_error=ticket.last_transient_error,
        next_retry_at=ticket.next_retry_at,
        priority=bool(getattr(ticket, "priority", False)),
        board_id=getattr(ticket, "board_id", "") or "",
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )

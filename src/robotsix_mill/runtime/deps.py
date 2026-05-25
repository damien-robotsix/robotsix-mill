"""FastAPI ``Depends`` callables and standalone utilities for route handlers.

Replaces the closure helpers (``_svc``, ``_maybe_enqueue``, ``_with_cost``)
that were previously defined inside ``create_app()``.
"""

from __future__ import annotations

from fastapi import Request

from ..config import ReposRegistry, Settings, get_secrets
from ..core.models import Ticket, TicketRead
from ..core.service import TicketService
from ..core.states import STAGE_FOR_STATE, State
from ..forge import get_forge
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


def get_run_registry(request: Request) -> RunRegistry:
    """Return the ``RunRegistry`` stored on app state during lifespan startup."""
    return request.app.state.run_registry


def get_repos_registry(request: Request) -> ReposRegistry:
    """Return the ``ReposRegistry`` stored on app state during lifespan startup."""
    return request.app.state.repos


def maybe_enqueue(ticket: Ticket, worker: Worker) -> None:
    """Enqueue *ticket* on the worker if its state has a pipeline stage."""
    if ticket.state in STAGE_FOR_STATE:
        worker.enqueue(ticket.id)


def _origin_session_url(ticket: Ticket, settings: Settings) -> str | None:
    """Return a Langfuse web-UI session URL for *ticket*'s origin session.

    Returns ``None`` when any ingredient is missing — no broken links.
    """
    origin = ticket.origin_session
    secrets = get_secrets()
    base = secrets.langfuse_base_url
    project_id = secrets.langfuse_project_id
    if origin and base and project_id:
        return f"{base.rstrip('/')}/project/{project_id}/sessions/{origin}"
    return None


def with_cost(ticket: Ticket, settings: Settings, *, blocking: bool = True) -> Ticket:
    """Populate ``cost_usd`` on *ticket* (in-place) from the Langfuse session.

    Cost is NOT persisted — it lives in Langfuse; this is read-time
    only.  Mutates and returns the same object.

    When ``blocking=False`` the lookup is cache-only — returns the
    cached value if present, else 0.0, and never hits the network. Use
    this for list endpoints like /tickets which the board polls every
    5s; otherwise N cold-cache tickets would issue N serial Langfuse
    HTTP calls and the response would take seconds.
    """
    from ..langfuse_client import session_cost, session_cost_cached

    if blocking:
        ticket.cost_usd = session_cost(settings, ticket.id)
    else:
        ticket.cost_usd = session_cost_cached(ticket.id)
    return ticket


_REVIEW_STATES: frozenset[State] = frozenset({
    State.HUMAN_MR_APPROVAL,
    State.HUMAN_ISSUE_APPROVAL,
    State.FIXING_CI,
    State.REBASING,
    State.READY,
    State.DONE,
})


def _pr_url(ticket: Ticket, settings: Settings) -> str | None:
    """Return the PR/merge-request URL for *ticket* from the forge, or ``None``.

    Only calls the forge when the ticket is in a review-relevant state
    and has (or can infer) a branch name.  Failures are silent — the
    enrichment must never crash the read path.
    """
    if ticket.state not in _REVIEW_STATES:
        return None
    branch = ticket.branch or f"{settings.branch_prefix}{ticket.id}"
    if not branch:
        return None
    try:
        pr = get_forge(settings).pr_status(source_branch=branch)
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
    """
    with_cost(ticket, settings, blocking=blocking_cost)

    # Compute cumulative cost for any ticket with descendants.
    cumulative: float | None = None
    children = service.list_children(ticket.id)
    if children:
        cum = service.cumulative_cost(
            ticket.id, settings, blocking=blocking_cost
        )
        # Only expose cumulative when it's meaningfully larger than direct.
        if cum > ticket.cost_usd:
            cumulative = cum

    parent_title: str | None = None
    if ticket.parent_id:
        parent = service.get(ticket.parent_id)
        if parent:
            parent_title = parent.title
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
        origin_session_url=_origin_session_url(ticket, settings),
        cost_usd=ticket.cost_usd,
        cumulative_cost=cumulative,
        depends_on=ticket.depends_on,
        unmet_deps=service.unmet_dependencies(ticket),
        pr_url=_pr_url(ticket, settings) if fetch_pr_url else None,
        retry_attempt=ticket.retry_attempt,
        last_transient_error=ticket.last_transient_error,
        next_retry_at=ticket.next_retry_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )

"""FastAPI ``Depends`` callables and standalone utilities for route handlers.

Replaces the closure helpers (``_svc``, ``_maybe_enqueue``, ``_with_cost``)
that were previously defined inside ``create_app()``.
"""

from __future__ import annotations

from fastapi import Request

from ..config import Settings
from ..core.models import Ticket, TicketRead
from ..core.service import TicketService
from ..core.states import STAGE_FOR_STATE
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


def maybe_enqueue(ticket: Ticket, worker: Worker) -> None:
    """Enqueue *ticket* on the worker if its state has a pipeline stage."""
    if ticket.state in STAGE_FOR_STATE:
        worker.enqueue(ticket.id)


def _origin_session_url(ticket: Ticket, settings: Settings) -> str | None:
    """Return a Langfuse web-UI session URL for *ticket*'s origin session.

    Returns ``None`` when any ingredient is missing — no broken links.
    """
    origin = ticket.origin_session
    base = settings.langfuse_base_url
    project_id = settings.langfuse_project_id
    if origin and base and project_id:
        return f"{base.rstrip('/')}/project/{project_id}/sessions/{origin}"
    return None


def with_cost(ticket: Ticket, settings: Settings) -> Ticket:
    """Populate ``cost_usd`` on *ticket* (in-place) from the Langfuse session.

    Cost is NOT persisted — it lives in Langfuse; this is read-time
    only.  Mutates and returns the same object.
    """
    from ..langfuse_client import session_cost

    ticket.cost_usd = session_cost(settings, ticket.id)
    return ticket


def enrich_ticket_read(ticket: Ticket, settings: Settings) -> TicketRead:
    """Convert a :class:`Ticket` into a :class:`TicketRead`, populating
    ``cost_usd`` from Langfuse and computing ``origin_session_url``."""
    with_cost(ticket, settings)
    return TicketRead(
        id=ticket.id,
        title=ticket.title,
        state=ticket.state,
        branch=ticket.branch,
        parent_id=ticket.parent_id,
        source=ticket.source,
        origin_session=ticket.origin_session,
        origin_session_url=_origin_session_url(ticket, settings),
        cost_usd=ticket.cost_usd,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )

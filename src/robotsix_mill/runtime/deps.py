"""FastAPI ``Depends`` callables and standalone utilities for route handlers.

Replaces the closure helpers (``_svc``, ``_maybe_enqueue``, ``_with_cost``)
that were previously defined inside ``create_app()``.
"""

from __future__ import annotations

from fastapi import Request

from ..config import Settings
from ..core.models import Ticket
from ..core.service import TicketService
from ..core.states import STAGE_FOR_STATE
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


def maybe_enqueue(ticket: Ticket, worker: Worker) -> None:
    """Enqueue *ticket* on the worker if its state has a pipeline stage."""
    if ticket.state in STAGE_FOR_STATE:
        worker.enqueue(ticket.id)


def with_cost(ticket: Ticket, settings: Settings) -> Ticket:
    """Populate ``cost_usd`` on *ticket* (in-place) from the Langfuse session.

    Cost is NOT persisted — it lives in Langfuse; this is read-time
    only.  Mutates and returns the same object.
    """
    from ..langfuse_client import session_cost

    ticket.cost_usd = session_cost(settings, ticket.id)
    return ticket

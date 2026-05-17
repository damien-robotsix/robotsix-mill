"""FastAPI app = the management-plane service.

It owns the DB, the in-process worker, and the HTTP surface the CLI (and
a future web frontend) use. Emitting a ticket enqueues it; the worker
picks it up immediately and chains it through the pipeline.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ..config import Settings
from ..core import db
from ..core.models import (
    Ticket,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ..core.service import TicketService, TransitionError
from ..core.states import STAGE_FOR_STATE, State
from ..stages import StageContext
from . import tracing
from .worker import Worker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings)
        tracing.init(settings)
        service = TicketService(settings)
        ctx = StageContext(settings=settings, service=service)
        worker = Worker(ctx)
        app.state.settings = settings
        app.state.service = service
        app.state.worker = worker
        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(title="robotsix-mill", lifespan=lifespan)

    def _svc() -> TicketService:
        return app.state.service

    def _maybe_enqueue(ticket: Ticket) -> None:
        if ticket.state in STAGE_FOR_STATE:
            app.state.worker.enqueue(ticket.id)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/tickets", response_model=TicketRead, status_code=201)
    def create_ticket(body: TicketCreate) -> Ticket:
        ticket = _svc().create(body.title, body.description)
        _maybe_enqueue(ticket)  # "directly taken in charge"
        return ticket

    @app.get("/tickets", response_model=list[TicketRead])
    def list_tickets(state: State | None = None) -> list[Ticket]:
        return _svc().list(state=state)

    @app.get("/tickets/{ticket_id}", response_model=TicketRead)
    def get_ticket(ticket_id: str) -> Ticket:
        ticket = _svc().get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        return ticket

    @app.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
    def get_history(ticket_id: str) -> list[TicketEvent]:
        if _svc().get(ticket_id) is None:
            raise HTTPException(404, "ticket not found")
        return _svc().history(ticket_id)

    @app.get("/tickets/{ticket_id}/description")
    def get_description(ticket_id: str) -> dict:
        ticket = _svc().get(ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        return {"description": _svc().workspace(ticket).read_description()}

    @app.post("/tickets/{ticket_id}/transition", response_model=TicketRead)
    def transition(ticket_id: str, body: TicketTransition) -> Ticket:
        try:
            ticket = _svc().transition(ticket_id, body.state, body.note)
        except KeyError:
            raise HTTPException(404, "ticket not found") from None
        except TransitionError as e:
            raise HTTPException(409, str(e)) from None
        _maybe_enqueue(ticket)  # human unblock re-triggers the chain
        return ticket

    return app

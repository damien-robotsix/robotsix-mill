"""HTTP route handlers for the robotsix-mill management-plane API.

All endpoints are registered on a module-level ``APIRouter`` named
``router``.  Handlers use ``fastapi.Depends`` to obtain the service,
worker, and settings that were stored on ``app.state`` during lifespan
startup, replacing the closure-based helpers that were previously
defined inside ``create_app()``.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from ..core.models import (
    Ticket,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ..core.service import TransitionError
from ..core.states import State
from .board_html import BOARD_HTML
from .deps import get_service, get_settings, get_worker, maybe_enqueue, with_cost

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def board() -> str:
    return BOARD_HTML


@router.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(
    body: TicketCreate,
    svc=Depends(get_service),
    worker=Depends(get_worker),
) -> Ticket:
    ticket = svc.create(body.title, body.description)
    maybe_enqueue(ticket, worker)  # "directly taken in charge"
    return ticket


@router.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    state: State | None = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[Ticket]:
    return [with_cost(t, settings) for t in svc.list(state=state)]


@router.get("/tickets/{ticket_id}", response_model=TicketRead)
def get_ticket(
    ticket_id: str,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> Ticket:
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return with_cost(ticket, settings)


@router.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
def get_history(
    ticket_id: str,
    svc=Depends(get_service),
) -> list[TicketEvent]:
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    return svc.history(ticket_id)


@router.get("/tickets/{ticket_id}/description")
def get_description(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict:
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return {"description": svc.workspace(ticket).read_description()}


@router.post("/tickets/{ticket_id}/transition", response_model=TicketRead)
def transition(
    ticket_id: str,
    body: TicketTransition,
    svc=Depends(get_service),
    worker=Depends(get_worker),
) -> Ticket:
    try:
        ticket = svc.transition(ticket_id, body.state, body.note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)  # human unblock re-triggers the chain
    return ticket


@router.post("/tickets/{ticket_id}/approve", response_model=TicketRead)
def approve_ticket(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
) -> Ticket:
    try:
        ticket = svc.transition(
            ticket_id, State.READY, note="approved by human"
        )
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)  # implement picks it up from ready
    return ticket


@router.post("/tickets/{ticket_id}/resume-blocked", response_model=TicketRead)
def resume_blocked(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
) -> Ticket:
    """Resume a blocked ticket back to the state it was blocked from."""
    try:
        ticket = svc.resume_blocked(ticket_id)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)
    return ticket


@router.post("/audit", status_code=202)
def audit_pass() -> dict:
    """Kick off an audit pass in the BACKGROUND and return at once.

    The audit runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.
    """
    from ..audit_runner import run_audit_pass

    def _run() -> None:
        try:
            r = run_audit_pass()
            log.info(
                "audit pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception:  # noqa: BLE001 — background; just log
            log.exception("audit pass failed")

    threading.Thread(
        target=_run, name="audit-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/scout")
def scout_pass() -> dict:
    """Trigger a scout pass: reads memory, evaluates OpenRouter
    models, writes updated memory, creates draft tickets for model
    improvements.
    """
    from ..scout_runner import run_scout_pass

    try:
        result = run_scout_pass()
        return {
            "memory_updated": len(result.updated_memory) > 0,
            "tickets_created": result.drafts_created,
        }
    except Exception as e:
        raise HTTPException(500, f"scout pass failed: {e}") from None


@router.post("/trace-health", status_code=202)
def trace_health_check() -> dict:
    """Kick off a trace-health check in the BACKGROUND and return at
    once.  The check fetches Langfuse traces from the last 24h,
    detects unsessioned traces, and files a draft ticket if needed.
    No LLM — deterministic and fast.
    """
    from ..trace_health_runner import run_trace_health_check

    def _run() -> None:
        try:
            r = run_trace_health_check()
            if r.draft_created:
                log.info(
                    "trace-health check: draft created — "
                    "%d/%d traces unsessioned",
                    r.unsessioned_count,
                    r.total_traces,
                )
            else:
                log.info(
                    "trace-health check: no alert "
                    "(%d/%d traces unsessioned)",
                    r.unsessioned_count,
                    r.total_traces,
                )
        except Exception:  # noqa: BLE001 — background; just log
            log.exception("trace-health check failed")

    threading.Thread(
        target=_run, name="trace-health-check", daemon=True
    ).start()
    return {"status": "started"}

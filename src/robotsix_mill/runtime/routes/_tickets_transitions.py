"""State transitions & enrichment ticket routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...core.models import CommentCreate, TicketRead
from ...core.states import STAGE_FOR_STATE, State
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
    resolve_ticket_id,
)
from ._tickets import _repo_config_for_ticket

log = logging.getLogger(__name__)

router = APIRouter(tags=["Tickets"])


@router.post("/tickets/{ticket_id}/request-changes")
def request_changes(
    ticket_id: str,
    body: CommentCreate,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Add a comment AND transition from human_issue_approval back to draft
    in one atomic operation."""
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        comment, ticket = svc.request_changes(ticket_id, body.body, author=body.author)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return {
        "comment": comment,
        "ticket": enrich_ticket_read(ticket, settings, svc, repo_config=repo_config),
    }


@router.post("/tickets/{ticket_id}/priority", response_model=TicketRead)
def set_priority(
    ticket_id: str,
    body: dict,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Toggle the operator-controlled priority flag on a ticket.

    Body: ``{"priority": true|false}``.  Re-enqueues the ticket so the
    priority change is reflected in the next consumer pop.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    priority = bool(body.get("priority", False))
    try:
        changed_ids = svc.set_priority(ticket_id, priority)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    # Force a fresh enqueue with the new priority rank for every
    # ticket whose priority actually flipped — the target plus any
    # descendants that inherited the flag from an epic. `maybe_enqueue`
    # would short-circuit on the worker's _pending dedup, leaving the
    # stale rank in the heap (see worker.requeue_with_current_priority
    # for the rationale).
    for cid in changed_ids:
        ct = svc.get(cid)
        if ct is not None and ct.state in STAGE_FOR_STATE:
            worker.requeue_with_current_priority(cid)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/redraft")
def redraft(
    ticket_id: str,
    body: CommentCreate,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Redraft a ticket from any active state back to DRAFT with an
    optional comment."""
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        comment, ticket = svc.redraft(
            ticket_id, body.body or "", author=body.author or "user"
        )
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return {
        "comment": comment,
        "ticket": enrich_ticket_read(ticket, settings, svc, repo_config=repo_config),
    }


@router.post("/tickets/{ticket_id}/mark-done")
def mark_done(
    ticket_id: str,
    body: dict = Body({}),
    request: Request = None,  # type: ignore[assignment]
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Mark a ticket as DONE from any non-terminal state.

    Accepts an optional ``note`` in the JSON body that is recorded
    as the event note.  Returns the updated ticket on success, 404
    when the ticket is unknown, and 409 when the ticket is already in
    a terminal state or an epic.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        raw_note = body.get("note", "")
        note = str(raw_note) if raw_note else ""
        comment, ticket = svc.mark_done(ticket_id, note=note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/resume-blocked", response_model=TicketRead)
def resume_blocked(
    ticket_id: str,
    request: Request,
    body: dict[str, str] = Body({}),
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Resume a blocked or retrying ticket.

    For BLOCKED tickets, transitions back to the originating state.
    For retrying tickets (retry_attempt > 0 in any non-BLOCKED state),
    clears the retry metadata and re-enqueues immediately.

    Accepts an optional ``note`` in the JSON body. For a BLOCKED
    ticket, the note is recorded as a comment and — when resuming back
    into READY — clears the implement stage's stale-spec guard, so an
    explicit operator justification lets the retry proceed instead of
    immediately re-blocking on the unchanged-spec check.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    if ticket.state is State.BLOCKED:
        note = str(body.get("note", "") or "")
        try:
            ticket = svc.resume_blocked(ticket_id, note=note)
        except KeyError:
            raise HTTPException(404, "ticket not found") from None
    elif ticket.retry_attempt > 0:
        svc.set_retry_state(
            ticket_id,
            retry_attempt=0,
            last_transient_error=None,
            next_retry_at=None,
        )
        ticket = svc.get(ticket_id)
    else:
        raise HTTPException(
            409, f"ticket is not blocked or retrying (currently {ticket.state})"
        )

    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/cost-breakdown")
def cost_breakdown(
    ticket_id: str,
    # FastAPI injects Request and ignores the default; the implicit-Optional
    # form is intentional. Suppress the [assignment] error so its PEP-484
    # notes don't trip mypy-baseline's note-block sync.
    request: Request = None,  # type: ignore[assignment]
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Per-trace cost breakdown for a ticket, used by the drawer to
    overlay agent-step costs on history rows.

    The Langfuse sessionId is the repo-qualified ticket id
    (``<repo> · <ticket>``, applied inside ``session_traces``), so a
    single `/api/public/traces?sessionId=…` query returns every agent
    invocation tied to the ticket. Each entry carries
    ``{name, cost, at, trace_id}`` ordered by timestamp; the drawer's
    renderHistoryHtml matches the entries to history events by inferred
    agent name + nearest-in-time-≤ pairing.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    from ...langfuse.client import session_traces

    rows = session_traces(settings, ticket_id, repo_config=repo_config)
    if rows is None:
        return {"available": False, "traces": []}
    return {"available": True, "traces": rows}

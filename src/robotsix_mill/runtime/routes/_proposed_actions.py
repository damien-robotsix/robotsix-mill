"""Proposed-action routes — list, approve, reject.

Proposed actions are pending mutations (close, transition, comment,
relabel) written by periodic agents and held until a human approves or
rejects them. These endpoints expose that workflow to the board UI.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from ...core import db
from ...core.models import (
    ActionType,
    ProposedAction,
    ProposedActionStatus,
    Ticket,
)
from ...core.service import TicketService
from ...core.states import State
from ..deps import get_settings

log = logging.getLogger(__name__)

# Per-module router, aggregated by routes/__init__.py via include_router.
router = APIRouter()


class ProposedActionRead(BaseModel):
    """JSON shape returned to the board UI."""

    id: int
    source: str
    target_ticket_id: str
    action_type: str
    payload: str | None
    rationale: str
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None


def _to_read(pa: ProposedAction) -> ProposedActionRead:
    return ProposedActionRead(
        id=pa.id,
        source=pa.source,
        target_ticket_id=pa.target_ticket_id,
        action_type=pa.action_type.value,
        payload=pa.payload,
        rationale=pa.rationale,
        status=pa.status.value,
        created_at=pa.created_at.isoformat(),
        decided_at=pa.decided_at.isoformat() if pa.decided_at else None,
        decided_by=pa.decided_by,
    )


def _resolve_board(repo_id: str, request: Request):
    """Resolve *repo_id* to its ``RepoConfig`` or raise 400."""
    repos = request.app.state.repos
    if not repo_id or repo_id not in repos.repos:
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
        )
    return repos.repos[repo_id]


@router.get(
    "/proposed-actions",
    response_model=list[ProposedActionRead],
)
def list_proposed_actions(
    repo_id: str,
    request: Request,
    status: str = "pending",
    settings=Depends(get_settings),
) -> list[ProposedActionRead]:
    """List proposed actions for a repo.

    By default returns only pending entries. Pass ``status=all`` or a
    specific ``ProposedActionStatus`` value (e.g. ``approved``,
    ``rejected``, ``executed``, ``failed``) to filter differently.
    """
    rc = _resolve_board(repo_id, request)
    board_id = rc.board_id

    with db.session(settings, board_id) as s:
        stmt = (
            select(ProposedAction)
            .join(Ticket, ProposedAction.target_ticket_id == Ticket.id)
            .where(Ticket.board_id == board_id)
        )
        if status != "all":
            stmt = stmt.where(ProposedAction.status == status)
        stmt = stmt.order_by(ProposedAction.created_at.desc())
        rows = s.exec(stmt).all()

    return [_to_read(r) for r in rows]


def _execute_proposed_action(
    pa: ProposedAction,
    svc: TicketService,
    settings: db.Settings,
    board_id: str,
) -> None:
    """Apply *pa* to its target ticket.

    Raises ``ValueError`` for invalid payloads; raises
    ``KeyError`` / ``TransitionError`` from ``TicketService``
    for missing tickets / invalid transitions.
    """
    action_type = pa.action_type
    payload = pa.payload or "{}"

    if action_type == ActionType.CLOSE:
        svc.transition(
            pa.target_ticket_id,
            State.CLOSED,
            note="closed by proposed-action executor",
        )

    elif action_type == ActionType.TRANSITION:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError(
                f"TRANSITION payload is not valid JSON: {payload!r}"
            )
        to_state = payload_dict.get("to_state")
        if not to_state or to_state not in State._value2member_map_:
            raise ValueError(
                f"TRANSITION payload missing or invalid 'to_state': {payload!r}"
            )
        svc.transition(
            pa.target_ticket_id,
            State(to_state),
            note="transitioned by proposed-action executor",
        )

    elif action_type == ActionType.COMMENT:
        body = pa.payload or ""
        svc.add_comment(
            pa.target_ticket_id,
            body=body,
            author="proposed-action",
        )

    elif action_type == ActionType.RELABEL:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError:
            raise ValueError(
                f"RELABEL payload is not valid JSON: {payload!r}"
            )
        if "priority" not in payload_dict or not isinstance(
            payload_dict["priority"], bool
        ):
            raise ValueError(
                f"RELABEL payload missing or invalid 'priority' (bool): {payload!r}"
            )
        with db.session(settings, board_id) as s:
            ticket = s.get(Ticket, pa.target_ticket_id)
            if ticket is None:
                raise KeyError(pa.target_ticket_id)
            ticket.priority = payload_dict["priority"]
            s.add(ticket)
            s.commit()

    else:
        raise ValueError(f"Unknown action_type: {action_type}")


@router.post(
    "/proposed-actions/{pa_id}/approve",
    response_model=ProposedActionRead,
)
def approve_proposed_action(
    pa_id: int,
    repo_id: str,
    request: Request,
    settings=Depends(get_settings),
) -> ProposedActionRead:
    """Approve and execute a pending proposed action.

    Transitions the record to APPROVED, executes the action, then
    marks it EXECUTED on success (FAILED on error).
    """
    rc = _resolve_board(repo_id, request)
    board_id = rc.board_id

    with db.session(settings, board_id) as s:
        pa = s.get(ProposedAction, pa_id)
        if pa is None:
            raise HTTPException(404, "proposed action not found")
        if pa.status != ProposedActionStatus.PENDING:
            raise HTTPException(
                409,
                f"proposed action already {pa.status.value}"
                + (f" by {pa.decided_by}" if pa.decided_by else ""),
            )

        # Transition to APPROVED before executing.
        now = datetime.now(timezone.utc)
        pa.status = ProposedActionStatus.APPROVED
        pa.decided_at = now
        pa.decided_by = "human"
        s.add(pa)
        s.commit()
        s.refresh(pa)

    # Execute outside the first session to give the executor its own
    # clean session context.
    svc = TicketService(settings, board_id=board_id)
    try:
        _execute_proposed_action(pa, svc, settings, board_id)
    except Exception as exc:
        log.exception(
            "Proposed action %d (%s) execution failed: %s",
            pa_id,
            pa.action_type.value,
            exc,
        )
        with db.session(settings, board_id) as s:
            pa = s.get(ProposedAction, pa_id)
            if pa is not None:
                pa.status = ProposedActionStatus.FAILED
                s.add(pa)
                s.commit()
                s.refresh(pa)
        raise HTTPException(
            500,
            f"execution failed: {exc}",
        )

    # Mark EXECUTED.
    with db.session(settings, board_id) as s:
        pa = s.get(ProposedAction, pa_id)
        pa.status = ProposedActionStatus.EXECUTED
        s.add(pa)
        s.commit()
        s.refresh(pa)

    return _to_read(pa)


@router.post(
    "/proposed-actions/{pa_id}/reject",
    response_model=ProposedActionRead,
)
def reject_proposed_action(
    pa_id: int,
    repo_id: str,
    request: Request,
    settings=Depends(get_settings),
) -> ProposedActionRead:
    """Reject a pending proposed action without executing it."""
    rc = _resolve_board(repo_id, request)
    board_id = rc.board_id

    with db.session(settings, board_id) as s:
        pa = s.get(ProposedAction, pa_id)
        if pa is None:
            raise HTTPException(404, "proposed action not found")
        if pa.status != ProposedActionStatus.PENDING:
            raise HTTPException(
                409,
                f"proposed action already {pa.status.value}"
                + (f" by {pa.decided_by}" if pa.decided_by else ""),
            )

        now = datetime.now(timezone.utc)
        pa.status = ProposedActionStatus.REJECTED
        pa.decided_at = now
        pa.decided_by = "human"
        s.add(pa)
        s.commit()
        s.refresh(pa)

    return _to_read(pa)

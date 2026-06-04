"""Proposed-action review and approval routes.

Exposes the periodic-agent proposal queue so humans can review,
approve, or reject proposed mutations before they are applied.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core.models import ProposedAction, ProposedActionStatus
from ..deps import get_proposed_action_service

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/proposed-actions", response_model=list[ProposedAction])
def list_proposed_actions(
    status: ProposedActionStatus | None = None,
    source: str | None = None,
    board_id: str | None = None,
    svc=Depends(get_proposed_action_service),
) -> list[ProposedAction]:
    """List proposed actions, optionally filtered by status, source, or board."""
    return svc.list(status=status, source=source, board_id=board_id)


@router.get("/proposed-actions/{action_id}", response_model=ProposedAction)
def get_proposed_action(
    action_id: int,
    svc=Depends(get_proposed_action_service),
) -> ProposedAction:
    """Retrieve a single proposed action by id."""
    action = svc.get(action_id)
    if action is None:
        raise HTTPException(404, "proposed action not found")
    return action


@router.post("/proposed-actions/{action_id}/approve", response_model=ProposedAction)
def approve_proposed_action(
    action_id: int,
    svc=Depends(get_proposed_action_service),
) -> ProposedAction:
    """Approve a pending action, triggering the executor to apply the
    mutation to its target ticket."""
    action = svc.get(action_id)
    if action is None:
        raise HTTPException(404, "proposed action not found")
    if action.status != ProposedActionStatus.PENDING:
        raise HTTPException(
            409, f"action is already {action.status.value}, not pending"
        )
    try:
        return svc.approve(action_id, decided_by="human")
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    except RuntimeError as e:
        log.exception("executor failed for proposed action %d", action_id)
        raise HTTPException(500, str(e)) from None


@router.post("/proposed-actions/{action_id}/reject", response_model=ProposedAction)
def reject_proposed_action(
    action_id: int,
    body: dict = Body(...),
    svc=Depends(get_proposed_action_service),
) -> ProposedAction:
    """Reject a pending action with a mandatory reason."""
    action = svc.get(action_id)
    if action is None:
        raise HTTPException(404, "proposed action not found")
    if action.status != ProposedActionStatus.PENDING:
        raise HTTPException(
            409, f"action is already {action.status.value}, not pending"
        )
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "a non-empty reason is required to reject")
    try:
        return svc.reject(action_id, decided_by="human", reason=reason)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None

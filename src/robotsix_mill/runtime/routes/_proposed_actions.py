"""Proposed-action review routes — list, get, approve, reject."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...config import Settings
from ...core.models import ProposedAction, ProposedActionStatus
from ...core.service import TicketService
from ..deps import get_service, get_settings
from ._repo_helpers import _resolve_board_id

log = logging.getLogger(__name__)

router = APIRouter(tags=["Proposed Actions"])


@router.get("/proposed-actions", response_model=list[ProposedAction])
def list_proposed_actions(
    status: ProposedActionStatus | None = None,
    repo_id: str | None = None,
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[ProposedAction]:
    """List all proposed actions, ordered by ``created_at`` DESC.

    Optional query params: ``?status=pending``, ``?repo_id=X``.

    Multi-repo: when *repo_id* is omitted or ``"all"``, iterates all
    registered boards and aggregates results sorted by ``created_at``
    DESC.
    """
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos
    if repo_id and repo_id != "all":
        board_id = _resolve_board_id(repo_id, repos)
        services = [_TicketService(settings, board_id=board_id)]
    else:
        services = [
            _TicketService(settings, board_id=rc.board_id)
            for rc in repos.repos.values()
        ]
        services.append(_TicketService(settings, board_id="meta"))

    actions: list[ProposedAction] = []
    for s in services:
        try:
            # exclude_status=None: filter purely by ``status`` (the service
            # default would otherwise drop PENDING rows the route must show).
            actions.extend(s.list_proposed_actions(status=status, exclude_status=None))
        except Exception:
            log.exception("list_proposed_actions: failed to query board %r", s.board_id)

    # Re-sort merged list by created_at DESC.
    actions.sort(key=lambda a: a.created_at, reverse=True)
    return actions


@router.get("/proposed-actions/{action_id}", response_model=ProposedAction)
def get_proposed_action(
    action_id: int,
    repo_id: str | None = None,
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> ProposedAction:
    """Return a single proposed action by id; 404 on miss.

    For multi-repo disambiguation, accepts optional ``?repo_id=X``.
    When omitted, tries the lead board first, then fans out to all
    registered boards.
    """
    from ...core.service import TicketService as _TicketService

    action = svc.get_proposed_action(action_id)
    if action is not None:
        return action

    # Fan out to other boards.
    repos = request.app.state.repos
    for rc in repos.repos.values():
        if rc.board_id == svc.board_id:
            continue
        s = _TicketService(settings, board_id=rc.board_id)
        action = s.get_proposed_action(action_id)
        if action is not None:
            return action

    # Try the meta board.
    s = _TicketService(settings, board_id="meta")
    action = s.get_proposed_action(action_id)
    if action is not None:
        return action

    raise HTTPException(404, f"proposed action {action_id} not found")


def _resolve_service_for_action(
    action_id: int,
    repo_id: str | None,
    request: Request,
    settings: Settings,
    default_svc: TicketService,
) -> TicketService:
    """Return the TicketService that owns *action_id*, or raise 404.

    When *repo_id* is a specific repo (not ``None`` / ``"all"``),
    resolves to that board's service directly.  Otherwise tries the
    default service, then all registered boards, then the ``"meta"``
    board.
    """
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos

    if repo_id and repo_id != "all":
        board_id = _resolve_board_id(repo_id, repos)
        return _TicketService(settings, board_id=board_id)

    # Try the default service first.
    if default_svc.get_proposed_action(action_id) is not None:
        return default_svc

    # Fan out to other boards.
    for rc in repos.repos.values():
        if rc.board_id == default_svc.board_id:
            continue
        s = _TicketService(settings, board_id=rc.board_id)
        if s.get_proposed_action(action_id) is not None:
            return s

    # Try the meta board.
    s = _TicketService(settings, board_id="meta")
    if s.get_proposed_action(action_id) is not None:
        return s

    raise HTTPException(404, f"proposed action {action_id} not found")


@router.post(
    "/proposed-actions/{action_id}/approve",
    response_model=ProposedAction,
)
def approve_proposed_action(
    action_id: int,
    repo_id: str | None = None,
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
    svc: TicketService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> ProposedAction:
    """Approve a pending action (transitions to APPROVED then
    executes).  404 on unknown id, 400 if not PENDING.

    Accepts optional ``?repo_id=`` to disambiguate across boards."""
    service = _resolve_service_for_action(action_id, repo_id, request, settings, svc)
    try:
        return service.approve_proposed_action(action_id)
    except KeyError:
        raise HTTPException(404, f"proposed action {action_id} not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.post(
    "/proposed-actions/{action_id}/reject",
    response_model=ProposedAction,
)
def reject_proposed_action(
    action_id: int,
    repo_id: str | None = None,
    request: Request = None,  # type: ignore[assignment]  # injected by FastAPI
    svc: TicketService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> ProposedAction:
    """Reject a pending action (transitions to REJECTED, no
    execution).  404 on unknown id, 400 if not PENDING.

    Accepts optional ``?repo_id=`` to disambiguate across boards."""
    service = _resolve_service_for_action(action_id, repo_id, request, settings, svc)
    try:
        return service.reject_proposed_action(action_id)
    except KeyError:
        raise HTTPException(404, f"proposed action {action_id} not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

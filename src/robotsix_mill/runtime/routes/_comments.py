"""Comment CRUD + thread management routes."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException

from ...core.models import Comment, CommentCreate, TicketKind
from ..deps import get_service, get_settings, resolve_ticket_id

log = logging.getLogger(__name__)

router = APIRouter(tags=["Comments"])


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=Comment,
    status_code=201,
)
def add_comment(
    ticket_id: str,
    body: CommentCreate,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> Comment:
    """Add a comment to a ticket (any state).

    Set *parent_id* to reply to an existing comment, forming a
    threaded discussion.  Omit it (or pass ``null``) to start a new
    top-level thread.

    For epic tickets, the comment triggers a background re-processing:
    the epic is re-broken-down by the breakdown agent with the full
    comment history as operator direction, and net-new children are
    created.  Non-epic tickets are unaffected — the comment is simply
    persisted.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        comment = svc.add_comment(
            ticket_id, body.body, author=body.author, parent_id=body.parent_id
        )
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Fire-and-forget: re-process the epic in a daemon thread.
    ticket = svc.get(ticket_id)
    if ticket is not None and ticket.kind == TicketKind.EPIC:
        from ...runtime.worker import _run_epic_reprocess

        threading.Thread(
            target=_run_epic_reprocess,
            args=(ticket_id, body.body, settings, ticket.board_id),
            daemon=True,
        ).start()

    return comment


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=list[Comment],
)
def list_comments(
    ticket_id: str,
    svc=Depends(get_service),
) -> list[Comment]:
    """List all comments for a ticket, ordered oldest-first."""
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        return svc.list_comments(ticket_id)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None


@router.post("/comments/{comment_id}/close", response_model=Comment)
def close_thread(
    comment_id: int,
    ticket_id: str | None = None,
    svc=Depends(get_service),
) -> Comment:
    """Close a top-level comment thread to mark it as resolved.

    Pass ``ticket_id`` so the service resolves the correct per-board
    DB — Comment.id is per-board (not globally unique), so a bare
    ``comment_id`` lookup is ambiguous across repos.
    """
    try:
        return svc.close_thread(comment_id, ticket_id=ticket_id)
    except KeyError:
        raise HTTPException(404, "comment not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.post("/comments/{comment_id}/reopen", response_model=Comment)
def reopen_thread(
    comment_id: int,
    ticket_id: str | None = None,
    svc=Depends(get_service),
) -> Comment:
    """Reopen a previously-closed comment thread.

    Pass ``ticket_id`` so the service resolves the correct per-board
    DB — Comment.id is per-board (not globally unique).
    """
    try:
        return svc.reopen_thread(comment_id, ticket_id=ticket_id)
    except KeyError:
        raise HTTPException(404, "comment not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None

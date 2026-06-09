"""Board-specific HTTP endpoints for the robotsix-board integration.

Provides ``/board/cards`` (card list for JSON hydration) and
``/board/move/{card_id}/{target_status}`` (column move action).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.service import TransitionError
from ..board_adapter import MillBoardAdapter
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
)

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = MillBoardAdapter()


def _ticket_to_card(ticket, settings, svc):
    """Convert a Ticket to the card dict shape expected by robotsix-board's board.js.

    The board.js expects::

        {
            id: string,
            title: string,
            status: string,          // must match a column status_key
            badges: string[],        // optional
            timestamps: object,      // optional — { label: value, ... }
            merged: boolean,         // optional
            agent_badges: string[],  // optional
            source_badge: string,    // optional
        }
    """
    read = enrich_ticket_read(
        ticket, settings, svc, blocking_cost=False, fetch_pr_url=False
    )
    return {
        "id": _adapter.card_id(read),
        "title": _adapter.card_title(read),
        "status": read.state.value,
        "badges": _adapter.card_badges(read),
        "timestamps": _adapter.card_timestamps(read),
        "source_badge": read.source if read.source and read.source != "user" else "",
    }


@router.get("/board/cards")
def board_cards(
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[dict]:
    """Return all tickets as card objects for the board JS hydration.

    Mirrors ``GET /tickets`` but returns the flat card shape expected
    by robotsix-board's ``board.js`` instead of the full ``TicketRead``
    model.
    """
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos
    repo_id = request.query_params.get("repo_id")

    if repo_id and repo_id != "all":
        board_id = _resolve_board_id(repo_id, repos)
        services = [_TicketService(settings, board_id=board_id)]
    else:
        services = [
            _TicketService(settings, board_id=rc.board_id)
            for rc in repos.repos.values()
        ]
        services.append(_TicketService(settings, board_id="meta"))

    cards: list[dict] = []
    for s in services:
        try:
            tickets = s.list()
        except Exception:
            log.warning("Failed to list tickets from board service", exc_info=True)
            continue
        for t in tickets:
            cards.append(_ticket_to_card(t, settings, s))

    return cards


@router.post("/board/move/{card_id}/{target_status}")
def board_move(
    card_id: str,
    target_status: str,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Move a card to a new column (robotsix-board move action).

    Receives the ``POST`` from robotsix-board's ``board-card-move``
    form (JSON_HYDRATION mode).  Translates to a ticket state
    transition.
    """
    try:
        ticket = svc.transition(card_id, target_status, None)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None

    maybe_enqueue(ticket, worker)
    return {"ok": True, "id": card_id, "status": target_status}


def _resolve_board_id(repo_id: str, repos) -> str:
    """Resolve a repo_id to its board_id, falling back to repo_id itself."""
    rc = repos.repos.get(repo_id)
    return rc.board_id if rc else repo_id

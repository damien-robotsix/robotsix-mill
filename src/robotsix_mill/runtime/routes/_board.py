"""Board-specific HTTP endpoints for the robotsix-board integration.

Provides ``/board/cards`` (card list for JSON hydration) and
``/board/move/{card_id}/{target_status}`` (column move action).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...config import Settings
from ...core.models import Ticket
from ...core.states import State
from ..board_adapter import MillBoardAdapter
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
)

# Terminal states excluded from default board listings — matches the
# set in ``_tickets._LIST_TERMINAL_STATES`` (CLOSED, EPIC_CLOSED,
# ANSWERED). States with empty transition sets in the state machine.
_BOARD_LIST_TERMINAL: set[State] = {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}

log = logging.getLogger(__name__)
router = APIRouter(tags=["Board"])

_adapter = MillBoardAdapter()

# States whose cards should be sorted by updated_at descending (most
# recent first) instead of the default created_at ascending.
_CLOSED_TERMINAL_STATES: set[State] = {State.CLOSED, State.EPIC_CLOSED}


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
        "pending_question": read.pending_question,
    }


@router.get("/board/cards")
def board_cards(
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
    include_closed: bool = False,
) -> list[dict]:
    """Return all tickets as card objects for the board JS hydration.

    Mirrors ``GET /tickets`` but returns the flat card shape expected
    by robotsix-board's ``board.js`` instead of the full ``TicketRead``
    model.

    ``include_closed`` **defaults to False** — terminal states
    (CLOSED, EPIC_CLOSED, ANSWERED) are excluded.  Pass
    ``include_closed=true`` to retrieve them.

    Closed and epic-closed cards are sorted by ``updated_at``
    descending (most recent first); all other cards remain sorted by
    ``created_at`` ascending.
    """
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos
    repo_id = request.query_params.get("repo_id")

    if repo_id and repo_id != "all":
        board_id = _resolve_board_id(repo_id, repos)
        services = [_TicketService(settings, board_id=board_id)]
    else:
        services = [
            _TicketService(settings, board_id=rc.repo_id) for rc in repos.repos.values()
        ]
        services.append(_TicketService(settings, board_id="meta"))

    exclude = set(_BOARD_LIST_TERMINAL) if not include_closed else None

    # Collect all (ticket, settings, svc) tuples first, then sort.
    collected: list[tuple[Ticket, Settings, _TicketService]] = []
    for s in services:
        try:
            tickets = s.list(exclude_states=exclude)
        except Exception:
            log.warning("Failed to list tickets from board service", exc_info=True)
            continue
        for t in tickets:
            collected.append((t, settings, s))

    # Compound sort: closed / epic_closed cards sorted by updated_at
    # descending (group 1), everything else by created_at ascending
    # (group 0).
    collected.sort(
        key=lambda item: (
            0 if item[0].state not in _CLOSED_TERMINAL_STATES else 1,
            item[0].created_at.timestamp()
            if item[0].state not in _CLOSED_TERMINAL_STATES
            else -item[0].updated_at.timestamp(),
        )
    )

    return [_ticket_to_card(t, settings, s) for t, settings, s in collected]


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

    maybe_enqueue(ticket, worker)
    return {"ok": True, "id": card_id, "status": target_status}


def _resolve_board_id(repo_id: str, repos) -> str:
    """Resolve a repo_id to its board_id, falling back to repo_id itself."""
    rc = repos.repos.get(repo_id)
    return rc.repo_id if rc else repo_id

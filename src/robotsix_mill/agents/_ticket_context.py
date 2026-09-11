"""Shared helper for agent tools that need a TicketService bound to the
current ticket session.

Extracted from the duplicated lazy-import + guard + construction
pattern that appeared in ``reply_thread``, ``list_threads``,
``close_thread``, ``post_comment``, and ``ask_user``.
"""

from __future__ import annotations

from ..config import Settings
from ..core.service import TicketService


def current_ticket_service(
    settings: Settings,
    board_id: str = "",
) -> tuple[TicketService, str] | None:
    """Return ``(TicketService, ticket_id)`` for the current session.

    Lazily imports ``current_ticket_id`` from ``runtime.tracing`` and
    returns ``None`` when there is no active ticket session so each
    caller can format its own error message.

    *board_id* is threaded through to the ``TicketService`` so tools
    that operate on the current ticket (reply_to_thread, list_threads,
    close_thread, post_comment, ask_user) resolve the right per-board
    DB instead of falling through to the board-less default, which
    raises ``db._db_path: board_id is required`` in a multi-repo
    deployment.
    """
    from ..runtime.tracing import current_ticket_id

    ticket_id = current_ticket_id()
    if ticket_id is None:
        return None

    return TicketService(settings, board_id=board_id), ticket_id

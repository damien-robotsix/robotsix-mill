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
) -> tuple[TicketService, str] | None:
    """Return ``(TicketService, ticket_id)`` for the current session.

    Lazily imports ``current_ticket_id`` from ``runtime.tracing`` and
    returns ``None`` when there is no active ticket session so each
    caller can format its own error message.
    """
    from ..runtime.tracing import current_ticket_id

    ticket_id = current_ticket_id()
    if ticket_id is None:
        return None

    return TicketService(settings), ticket_id

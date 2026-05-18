"""Context variables for ticket-scoped cost attribution.

Set in ``process_ticket`` so every LLM completion during a ticket's
pipeline (refine, implement, retrospect, sub-agents, retries) tags its
cost to the correct ticket.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.service import TicketService

active_ticket_id: ContextVar[str | None] = ContextVar(
    "active_ticket_id", default=None
)
active_ticket_service: ContextVar["TicketService | None"] = ContextVar(
    "active_ticket_service", default=None
)

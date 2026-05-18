"""Per-ticket context for cost attribution.

A ``ContextVar`` carries the currently processing ticket's ID across
``asyncio.to_thread`` boundaries so the cost-instrumented model can
attribute LLM spend to the right ticket without threading it through
every agent's parameter list.

A module-level callback bridges the agents package (which must not
import from ``core.service``) to the ``TicketService.add_cost`` method.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable

active_ticket_id: ContextVar[str | None] = ContextVar(
    "active_ticket_id", default=None
)

_cost_callback: Callable[[str, float], None] | None = None


def set_cost_callback(cb: Callable[[str, float], None]) -> None:
    """Wire the cost-attribution callback (called once at startup)."""
    global _cost_callback
    _cost_callback = cb


def notify_cost(ticket_id: str, cost: float) -> None:
    """Forward a cost increment to the registered callback (no-op if
    none is set)."""
    if _cost_callback is not None:
        _cost_callback(ticket_id, cost)

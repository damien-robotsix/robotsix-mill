"""BoardBroadcaster — pushes ticket state changes to connected WebSocket
clients so the board UI can update in real time without polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from asyncio import Queue

from ..core.models import Ticket

log = logging.getLogger(__name__)


class BoardBroadcaster:
    """Manages connected WebSocket clients and broadcasts ticket updates.

    Each connected client gets an ``asyncio.Queue``.  The synchronous
    ``broadcast_sync`` method is safe to call from any thread — it
    schedules the async broadcast on the event loop.
    """

    def __init__(self) -> None:
        self._queues: list[Queue] = []

    def broadcast_sync(self, ticket: Ticket) -> None:
        """Schedule a broadcast of *ticket* to all connected clients.

        Thread-safe: may be called from the worker threadpool as well
        as the main event-loop thread.
        """
        payload = {
            "type": "ticket_update",
            "ticket": {
                "id": ticket.id,
                "title": ticket.title,
                "state": ticket.state.value,
                "kind": ticket.kind,
                "board_id": ticket.board_id,
                "priority": ticket.priority,
                "retry_attempt": ticket.retry_attempt,
                "updated_at": (
                    ticket.updated_at.isoformat() if ticket.updated_at else None
                ),
                "parent_id": ticket.parent_id,
                "source": ticket.source,
                "cost_usd": ticket.cost_usd,
                "cumulative_cost": getattr(ticket, "cumulative_cost", None),
            },
        }
        data = json.dumps(payload)
        # Schedule on the running loop.  If called from outside the
        # event loop (worker threadpool), schedule_broadcast is a
        # coroutine-safe way to push the JSON string onto each queue.
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self._broadcast_now, data)
        except RuntimeError:
            # No running loop — best-effort; nothing to broadcast to.
            pass

    def _broadcast_now(self, data: str) -> None:
        """Push *data* onto every connected client's queue.

        Must be called on the event-loop thread.
        """
        dead: list[Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    async def subscribe(self, initial_tickets: list[dict]) -> Queue:
        """Register a new WebSocket client.

        Returns an ``asyncio.Queue`` that the client can iterate to
        receive broadcast messages.  *initial_tickets* is sent as the
        first message (a ``ticket_list`` event) so the client doesn't
        need a separate HTTP fetch on connect.
        """
        q: Queue = asyncio.Queue()
        self._queues.append(q)
        # Send initial state as the first message.
        await q.put(json.dumps({"type": "ticket_list", "tickets": initial_tickets}))
        return q

    def unsubscribe(self, q: Queue) -> None:
        """Remove a client queue (called on disconnect)."""
        try:
            self._queues.remove(q)
        except ValueError:
            pass

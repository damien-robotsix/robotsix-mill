"""Unit tests for :mod:`robotsix_mill.runtime.broadcaster`.

Exercises :class:`BoardBroadcaster` directly: queue lifecycle
(``subscribe`` / ``unsubscribe``), thread-safe scheduling
(``broadcast_sync``), and the event-loop-thread push with dead-queue
cleanup (``_broadcast_now``).  No network or external I/O is involved.
"""

from __future__ import annotations

import asyncio
import json

from robotsix_mill.core.models import State, Ticket
from robotsix_mill.runtime.broadcaster import BoardBroadcaster


def _make_ticket() -> Ticket:
    return Ticket(id="t-1", title="Test Ticket", workspace_path="/tmp/t-1")


async def test_subscribe_returns_queue_and_sends_initial_list() -> None:
    bc = BoardBroadcaster()
    initial = [{"id": "t-1", "title": "Test Ticket"}]

    q = await bc.subscribe(initial)

    # Queue is registered internally.
    assert q in bc._queues
    # First message is the ticket_list event carrying the initial state.
    first = json.loads(q.get_nowait())
    assert first == {"type": "ticket_list", "tickets": initial}


async def test_broadcast_sync_inside_loop_pushes_to_queues() -> None:
    bc = BoardBroadcaster()
    q = await bc.subscribe([])
    q.get_nowait()  # discard the initial ticket_list message

    bc.broadcast_sync(_make_ticket())
    # call_soon_threadsafe schedules the push; yield so it runs.
    await asyncio.sleep(0)

    payload = json.loads(q.get_nowait())
    assert payload["type"] == "ticket_update"
    assert payload["ticket"]["id"] == "t-1"
    assert payload["ticket"]["state"] == State.DRAFT.value


def test_broadcast_sync_outside_loop_is_noop() -> None:
    bc = BoardBroadcaster()
    q: asyncio.Queue[str] = asyncio.Queue()
    bc._queues.append(q)

    # No running event loop — must be a graceful no-op, not an error.
    bc.broadcast_sync(_make_ticket())

    assert q.empty()


def test_broadcast_now_pushes_to_all_queues() -> None:
    bc = BoardBroadcaster()
    q1: asyncio.Queue[str] = asyncio.Queue()
    q2: asyncio.Queue[str] = asyncio.Queue()
    bc._queues.extend([q1, q2])

    bc._broadcast_now("hello")

    assert q1.get_nowait() == "hello"
    assert q2.get_nowait() == "hello"


def test_broadcast_now_empty_queue_list_is_noop() -> None:
    bc = BoardBroadcaster()
    # No queues registered — must not raise.
    bc._broadcast_now("hello")
    assert bc._queues == []


def test_broadcast_now_removes_dead_queue_on_queuefull() -> None:
    bc = BoardBroadcaster()
    full: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    full.put_nowait("preexisting")  # now at capacity -> QueueFull on next put
    alive: asyncio.Queue[str] = asyncio.Queue()
    bc._queues.extend([full, alive])

    bc._broadcast_now("data")

    # The full (dead) queue is dropped; the healthy one still receives.
    assert full not in bc._queues
    assert alive in bc._queues
    assert alive.get_nowait() == "data"


def test_unsubscribe_removes_queue() -> None:
    bc = BoardBroadcaster()
    q: asyncio.Queue[str] = asyncio.Queue()
    bc._queues.append(q)

    bc.unsubscribe(q)
    assert q not in bc._queues

    # Unsubscribing an unknown queue is a graceful no-op.
    bc.unsubscribe(q)
    assert bc._queues == []

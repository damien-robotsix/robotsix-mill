"""Priority-aware admission gate for the global concurrency cap.

Why this exists
---------------
Ticket ordering is **per board**: every repo owns its own
``asyncio.PriorityQueue`` sorted by ``(priority_rank, stage_rank, seq)``
(see :class:`~.core.Worker`).  That gets the intended order *within* a
repo — flagged tickets first, then whatever is closest to CLOSED.

But a consumer that wins its own queue then has to acquire the global
concurrency slot, and a plain :class:`asyncio.Semaphore` hands slots out
in strict **FIFO arrival order** with no idea what its waiters are
carrying.  With ~26 consumer tasks across ~21 boards contending for
``max_global_concurrency`` (3 in production), a flagged ticket that
instantly won its own queue then loses the global slot to whichever
board's non-flagged ticket happened to arrive at the semaphore first.
Observed effect: operators flag a ticket and watch unflagged work from
other repos start ahead of it.

This gate keeps the cap but orders the *waiters* by the same rank tuple
the per-board queues use, so priority becomes fleet-wide rather than
board-local.

Semantics
---------
* ``acquire(rank)`` blocks until a slot is free, waking the lowest rank
  first; ``seq`` breaks ties FIFO so equal-rank waiters keep arrival
  order and nothing starves within a rank.
* No barging: a caller only takes the fast path when there are no
  waiters, otherwise a stream of arrivals could jump a queued waiter.
* Cancellation-safe: a cancelled waiter removes itself, and a waiter
  cancelled *after* being granted a slot hands the slot to the next in
  line instead of leaking it.
"""

from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from typing import AsyncIterator

__all__ = ["PriorityGate"]

# Rank of work with no better information — sorts behind every ranked
# waiter but still ahead of nothing, so an unranked caller cannot starve
# a flagged ticket.
DEFAULT_RANK: tuple[int, int] = (1, 99)


class PriorityGate:
    """Bounded concurrency gate that admits its highest-ranked waiter first.

    Drop-in for the ``asyncio.Semaphore`` it replaces except that
    :meth:`acquire` takes the caller's rank tuple (lower = admitted
    sooner).
    """

    def __init__(self, value: int) -> None:
        if value < 1:
            raise ValueError("PriorityGate value must be >= 1")
        self._value = value
        # heap of (rank, seq, future); seq is unique so tuple comparison
        # never reaches the (non-orderable) future.
        self._waiters: list[tuple[tuple[int, int], int, asyncio.Future[None]]] = []
        self._seq = 0

    def locked(self) -> bool:
        """True when no slot is free right now (matches ``Semaphore.locked``)."""
        return self._value <= 0

    @property
    def waiting(self) -> int:
        """Number of callers currently queued for a slot."""
        return len(self._waiters)

    async def acquire(self, rank: tuple[int, int] = DEFAULT_RANK) -> None:
        """Take a slot, waiting behind any better-ranked caller."""
        if self._value > 0 and not self._waiters:
            self._value -= 1
            return
        self._seq += 1
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (rank, self._seq, fut)
        heapq.heappush(self._waiters, entry)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # Slot was already granted to us — pass it on rather
                # than leaking it out of the cap.
                self._release_slot()
            else:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    pass
                else:
                    heapq.heapify(self._waiters)
            raise

    def release(self) -> None:
        """Give a slot back and wake the best-ranked waiter."""
        self._release_slot()

    def _release_slot(self) -> None:
        self._value += 1
        while self._value > 0 and self._waiters:
            _rank, _seq, fut = heapq.heappop(self._waiters)
            if fut.cancelled():
                continue  # raced with cancellation; try the next one
            self._value -= 1
            fut.set_result(None)
            return

    @asynccontextmanager
    async def slot(self, rank: tuple[int, int] = DEFAULT_RANK) -> AsyncIterator[None]:
        """``async with gate.slot(rank):`` — acquire, run, always release."""
        await self.acquire(rank)
        try:
            yield
        finally:
            self.release()

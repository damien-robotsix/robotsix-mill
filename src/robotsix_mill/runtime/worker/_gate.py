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

Reserved slots
--------------
Ranking only orders the *waiters* — it does nothing when every slot is
already **held**.  When the whole fleet fell back to the slow keyed
model, hour-scale implement/ci_fix runs occupied every slot and the
cheap two-call classify stage (highest rank) still sat behind them for
30+ minutes: new tickets never classified, and operators re-filed
"stuck" requests as duplicates.  ``reserved`` fixes that by holding back
a small quota of slots that only *privileged* callers (the classify
stage) may occupy: unprivileged work is capped at ``value - reserved``
concurrent slots, leaving ``reserved`` always available to admit a cheap
classify promptly.  The total cap is unchanged — the reserved slots come
out of the same ``value``, they are not added on top of it.

Semantics
---------
* ``acquire(rank)`` blocks until a slot is free, waking the lowest rank
  first; ``seq`` breaks ties FIFO so equal-rank waiters keep arrival
  order and nothing starves within a rank.
* ``acquire(rank, reserved_ok=True)`` marks the caller as privileged, so
  it may take one of the ``reserved`` slots that unprivileged callers are
  held back from.
* No barging: a caller only takes the fast path when there are no
  waiters, otherwise a stream of arrivals could jump a queued waiter.
* Cancellation-safe: a cancelled waiter removes itself, and a waiter
  cancelled *after* being granted a slot hands the slot to the next in
  line instead of leaking it.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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

    def __init__(self, value: int, reserved: int = 0) -> None:
        if value < 1:
            raise ValueError("PriorityGate value must be >= 1")
        if reserved < 0:
            raise ValueError("PriorityGate reserved must be >= 0")
        self._value = value
        # Slots held back for privileged (reserved_ok) callers.  Clamp to
        # value - 1 so unprivileged work always keeps at least one slot —
        # a reserved count >= value would deadlock every non-classify stage.
        self._reserved = min(reserved, value - 1)
        # heap of (rank, seq, future, reserved_ok); seq is unique so tuple
        # comparison never reaches the (non-orderable) future.
        self._waiters: list[
            tuple[tuple[int, int], int, asyncio.Future[None], bool]
        ] = []
        self._seq = 0

    def locked(self) -> bool:
        """True when no slot is free right now (matches ``Semaphore.locked``)."""
        return self._value <= 0

    @property
    def waiting(self) -> int:
        """Number of callers currently queued for a slot."""
        return len(self._waiters)

    def _can_admit(self, reserved_ok: bool) -> bool:
        """True if a caller may take a slot right now.

        Privileged (``reserved_ok``) callers may take any free slot;
        unprivileged callers must leave the ``reserved`` slots free.
        """
        if self._value <= 0:
            return False
        return reserved_ok or self._value > self._reserved

    def _admissible_waiter_exists(self) -> bool:
        """True if any queued waiter could be admitted at the current value.

        Guards the fast path: without reservation a free slot never
        coexists with a queued waiter, but a reserved slot CAN linger while
        an ineligible unprivileged waiter sits queued.  A privileged
        newcomer must still be able to take that reserved slot, yet it must
        NOT barge past a waiter that is itself currently admissible.
        """
        return any(
            not fut.cancelled() and self._can_admit(reserved_ok)
            for _rank, _seq, fut, reserved_ok in self._waiters
        )

    async def acquire(
        self, rank: tuple[int, int] = DEFAULT_RANK, reserved_ok: bool = False
    ) -> None:
        """Take a slot, waiting behind any better-ranked caller.

        Set ``reserved_ok`` to let this caller draw on the reserved slots
        that unprivileged work is held back from.
        """
        if self._can_admit(reserved_ok) and not self._admissible_waiter_exists():
            self._value -= 1
            return
        self._seq += 1
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (rank, self._seq, fut, reserved_ok)
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
        # Grant to the best-ranked waiter this slot is *eligible* for: the
        # head may be an unprivileged waiter that the reservation still
        # holds back (value <= reserved), while a privileged classify
        # waiter behind it can be admitted.  Skipped waiters are restored.
        skipped: list[tuple[tuple[int, int], int, asyncio.Future[None], bool]] = []
        try:
            while self._value > 0 and self._waiters:
                entry = heapq.heappop(self._waiters)
                _rank, _seq, fut, reserved_ok = entry
                if fut.cancelled():
                    continue  # raced with cancellation; try the next one
                if not self._can_admit(reserved_ok):
                    skipped.append(entry)  # ineligible now; leave it queued
                    continue
                self._value -= 1
                fut.set_result(None)
                return
        finally:
            for entry in skipped:
                heapq.heappush(self._waiters, entry)

    @asynccontextmanager
    async def slot(
        self, rank: tuple[int, int] = DEFAULT_RANK, reserved_ok: bool = False
    ) -> AsyncIterator[None]:
        """``async with gate.slot(rank):`` — acquire, run, always release."""
        await self.acquire(rank, reserved_ok=reserved_ok)
        try:
            yield
        finally:
            self.release()

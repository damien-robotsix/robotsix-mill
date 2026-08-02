"""Priority-aware slot gate for the sandbox concurrency ceiling.

``threading`` rather than ``asyncio``: :func:`robotsix_mill.sandbox.run` is
synchronous and called from worker threads (stage handlers offload to
threads because the agent SDK is sync).

Why it is priority-aware
------------------------
Sandboxes are the real resource ceiling, so the cap lives here, where they
are created — every spawner goes through ``run()``. But that means the cap
is shared by two very different populations: ticket stages, which carry the
operator's priority flag, and the ~20 per-repo periodic passes (audit,
test-gap, survey, …), which carry nothing at all.

A plain ``BoundedSemaphore`` hands slots out in arrival order, so a flagged
ticket that already won its board queue *and* the global stage gate could
still sit behind a ``test_gap_workspace`` pass. This gate keeps the same
ceiling and the same bounded-wait behaviour, but admits the best-ranked
waiter first — the same ``(priority_rank, stage_rank)`` tuple the worker
queues use.

Callers that have no rank (the periodic passes) get :data:`DEFAULT_RANK`,
which sorts behind every flagged ticket but keeps arrival order among
themselves.
"""

from __future__ import annotations

import heapq
import threading
from contextvars import ContextVar
from time import monotonic

__all__ = ["DEFAULT_RANK", "PrioritySlots", "current_rank", "sandbox_rank"]

# Rank for a caller that declares none. Mirrors the worker's
# "not flagged" priority rank and its unknown-stage fallback, so an
# unranked sandbox never outranks a flagged ticket.
DEFAULT_RANK: tuple[int, int] = (1, 99)

# Set by the board consumer around a ticket's stage run. ``asyncio.to_thread``
# copies the current context into the worker thread, so a value set on the
# consumer task is visible to ``sandbox.run()`` deep inside ``stage.run``.
# Periodic passes never set it and therefore inherit DEFAULT_RANK.
sandbox_rank: ContextVar[tuple[int, int]] = ContextVar(
    "sandbox_rank", default=DEFAULT_RANK
)


def current_rank() -> tuple[int, int]:
    """Rank of the work running in this context, or :data:`DEFAULT_RANK`."""
    return sandbox_rank.get()


class PrioritySlots:
    """Bounded slot pool that admits its best-ranked waiter first.

    Ties break FIFO on arrival sequence, so equal-ranked callers keep their
    order and nothing starves within a rank.
    """

    def __init__(self, cap: int) -> None:
        if cap < 1:
            raise ValueError("PrioritySlots cap must be >= 1")
        self._cap = cap
        self._in_use = 0
        self._cv = threading.Condition()
        # heap of (rank, seq); seq is unique so comparison never ties.
        self._waiting: list[tuple[tuple[int, int], int]] = []
        self._seq = 0

    @property
    def cap(self) -> int:
        return self._cap

    def in_use(self) -> int:
        with self._cv:
            return self._in_use

    def acquire(self, rank: tuple[int, int], timeout: float) -> bool:
        """Take a slot, waiting at most *timeout* seconds.

        Returns True when a slot was taken, False on timeout. A caller only
        takes a slot when it is the best-ranked waiter, so a newcomer cannot
        barge past someone already queued.
        """
        deadline = monotonic() + timeout
        with self._cv:
            self._seq += 1
            entry = (rank, self._seq)
            heapq.heappush(self._waiting, entry)
            # A better-ranked arrival changes who should go next.
            self._cv.notify_all()
            try:
                while True:
                    if self._in_use < self._cap and self._waiting[0] == entry:
                        heapq.heappop(self._waiting)
                        self._in_use += 1
                        # The head changed — let the new head re-evaluate.
                        self._cv.notify_all()
                        return True
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        self._drop(entry)
                        return False
                    self._cv.wait(remaining)
            except BaseException:
                self._drop(entry)
                raise

    def release(self) -> None:
        with self._cv:
            if self._in_use <= 0:
                raise RuntimeError("PrioritySlots released more times than acquired")
            self._in_use -= 1
            self._cv.notify_all()

    def _drop(self, entry: tuple[tuple[int, int], int]) -> None:
        """Remove a waiter that gave up. Caller holds the condition."""
        try:
            self._waiting.remove(entry)
        except ValueError:
            return
        heapq.heapify(self._waiting)
        self._cv.notify_all()

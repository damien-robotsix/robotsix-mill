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

import contextlib
import heapq
import threading
from collections.abc import Callable
from contextvars import ContextVar
from time import monotonic

# How often a waiter re-checks its ``abandoned`` predicate while queued.
_ABANDON_POLL_S = 0.5

__all__ = [
    "DEFAULT_RANK",
    "PrioritySlots",
    "StageAbandonedError",
    "current_abandon",
    "current_lane",
    "current_rank",
    "raise_if_abandoned",
    "sandbox_abandon",
    "sandbox_lane",
    "sandbox_rank",
]

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


def _record_slot_wait(seconds: float) -> None:
    """Accumulate sandbox slot-contention wall time into the phase-timing
    accumulator.

    Imported lazily and best-effort so instrumentation can never break slot
    acquisition; ``runtime.tracing`` does not import ``sandbox``, so there is
    no import cycle.  No-op when no accumulator is installed (e.g. periodic
    passes outside an instrumented implement run).
    """
    with contextlib.suppress(Exception):
        from ..runtime.tracing import add_phase_time

        add_phase_time("sandbox_slot_wait", seconds)


# Which slot pool a sandbox command takes. ``"heavy"`` (default) is the
# ``max_global_concurrency`` pool that implement/ci_fix test runs hold for
# minutes; ``"light"`` is the small ``sandbox_light_slots`` pool reserved for
# the explore scout's read-only greps/finds/reads. Before the lane existed
# (2026-09-07) 64 % of explore attempts were killed at the 90 s timeout while
# their greps queued behind implement pytest runs at the same rank — the scout
# model itself was idle ~80 s of the 90 (ticket 79a2).
sandbox_lane: ContextVar[str] = ContextVar("sandbox_lane", default="heavy")

# Cooperative abandonment. ``asyncio.wait_for`` cancels the awaiting coroutine,
# but the fs tool runs ``sandbox.run()`` in a worker thread that keeps waiting
# for a slot and then launches the ``docker run`` anyway — a container whose
# result nobody reads (three ``mill-sbx-*`` spawned within 2 s while 3
# implements ran, 2026-09-07 23:15Z). The explore runner sets a fresh Event
# per attempt and sets it when the attempt is cancelled; slot acquisition polls
# it and the spawn is skipped once it fires.
sandbox_abandon: ContextVar[threading.Event | None] = ContextVar(
    "sandbox_abandon", default=None
)


def current_lane() -> str:
    """Slot lane of the work running in this context (``heavy``/``light``)."""
    return sandbox_lane.get()


class StageAbandonedError(RuntimeError):
    """Raised by a tool when the run that owns it has been abandoned.

    The worker's stage deadline cancels the ``asyncio.to_thread`` coroutine,
    but Python cannot stop the thread: the agent loop inside kept calling the
    model and editing the workspace for 15+ min after its ``STALL`` on
    2026-09-09 (ticket 9d32), racing the transient retry that had already
    re-cloned the same workspace. Tools raise this at their entry once the
    stage-level abandon Event is set, so the pydantic-ai loop unwinds on its
    next tool call instead of running to its own end.
    """


def raise_if_abandoned(what: str) -> None:
    """Raise :class:`StageAbandonedError` when the current context's abandon
    Event is set (no-op when there is no Event or it is clear).
    """
    ev = sandbox_abandon.get()
    if ev is not None and ev.is_set():
        raise StageAbandonedError(
            f"{what}: the stage run owning this tool was abandoned "
            "(deadline expired) — stop; a fresh run has taken over the ticket"
        )


def current_abandon() -> threading.Event | None:
    """Abandon event of the work running in this context, if any."""
    return sandbox_abandon.get()


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

    def acquire(
        self,
        rank: tuple[int, int],
        timeout: float,
        abandoned: Callable[[], bool] | None = None,
    ) -> bool:
        """Take a slot, waiting at most *timeout* seconds.

        Returns True when a slot was taken, False on timeout — or as soon as
        *abandoned()* returns True (polled at least every
        :data:`_ABANDON_POLL_S`): a waiter whose caller has given up leaves
        the queue instead of holding a place for a spawn nobody will read.
        A caller only takes a slot when it is the best-ranked waiter, so a
        newcomer cannot barge past someone already queued.
        """
        entry_time = monotonic()
        deadline = entry_time + timeout
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
                        # Account the entry->grant wall time (slot contention)
                        # into the phase-timing accumulator (best-effort).
                        _record_slot_wait(monotonic() - entry_time)
                        return True
                    remaining = deadline - monotonic()
                    if remaining <= 0 or (abandoned is not None and abandoned()):
                        self._drop(entry)
                        return False
                    self._cv.wait(
                        remaining
                        if abandoned is None
                        else min(remaining, _ABANDON_POLL_S)
                    )
            except BaseException:
                self._drop(entry)
                raise

    def release(self) -> None:
        """Return one held slot to the pool and wake the best-ranked waiter.

        Raises:
            RuntimeError: if called more times than the slot was acquired, i.e.
                the in-use counter is already at zero.
        """
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

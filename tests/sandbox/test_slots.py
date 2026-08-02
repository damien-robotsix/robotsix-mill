"""Unit tests for the priority-aware sandbox slot pool."""

import threading

import pytest

from robotsix_mill.sandbox._slots import (
    DEFAULT_RANK,
    PrioritySlots,
    current_rank,
    sandbox_rank,
)


def test_rejects_non_positive_cap():
    with pytest.raises(ValueError):
        PrioritySlots(0)


def test_acquire_and_release_track_usage():
    pool = PrioritySlots(2)
    assert pool.acquire((0, 0), 1)
    assert pool.in_use() == 1
    assert pool.acquire((1, 5), 1)
    assert pool.in_use() == 2
    pool.release()
    assert pool.in_use() == 1


def test_cap_is_enforced():
    """The ceiling is the whole point — sandboxes are what cost memory."""
    pool = PrioritySlots(1)
    assert pool.acquire((0, 0), 1)
    assert not pool.acquire((0, 0), 0.05), "cap exceeded"
    pool.release()
    assert pool.acquire((0, 0), 1)


def test_release_without_acquire_is_an_error():
    """Mirrors BoundedSemaphore: over-release would inflate the ceiling."""
    pool = PrioritySlots(1)
    with pytest.raises(RuntimeError):
        pool.release()


def _queue_waiter(pool, rank, admitted, name, started):
    def body():
        started.set()
        if pool.acquire(rank, 5):
            admitted.append(name)
            pool.release()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    started.wait(2)
    return t


def test_admits_best_rank_first_regardless_of_arrival():
    """A flagged ticket must beat periodic passes that queued earlier —
    a plain BoundedSemaphore could not do this."""
    pool = PrioritySlots(1)
    assert pool.acquire((0, 0), 1)  # occupy the only slot

    admitted: list[str] = []
    threads = []
    # Arrival order is the reverse of the desired admission order. Each
    # waiter is fully queued before the next starts, so arrival order is
    # deterministic.
    for name, rank in (
        ("periodic-pass", DEFAULT_RANK),
        ("unflagged-draft", (1, 12)),
        ("flagged-ticket", (0, 12)),
    ):
        started = threading.Event()
        threads.append(_queue_waiter(pool, rank, admitted, name, started))
        # Wait until this waiter is actually queued before adding the next.
        deadline = threading.Event()
        deadline.wait(0.05)

    pool.release()  # ranked admission decides who goes
    for t in threads:
        t.join(5)

    assert admitted[0] == "flagged-ticket", (
        f"best rank must be admitted first; got {admitted}"
    )
    assert admitted == ["flagged-ticket", "unflagged-draft", "periodic-pass"]


def test_equal_ranks_keep_arrival_order():
    """Ties break FIFO so nothing starves within a rank."""
    pool = PrioritySlots(1)
    assert pool.acquire(DEFAULT_RANK, 1)

    admitted: list[str] = []
    threads = []
    for name in ("first", "second", "third"):
        started = threading.Event()
        threads.append(_queue_waiter(pool, DEFAULT_RANK, admitted, name, started))
        threading.Event().wait(0.05)

    pool.release()
    for t in threads:
        t.join(5)

    assert admitted == ["first", "second", "third"]


def test_timed_out_waiter_does_not_hold_a_slot():
    """A giving-up waiter must not ratchet the ceiling down."""
    pool = PrioritySlots(1)
    assert pool.acquire((0, 0), 1)
    assert not pool.acquire((0, 0), 0.05)
    pool.release()
    # Full capacity is back.
    assert pool.acquire((0, 0), 1)
    assert pool.in_use() == 1


def test_timed_out_waiter_does_not_block_the_queue_behind_it():
    """A better-ranked waiter that times out must be removed from the head,
    or everyone behind it waits forever for a slot it will never take."""
    pool = PrioritySlots(1)
    assert pool.acquire((0, 0), 1)

    admitted: list[str] = []
    started = threading.Event()
    behind = _queue_waiter(pool, (1, 5), admitted, "behind", started)

    # A better-ranked waiter arrives and gives up.
    assert not pool.acquire((0, 0), 0.05)

    pool.release()
    behind.join(5)
    assert admitted == ["behind"]


def test_context_var_defaults_and_scopes():
    assert current_rank() == DEFAULT_RANK
    token = sandbox_rank.set((0, 3))
    try:
        assert current_rank() == (0, 3)
    finally:
        sandbox_rank.reset(token)
    assert current_rank() == DEFAULT_RANK


def test_default_rank_sorts_behind_flagged_work():
    assert DEFAULT_RANK > (0, 99)

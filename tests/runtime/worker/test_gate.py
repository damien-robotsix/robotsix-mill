"""Unit tests for the priority-aware global admission gate."""

import asyncio

import pytest

from robotsix_mill.runtime.worker._gate import DEFAULT_RANK, PriorityGate


def test_rejects_non_positive_capacity():
    """A zero/negative cap would deadlock every consumer — reject it up front."""
    with pytest.raises(ValueError):
        PriorityGate(0)


async def test_uncontended_acquire_is_immediate():
    gate = PriorityGate(2)
    await gate.acquire((0, 0))
    assert not gate.locked()  # one of two slots still free
    await gate.acquire((1, 9))
    assert gate.locked()
    gate.release()
    assert not gate.locked()


async def test_admits_best_rank_first_regardless_of_arrival_order():
    """The whole point: a later-arriving flagged ticket beats earlier
    unflagged ones, which a FIFO semaphore could never do."""
    gate = PriorityGate(1)
    await gate.acquire((0, 0))  # occupy the only slot
    admitted: list[str] = []

    async def waiter(name: str, rank: tuple[int, int]) -> None:
        await gate.acquire(rank)
        admitted.append(name)

    # Arrival order is deliberately the reverse of the desired order.
    tasks = [
        asyncio.create_task(waiter("draft-other-board", (1, 12))),
        asyncio.create_task(waiter("review-other-board", (1, 3))),
        asyncio.create_task(waiter("priority-draft", (0, 12))),
    ]
    await asyncio.sleep(0)  # let all three queue up
    assert gate.waiting == 3

    for _ in tasks:
        gate.release()
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)

    assert admitted == ["priority-draft", "review-other-board", "draft-other-board"]


async def test_equal_ranks_keep_fifo_order():
    """Ties fall back to arrival order so nothing starves within a rank."""
    gate = PriorityGate(1)
    await gate.acquire()
    admitted: list[int] = []

    async def waiter(n: int) -> None:
        await gate.acquire((1, 5))
        admitted.append(n)

    tasks = [asyncio.create_task(waiter(n)) for n in range(4)]
    await asyncio.sleep(0)
    for _ in tasks:
        gate.release()
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)

    assert admitted == [0, 1, 2, 3]


async def test_no_barging_past_queued_waiters():
    """A newcomer must not take a freed slot ahead of an already-queued
    waiter of equal rank, even though the fast path looks available."""
    gate = PriorityGate(1)
    await gate.acquire()
    admitted: list[str] = []

    async def waiter(name: str) -> None:
        await gate.acquire((1, 5))
        admitted.append(name)

    first = asyncio.create_task(waiter("queued-first"))
    await asyncio.sleep(0)
    gate.release()  # slot is free, but "queued-first" owns it
    late = asyncio.create_task(waiter("arrived-later"))
    await asyncio.sleep(0)
    gate.release()
    await asyncio.gather(first, late)

    assert admitted == ["queued-first", "arrived-later"]


async def test_cancelled_waiter_does_not_consume_a_slot():
    """Cancelling a queued waiter must leave the cap intact for the rest."""
    gate = PriorityGate(1)
    await gate.acquire()

    async def waiter() -> None:
        await gate.acquire((1, 5))

    doomed = asyncio.create_task(waiter())
    survivor = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert gate.waiting == 2

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert gate.waiting == 1

    gate.release()
    await asyncio.wait_for(survivor, timeout=1)


async def test_slot_granted_then_cancelled_is_handed_on():
    """A waiter cancelled in the same tick it was granted a slot must
    pass the slot along instead of leaking it out of the cap."""
    gate = PriorityGate(1)
    await gate.acquire()

    async def waiter() -> None:
        await gate.acquire((1, 5))

    granted = asyncio.create_task(waiter())
    behind = asyncio.create_task(waiter())
    await asyncio.sleep(0)

    gate.release()  # resolves `granted`'s future...
    granted.cancel()  # ...but it never gets to run
    with pytest.raises(asyncio.CancelledError):
        await granted

    # The slot must reach `behind` rather than vanishing.
    await asyncio.wait_for(behind, timeout=1)


async def test_slot_context_manager_releases_on_error():
    gate = PriorityGate(1)
    with pytest.raises(RuntimeError):
        async with gate.slot((0, 0)):
            assert gate.locked()
            raise RuntimeError("boom")
    assert not gate.locked()


async def test_default_rank_sorts_behind_flagged_work():
    """An unranked caller must not outrank a flagged ticket."""
    assert DEFAULT_RANK > (0, 99)


def test_rejects_negative_reserved():
    """A negative reserved count is a programming error — reject it."""
    with pytest.raises(ValueError):
        PriorityGate(2, reserved=-1)


async def test_reserved_clamped_below_capacity():
    """reserved >= value would starve every unprivileged caller — clamp it
    to value - 1 so at least one slot stays generally available."""
    gate = PriorityGate(1, reserved=5)
    # Clamped to 0, so an ordinary (unprivileged) caller can still acquire.
    await asyncio.wait_for(gate.acquire((1, 5)), timeout=1)
    assert gate.locked()


async def test_reserved_slot_held_back_from_unprivileged():
    """Unprivileged work is capped at value - reserved; the reserved slot
    stays free until a privileged caller claims it — even when an
    unprivileged caller is queued and eager for a slot."""
    gate = PriorityGate(2, reserved=1)
    # First unprivileged caller takes the single unreserved slot.
    await gate.acquire((1, 5))
    assert not gate.locked()  # the reserved slot is still free...

    # ...but a second unprivileged caller must NOT take it.
    blocked = asyncio.create_task(gate.acquire((1, 5)))
    await asyncio.sleep(0)
    assert gate.waiting == 1

    # A privileged caller takes the reserved slot immediately, stepping
    # past the ineligible unprivileged waiter.
    await asyncio.wait_for(gate.acquire((0, 0), reserved_ok=True), timeout=1)
    assert gate.locked()

    # Freeing just the reserved slot does not release the unprivileged
    # waiter — it needs an *unreserved* slot (value > reserved).
    gate.release()
    await asyncio.sleep(0)
    assert gate.waiting == 1
    gate.release()
    await asyncio.wait_for(blocked, timeout=1)


async def test_release_prefers_eligible_privileged_over_ineligible_head():
    """On release the best-ranked *eligible* waiter wins: a better-ranked
    unprivileged waiter the reservation still holds back is skipped in
    favour of a privileged waiter that can use the reserved slot."""
    gate = PriorityGate(2, reserved=1)
    # Both slots must be occupied, but an unprivileged caller may only hold
    # value - reserved = 1 slot — fill the reserved slot too as privileged.
    await gate.acquire((0, 0), reserved_ok=True)
    await gate.acquire((0, 0), reserved_ok=True)
    assert gate.locked()

    admitted: list[str] = []

    async def unpriv() -> None:
        async with gate.slot((0, 0)):  # best rank — sits at the heap head
            admitted.append("unpriv")

    async def priv() -> None:
        async with gate.slot((1, 9), reserved_ok=True):  # worse rank
            admitted.append("priv")

    t_unpriv = asyncio.create_task(unpriv())
    t_priv = asyncio.create_task(priv())
    await asyncio.sleep(0)
    assert gate.waiting == 2

    # One slot frees → value == reserved, so the head (unprivileged) is
    # still ineligible; the privileged waiter takes the freed slot, then
    # releases it on exit of its context manager.
    gate.release()
    await asyncio.sleep(0)
    assert admitted == ["priv"]
    assert gate.waiting == 1

    # A second free slot lifts value past reserved, admitting the
    # unprivileged waiter that was held back.
    gate.release()
    await asyncio.gather(t_unpriv, t_priv)
    assert admitted == ["priv", "unpriv"]


async def test_reserved_default_is_no_reservation():
    """With the default reserved=0 the gate behaves exactly as before —
    every slot is available to unprivileged work."""
    gate = PriorityGate(2)
    await gate.acquire((1, 5))
    await gate.acquire((1, 5))
    assert gate.locked()

import asyncio

import pytest

from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker, process_ticket


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


async def test_stub_pauses_chain(ctx, service, monkeypatch):
    """A stage raising NotImplementedError pauses the chain, leaving the
    ticket in place (not FAILED)."""

    class Stub(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise NotImplementedError("not built")

    monkeypatch.setitem(registry.STAGES, "refine", Stub())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT


async def test_noop_outcome_leaves_ticket(ctx, service, monkeypatch):
    """Same-state Outcome (e.g. merge: PR still open) = no transition,
    no history spam — the poll re-runs it later."""

    class NoOp(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.DRAFT, "waiting")

    monkeypatch.setitem(registry.STAGES, "refine", NoOp())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT
    assert [e.state for e in service.history(t.id)] == [State.DRAFT]


async def test_working_stages_chain_to_real_tail(ctx, service, monkeypatch):
    """Fakes drive draft->ready->deliverable; the real deliver stage
    then BLOCKs (no forge configured) — exercises real chaining."""

    class FakeRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.READY, "refined")

    class FakeImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            return Outcome(State.DELIVERABLE, "implemented")

    monkeypatch.setitem(registry.STAGES, "refine", FakeRefine())
    monkeypatch.setitem(registry.STAGES, "implement", FakeImplement())

    t = service.create("x")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.BLOCKED  # real deliver: no forge
    states = [e.state for e in service.history(t.id)]
    assert State.READY in states and State.DELIVERABLE in states


async def test_failing_stage_marks_failed(ctx, service, monkeypatch):
    class Boom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise RuntimeError("boom")

    monkeypatch.setitem(registry.STAGES, "refine", Boom())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    assert reloaded.state is State.ERRORED
    assert "boom" in service.history(t.id)[-1].note


async def test_untraced_noop_stage_emits_no_trace(ctx, service, monkeypatch):
    """merge/deliver-style untraced stages (traced=False) returning a
    no-op must NOT open a Langfuse 'ticket' trace — the merge poll
    otherwise spams an empty trace per cycle."""
    import contextlib

    from robotsix_mill.runtime import tracing as tr

    calls = {"root": 0, "stage": 0}

    @contextlib.contextmanager
    def fake_root(_tid):
        calls["root"] += 1
        yield

    @contextlib.contextmanager
    def fake_stage(_n):
        calls["stage"] += 1
        yield

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(tr, "trace_stage", fake_stage)

    class TracedRefine(Stage):
        name = "refine"
        input_state = State.DRAFT
        traced = True

        def run(self, _t, _c):
            return Outcome(State.AWAITING_APPROVAL, "refined")

    class UntracedNoop(Stage):
        name = "refine"
        input_state = State.DRAFT
        traced = False

        def run(self, _t, _c):
            return Outcome(State.DRAFT, "noop")  # same state = no-op

    monkeypatch.setitem(registry.STAGES, "refine", TracedRefine())
    await process_ticket(service.create("a").id, ctx)
    assert calls["root"] >= 1 and calls["stage"] >= 1  # traced stage traced

    calls["root"] = calls["stage"] = 0
    monkeypatch.setitem(registry.STAGES, "refine", UntracedNoop())
    await process_ticket(service.create("b").id, ctx)
    assert calls == {"root": 0, "stage": 0}  # untraced no-op: silent


async def test_done_is_not_terminal_retrospect_runs(ctx, service, monkeypatch):
    """Regression: DONE must NOT be terminal — process_ticket must run
    the retrospect stage (done -> reviewed). Pre-fix it bailed at done."""

    class FakeRetrospect(Stage):
        name = "retrospect"
        input_state = State.DONE

        def run(self, _t, _c):
            return Outcome(State.CLOSED, "retrospected")

    monkeypatch.setitem(registry.STAGES, "retrospect", FakeRetrospect())
    t = service.create("x")
    for st in (State.READY, State.DELIVERABLE, State.IN_REVIEW, State.DONE):
        service.transition(t.id, st)
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.CLOSED


# --- no-progress safety net (interrupted/churning model stage) ----------

def test_no_progress_guard_blocks_traced_stage(ctx, service):
    """A ticket that keeps re-entering a model-driven (traced) stage
    without ever advancing — runs killed before any checkpoint — must
    escalate to BLOCKED instead of being re-billed forever."""
    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)  # implement stage (traced)
    cap = ctx.settings.max_stuck_cycles
    for _ in range(cap - 1):
        w._check_progress(t.id, State.READY, State.READY)
        assert service.get(t.id).state is State.READY  # tolerated so far
    w._check_progress(t.id, State.READY, State.READY)  # cap reached
    blocked = service.get(t.id)
    assert blocked.state is State.BLOCKED
    assert "no progress" in service.history(t.id)[-1].note


def test_no_progress_guard_exempts_poll_stage(ctx, service):
    """in_review (merge, traced=False) legitimately waits on an open PR
    across many poll cycles — it must NEVER be auto-blocked."""
    w = Worker(ctx)
    t = service.create("x")
    for st in (State.READY, State.DELIVERABLE, State.IN_REVIEW):
        service.transition(t.id, st)
    for _ in range(ctx.settings.max_stuck_cycles + 3):
        w._check_progress(t.id, State.IN_REVIEW, State.IN_REVIEW)
    assert service.get(t.id).state is State.IN_REVIEW


def test_no_progress_guard_exempts_dependency_gated_ticket(ctx, service):
    """A ticket with unmet ``depends_on`` legitimately doesn't advance
    — implement.py returns Outcome(READY) until the dep is merged. The
    watchdog must NOT count those cycles as 'stuck', otherwise any
    dependent ticket gets BLOCKED within max_stuck_cycles poll ticks
    of approval, regardless of how the dep is actually doing."""
    w = Worker(ctx)
    parent = service.create("parent dep")
    dependent = service.create("waits on parent")
    service.set_depends_on(dependent.id, [parent.id])
    service.transition(dependent.id, State.READY)
    # Hammer the dependent ticket past the cap: it must stay READY
    # (not BLOCKED) because the parent is non-terminal.
    for _ in range(ctx.settings.max_stuck_cycles + 3):
        w._check_progress(dependent.id, State.READY, State.READY)
    assert service.get(dependent.id).state is State.READY
    assert dependent.id not in w._stuck

    # Once the parent reaches a terminal state, the dependent is no
    # longer "waiting" — the watchdog kicks in normally if the
    # ticket still doesn't advance (e.g. coordinator never starts).
    service.transition(parent.id, State.DONE)
    service.transition(parent.id, State.CLOSED)
    for _ in range(ctx.settings.max_stuck_cycles):
        w._check_progress(dependent.id, State.READY, State.READY)
    assert service.get(dependent.id).state is State.BLOCKED


def test_no_progress_counter_resets_on_advance(ctx, service):
    """Any real state change clears the strike count — a later stall
    starts counting from zero, not from stale strikes."""
    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)  # 1 strike
    w._check_progress(t.id, State.READY, State.DELIVERABLE)  # progressed
    assert t.id not in w._stuck
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IN_REVIEW)
    service.transition(t.id, State.DONE)  # retrospect (traced) stage
    for _ in range(ctx.settings.max_stuck_cycles - 1):
        w._check_progress(t.id, State.DONE, State.DONE)
    assert service.get(t.id).state is State.DONE  # not blocked yet


# --- bounded-concurrency pool ------------------------------------------

def test_enqueue_dedupes(ctx):
    w = Worker(ctx)
    w.enqueue("a"); w.enqueue("a"); w.enqueue("b")
    assert w.queue.qsize() == 2
    assert w._pending == {"a", "b"}


async def test_start_creates_pool_of_max_concurrency(ctx):
    ctx.settings.max_concurrency = 3
    w = Worker(ctx)
    w.start()
    try:
        assert len(w._tasks) == 3
    finally:
        await w.stop()
    assert w._tasks == []


async def test_pool_runs_tickets_in_parallel(ctx, service, monkeypatch):
    """Distinct tickets are processed concurrently (not serialized),
    and a re-enqueue of an in-flight ticket is deduped."""
    import threading
    import time

    lock = threading.Lock()
    live = {"now": 0, "max": 0, "done": 0}

    class SlowRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.15)  # hold so peers overlap if truly parallel
            with lock:
                live["now"] -= 1
                live["done"] += 1
            return Outcome(State.AWAITING_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", SlowRefine())
    ctx.settings.max_concurrency = 4
    w = Worker(ctx)
    w.start()
    try:
        ids = [service.create(f"t{i}").id for i in range(4)]
        for tid in ids:
            w.enqueue(tid)
            w.enqueue(tid)  # dup while pending -> must be ignored
        await asyncio.wait_for(w.queue.join(), timeout=10)
    finally:
        await w.stop()

    assert live["done"] == 4              # each processed exactly once
    assert live["max"] >= 2               # genuinely overlapped
    assert all(
        service.get(i).state is State.AWAITING_APPROVAL for i in ids
    )


async def test_reconcile_sweep_enqueues_out_of_band_drafts(
    ctx, service, monkeypatch
):
    """Regression: drafts created directly via service.create() (audit
    runner / retrospect / report_issue) — NOT via the API enqueue path —
    must still get picked up by the periodic reconcile sweep, not sit in
    DRAFT until a process restart."""
    t = service.create("audit-spawned thing", "body", source="audit")
    assert t.state is State.DRAFT
    w = Worker(ctx)

    # Let the loop body run exactly once, then break out.
    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.asyncio.sleep", fake_sleep
    )
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id in w._pending  # swept in despite never being enqueued

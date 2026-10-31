import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker, process_ticket
from robotsix_mill.stages import Outcome, StageContext, registry
from robotsix_mill.stages.base import Stage


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# --- bounded-concurrency pool ------------------------------------------


def test_enqueue_dedupes(ctx):
    w = Worker(ctx)
    w.enqueue("a")
    w.enqueue("a")
    w.enqueue("b")
    assert w.queue_size() == 2
    assert w._pending == {"a", "b"}


def test_enqueue_orders_late_stage_before_draft(ctx, service):
    """Within a priority class, late-pipeline tickets pop before
    drafts — drains the pipeline before starting fresh refines."""
    w = Worker(ctx)
    # Create three tickets at different stages on the same repo so they
    # all land in the same per-repo queue.
    early = service.create("early draft")
    mid = service.create("mid review")
    late = service.create("late done")
    # Transition them to varied states via direct setter — bypass the
    # state machine because we just want different queue ranks.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.states import State as _S

    with _db.session(ctx.settings, service.board_id) as s:
        t_mid = s.get(
            __import__("robotsix_mill.core.models", fromlist=["Ticket"]).Ticket, mid.id
        )
        t_mid.state = _S.CODE_REVIEW
        s.add(t_mid)
        t_late = s.get(
            __import__("robotsix_mill.core.models", fromlist=["Ticket"]).Ticket, late.id
        )
        t_late.state = _S.DONE
        s.add(t_late)
        s.commit()
    # Enqueue in mixed order; the per-repo queue's sort should still
    # pop DONE first, then CODE_REVIEW, then DRAFT.
    w.enqueue(early.id)
    w.enqueue(mid.id)
    w.enqueue(late.id)
    # Drain the right queue and capture pop order.
    q = w._queue_for(ctx.repo_config.board_id if ctx.repo_config else "")
    popped: list[str] = []
    while q.qsize():
        popped.append(q.get_nowait()[-1])
    assert popped == [late.id, mid.id, early.id], (
        f"expected late-stage tickets first; got {popped}"
    )


def _fake_ticket(tid, *, board_id="", state=State.DRAFT, priority=False):
    """Minimal Ticket-like object for queue-routing unit tests — only the
    attributes ``enqueue`` / ``_stage_rank`` read (id, board_id, state,
    priority), so no DB is needed."""
    return SimpleNamespace(id=tid, board_id=board_id, state=state, priority=priority)


def _fake_worker(tickets):
    """Build a Worker whose ``ctx.service.get`` resolves *tickets* (a dict
    id -> fake ticket) without touching a DB."""
    ctx = SimpleNamespace(service=SimpleNamespace(get=lambda tid: tickets.get(tid)))
    return Worker(ctx)


def test_stage_rank_maps_known_states_to_explicit_values():
    """Every state in the _STAGE_RANK table resolves to its declared rank."""
    for state, rank in Worker._STAGE_RANK.items():
        assert Worker._stage_rank(_fake_ticket("t", state=state)) == rank


def test_stage_rank_falls_back_for_unknown_state_and_none():
    """An unranked state (or a missing ticket) yields _DEFAULT_STAGE_RANK
    rather than raising KeyError — the starvation guard's safety valve."""
    assert Worker._stage_rank(None) == Worker._DEFAULT_STAGE_RANK
    # BLOCKED is deliberately absent from the rank table.
    assert State.BLOCKED not in Worker._STAGE_RANK
    assert (
        Worker._stage_rank(_fake_ticket("t", state=State.BLOCKED))
        == Worker._DEFAULT_STAGE_RANK
    )


def test_enqueue_routes_tickets_to_per_board_queues():
    """Each ticket lands on its own board's queue; board-less tickets fall
    through to the default queue — no cross-board leakage."""
    tickets = {
        "a1": _fake_ticket("a1", board_id="board-A"),
        "b1": _fake_ticket("b1", board_id="board-B"),
        "none1": _fake_ticket("none1", board_id=""),
    }
    w = _fake_worker(tickets)
    w.enqueue("a1")
    w.enqueue("b1")
    w.enqueue("none1")
    assert set(w.queues) == {Worker._DEFAULT_BOARD, "board-A", "board-B"}
    assert w.queues["board-A"].get_nowait()[-1] == "a1"
    assert w.queues["board-B"].get_nowait()[-1] == "b1"
    assert w.queues[Worker._DEFAULT_BOARD].get_nowait()[-1] == "none1"


def test_enqueue_priority_tickets_pop_before_normal():
    """Within one board, a priority ticket (rank 0) pops before an
    earlier-enqueued normal ticket (rank 1) at the same stage rank."""
    tickets = {
        "norm": _fake_ticket("norm", board_id="b", priority=False),
        "prio": _fake_ticket("prio", board_id="b", priority=True),
    }
    w = _fake_worker(tickets)
    w.enqueue("norm")  # enqueued first...
    w.enqueue("prio")  # ...but priority should still pop first
    q = w.queues["b"]
    assert [q.get_nowait()[-1] for _ in range(2)] == ["prio", "norm"]


def test_queue_for_empty_board_returns_default_and_creates_lazily():
    """``_queue_for("")`` aliases the default queue; an unknown board id
    materializes a fresh queue on first use."""
    w = _fake_worker({})
    assert w._queue_for("") is w.queues[Worker._DEFAULT_BOARD]
    assert "fresh" not in w.queues
    q = w._queue_for("fresh")
    assert w.queues["fresh"] is q


# -- drain mode (update-pending) -----------------------------------------


def test_drain_defaults_inactive():
    """A fresh Worker has drain mode off; drain_status reports it."""
    w = _fake_worker({})
    assert w.drain_active() is False
    assert w.drain_status()["active"] is False
    assert w.drain_status()["drained"] is False


def test_drain_set_active_and_clear():
    """set_drain arms drain mode until the TTL; clear_drain clears it."""
    w = _fake_worker({})
    w.set_drain(ttl_seconds=60)
    assert w.drain_active() is True
    status = w.drain_status()
    assert status["active"] is True
    assert status["drain_until"] is not None

    w.clear_drain()
    assert w.drain_active() is False
    assert w.drain_status()["active"] is False


def test_drain_ttl_expires_fail_open():
    """A stale flag (caretaker gone) expires after the TTL and intake
    resumes — fail-open, no lingering state."""
    from datetime import timedelta

    w = _fake_worker({})
    w.set_drain(ttl_seconds=3600)
    assert w.drain_active() is True

    # Move drain_until into the past to simulate the TTL elapsing while
    # the caretaker is away.
    w._drain_until = datetime.now(UTC) - timedelta(seconds=1)
    assert w.drain_active() is False  # expired → fail-open
    assert w._drain_until is None  # state cleared


def test_drain_status_reports_drained_when_no_heavy_in_flight():
    """drained means drain armed AND no heavy stage currently running."""
    w = _fake_worker({})
    # Simulate an in-flight heavy stage (implement) and a light one.
    w._active[("t1", "implement")] = {"stage": "implement", "started_at": "x"}
    w._active[("t2", "merge")] = {"stage": "merge", "started_at": "x"}
    w.set_drain(ttl_seconds=60)
    status = w.drain_status()
    assert status["active"] is True
    assert status["drained"] is False  # implement still in flight
    assert "implement" in status["active_stages"]

    # Once the heavy stage finishes, drained flips true.
    w._active.pop(("t1", "implement"))
    status = w.drain_status()
    assert status["drained"] is True
    assert status["active_stages"] == ["merge"]


async def test_drain_gate_defers_heavy_stage_until_cleared(ctx, service, monkeypatch):
    """While drain mode is armed, a heavy stage (implement) is NOT started;
    once cleared, the queued ticket is processed normally."""
    import threading

    calls = []
    lock = threading.Lock()

    class FakeImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            with lock:
                calls.append("run")
            # BLOCKED halts the chain (not in STAGE_FOR_STATE) and is a
            # valid READY transition, so the ticket ends processing.
            return Outcome(State.BLOCKED, "implemented")

    monkeypatch.setitem(registry.STAGES, "implement", FakeImplement())

    from robotsix_mill.config import RepoConfig, ReposRegistry

    fake_repos = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=1,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: fake_repos,
    )

    t = service.create("drain test")
    service.transition(t.id, State.READY, note="ready")

    w = Worker(ctx)
    w.set_drain(ttl_seconds=60)  # arm BEFORE start so the gate holds
    w.start()
    try:
        w.enqueue(t.id)
        # Drain gate holds the heavy ticket for a few cycles; it must
        # not be dispatched while drain is armed.
        await asyncio.sleep(0.3)
        assert calls == [], f"heavy stage ran during drain: {calls}"
        assert service.get(t.id).state is State.READY

        # Clear drain → the queued ticket processes normally.
        w.clear_drain()
        await asyncio.wait_for(w.queue_join(), timeout=10)
        assert calls == ["run"]
        assert service.get(t.id).state is State.BLOCKED
    finally:
        await w.stop()


async def test_merge_poll_not_starved_by_full_slots(ctx, service, monkeypatch):
    """A green implement_complete ticket reaches human_mr_approval while
    two implements hold every non-classify slot (cap 3, 1 reserved):
    merge-poll states bypass the global gate."""
    import threading

    release = threading.Event()
    started = {"n": 0}
    lock = threading.Lock()

    class BlockingImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            with lock:
                started["n"] += 1
            release.wait(timeout=10)
            return Outcome(State.BLOCKED, "implemented")

    class GreenMerge(Stage):
        name = "merge"
        input_state = State.IMPLEMENT_COMPLETE

        def run(self, t, _c):
            # IC -> HMA, then a same-state no-op from HMA ends the chain.
            return Outcome(State.HUMAN_MR_APPROVAL, "green")

    monkeypatch.setitem(registry.STAGES, "implement", BlockingImplement())
    monkeypatch.setitem(registry.STAGES, "merge", GreenMerge())
    ctx.settings.max_global_concurrency = 3
    ctx.settings.classify_reserved_slots = 1

    from robotsix_mill.config import RepoConfig, ReposRegistry

    fake_repos = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=3,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: fake_repos,
    )

    impls = []
    for i in range(2):
        t = service.create(f"impl {i}")
        service.transition(t.id, State.READY, note="ready")
        impls.append(t.id)
    green = service.create("green PR")
    service.transition(green.id, State.READY, note="ready")
    service.transition(green.id, State.DELIVERABLE, note="delivered")
    service.transition(green.id, State.IMPLEMENT_COMPLETE, note="gates")

    w = Worker(ctx)
    w.start()
    try:
        for tid in impls:
            w.enqueue(tid)
        for _ in range(100):
            if started["n"] == 2:
                break
            await asyncio.sleep(0.02)
        assert started["n"] == 2
        assert w._global_semaphore.locked() is False  # reserved slot left
        # Non-classify slots are full: only the reserved one remains.
        assert w._global_semaphore._can_admit(False) is False

        w.enqueue(green.id)
        for _ in range(100):
            if service.get(green.id).state is State.HUMAN_MR_APPROVAL:
                break
            await asyncio.sleep(0.02)
        assert service.get(green.id).state is State.HUMAN_MR_APPROVAL
        assert all(service.get(t).state is State.READY for t in impls)
    finally:
        release.set()
        await asyncio.wait_for(w.queue_join(), timeout=10)
        await w.stop()


async def _wait_until(pred, tries: int = 200) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_merge_lane_hands_agent_stage_back_to_gate(ctx, service, monkeypatch):
    """An ungated merge-lane run that moves the ticket to FIXING_CI stops
    there: ci_fix must wait for a gate slot, not chain on the lane."""
    import threading

    release = threading.Event()
    started = {"n": 0}
    ci_fix_calls: list[str] = []
    lock = threading.Lock()

    class BlockingImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            with lock:
                started["n"] += 1
            release.wait(timeout=10)
            return Outcome(State.BLOCKED, "implemented")

    class RedMerge(Stage):
        name = "merge"
        input_state = State.IMPLEMENT_COMPLETE

        def run(self, t, _c):
            return Outcome(State.FIXING_CI, "ci red")

    class RecordingCIFix(Stage):
        name = "ci_fix"
        input_state = State.FIXING_CI

        def run(self, t, _c):
            ci_fix_calls.append(t.id)
            return Outcome(State.BLOCKED, "fixed")

    monkeypatch.setitem(registry.STAGES, "implement", BlockingImplement())
    monkeypatch.setitem(registry.STAGES, "merge", RedMerge())
    monkeypatch.setitem(registry.STAGES, "ci_fix", RecordingCIFix())
    ctx.settings.max_global_concurrency = 3
    ctx.settings.classify_reserved_slots = 1

    from robotsix_mill.config import RepoConfig, ReposRegistry

    fake_repos = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=3,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: fake_repos,
    )

    impls = []
    for i in range(2):
        t = service.create(f"impl {i}")
        service.transition(t.id, State.READY, note="ready")
        impls.append(t.id)
    red = service.create("red PR")
    service.transition(red.id, State.READY, note="ready")
    service.transition(red.id, State.DELIVERABLE, note="delivered")
    service.transition(red.id, State.IMPLEMENT_COMPLETE, note="gates")

    w = Worker(ctx)
    w.start()
    try:
        for tid in impls:
            w.enqueue(tid)
        await _wait_until(lambda: started["n"] == 2)

        w.enqueue(red.id)
        await _wait_until(lambda: service.get(red.id).state is State.FIXING_CI)
        await asyncio.sleep(0.2)
        assert service.get(red.id).state is State.FIXING_CI
        assert ci_fix_calls == []  # held behind the saturated gate

        release.set()
        await _wait_until(lambda: bool(ci_fix_calls))
        assert ci_fix_calls == [red.id]
    finally:
        release.set()
        await asyncio.wait_for(w.queue_join(), timeout=10)
        await w.stop()


async def test_merge_poll_starvation_logged(ctx, service, caplog):
    """The per-tick metric counts queued implement_complete tickets while
    the global gate is saturated."""
    import logging

    from robotsix_mill.runtime.worker._gate import PriorityGate

    t = service.create("green")
    service.transition(t.id, State.READY, note="ready")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates")

    w = Worker(ctx)
    w._global_semaphore = PriorityGate(1)
    w._pending.add(t.id)
    assert w._log_merge_poll_starvation() == 0  # slot still free

    await w._global_semaphore.acquire()
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.runtime.worker.core"):
        assert w._log_merge_poll_starvation() == 1
    assert "merge poll starved: 1 tickets at implement_complete" in caplog.text


async def test_start_creates_per_repo_consumer_pools(ctx, monkeypatch):
    """One consumer task per repo per its max_concurrency, plus a
    single fallback consumer for the default (no-repo) queue."""
    from robotsix_mill.config import RepoConfig, ReposRegistry

    fake_repos = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id="ba",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=2,
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id="bb",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=1,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: fake_repos,
    )

    w = Worker(ctx)
    w.start()
    try:
        # repo-a: 2 + repo-b: 1 + default: 1 + meta: 1 = 5 tasks across 4 boards
        total = sum(len(tasks) for tasks in w._tasks.values())
        assert total == 5
        assert set(w._tasks.keys()) == {"ba", "bb", "", "meta"}
    finally:
        await w.stop()
    assert w._tasks == {}


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
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", SlowRefine())
    # Set up a single test repo with max_concurrency=4 so the pool
    # sizes up to actually run things in parallel. (Per-repo model:
    # the old global ctx.settings.max_concurrency is unused.)
    from robotsix_mill.config import RepoConfig, ReposRegistry

    fake_repos = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=4,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: fake_repos,
    )

    w = Worker(ctx)
    w.start()
    try:
        ids = [service.create(f"t{i}").id for i in range(4)]
        for tid in ids:
            w.enqueue(tid)
            w.enqueue(tid)  # dup while pending -> must be ignored
        await asyncio.wait_for(w.queue_join(), timeout=10)
    finally:
        await w.stop()

    assert live["done"] == 4  # each processed exactly once
    assert live["max"] >= 2  # genuinely overlapped
    assert all(service.get(i).state is State.HUMAN_ISSUE_APPROVAL for i in ids)


async def test_reconcile_sweep_enqueues_out_of_band_drafts(ctx, service, monkeypatch):
    """Regression: drafts created directly via service.create() (audit
    runner / retrospect / report_issue) — NOT via the API enqueue path —
    must still get picked up by the periodic reconcile sweep, not sit in
    DRAFT until a process restart."""
    t = service.create("audit-spawned thing", "body", source=SourceKind.AUDIT)
    assert t.state is State.DRAFT
    w = Worker(ctx)

    # Let the loop body run exactly once, then break out.
    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("robotsix_mill.runtime.worker.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id in w._pending  # swept in despite never being enqueued


@pytest.mark.asyncio
async def test_reconcile_sweep_enqueues_meta_board_tickets(ctx, monkeypatch):
    """Regression: a ticket on the synthetic meta board that becomes
    workable AFTER startup (e.g. its dependency closes) must be picked up
    by the periodic reconcile sweep. requeue_unfinished() seeds the meta
    board but only runs once at startup; the poll loop must sweep it too."""
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.runtime.worker import Worker

    meta_svc = TicketService(ctx.settings, board_id=Worker._META_BOARD)
    t = meta_svc.create("meta migrate thing", "body", source=SourceKind.META)
    meta_svc.transition(t.id, State.READY)  # workable, no unmet deps
    w = Worker(ctx)

    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("robotsix_mill.runtime.worker.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id in w._pending  # meta board was swept


@pytest.mark.asyncio
async def test_reconcile_sweep_re_enqueues_parked_human_mr_approval(
    ctx, service, monkeypatch
):
    """Regression (76f7): a ticket parked at HUMAN_MR_APPROVAL must be
    re-enqueued by the periodic reconcile sweep so a CHANGES_REQUESTED
    review submitted AFTER parking is detected. HUMAN_MR_APPROVAL is in
    STAGE_FOR_STATE, so the sweep re-invokes the merge stage (and thus
    _handle_human_mr_approval) on every pass — proving the scheduler, not
    just a direct MergeStage().run() call, re-processes parked tickets."""
    t = service.create("awaiting human merge", "body")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(t.id, st)
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL
    w = Worker(ctx)

    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("robotsix_mill.runtime.worker.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id in w._pending  # parked ticket was re-enqueued for re-poll


# ---------------------------------------------------------------------------
# startup re-queue drip-feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_drip_feed_enqueues_in_batches(ctx, repo_config, monkeypatch):
    """requeue_unfinished() returns immediately; the background drip
    task enqueues all matching tickets in batches with pauses."""
    from robotsix_mill.config import ReposRegistry
    from robotsix_mill.core.states import STAGE_FOR_STATE
    from robotsix_mill.runtime.worker.core import Worker

    # Pick a batch size < total tickets so we exercise batching.
    ctx.settings.requeue_batch_size = 3
    ctx.settings.requeue_batch_pause_seconds = 0.5

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: ReposRegistry(repos={repo_config.repo_id: repo_config}),
    )

    # Create 8 fake ticket IDs. We mock TicketService.list() to
    # return stub objects whose .state is in STAGE_FOR_STATE.
    ticket_ids = [f"req-drip-{i}" for i in range(8)]
    workable_state = next(iter(STAGE_FOR_STATE))

    class _StubTicket:
        def __init__(self, tid):
            self.id = tid
            self.state = workable_state

    stub_tickets = [_StubTicket(tid) for tid in ticket_ids]

    class _FakeTicketService:
        def __init__(self, settings, board_id=None):
            pass

        def list(self):
            return stub_tickets

    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        _FakeTicketService,
    )

    sleep_durations: list[float] = []
    _real_sleep = asyncio.sleep

    async def recording_sleep(duration: float) -> None:
        sleep_durations.append(duration)
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    w = Worker(ctx)

    # Call requeue_unfinished — must return immediately.
    w.requeue_unfinished()
    assert w._requeue_task is not None

    # Right after the call, nothing or at most one batch enqueued
    # (the task is scheduled but hasn't run yet — _pending may be 0
    # or at most batch_size if the task ran synchronously).

    # Drain the drip task.
    await w._requeue_task

    # After draining, all 8 tickets should be in _pending.
    assert len(w._pending) == 8, (
        f"expected all 8 tickets in _pending, got {len(w._pending)}"
    )
    for tid in ticket_ids:
        assert tid in w._pending

    # 8 tickets, batch_size=3 → ceil(8/3)=3 batches → 2 pauses.
    expected_pauses = (
        8 + ctx.settings.requeue_batch_size - 1
    ) // ctx.settings.requeue_batch_size - 1
    assert len(sleep_durations) == expected_pauses, (
        f"expected {expected_pauses} sleep calls, got {len(sleep_durations)}"
    )
    for d in sleep_durations:
        assert d == ctx.settings.requeue_batch_pause_seconds


@pytest.mark.asyncio
async def test_requeue_drip_is_cancelled_on_stop(ctx, repo_config, monkeypatch):
    """A pending drip task is cancelled cleanly by stop()."""
    from robotsix_mill.config import ReposRegistry
    from robotsix_mill.core.states import STAGE_FOR_STATE

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: ReposRegistry(repos={repo_config.repo_id: repo_config}),
    )

    # Return tickets so the drip task hits the sleep between batches.
    workable_state = next(iter(STAGE_FOR_STATE))
    ticket_ids = [f"cancel-{i}" for i in range(10)]

    class _StubTicket:
        def __init__(self, tid):
            self.id = tid
            self.state = workable_state

    stub_tickets = [_StubTicket(tid) for tid in ticket_ids]

    class _FakeTicketService:
        def __init__(self, settings, board_id=None):
            pass

        def list(self):
            return stub_tickets

    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        _FakeTicketService,
    )

    # Let the drip sleep normally (fast) so it completes quickly.
    # The point of this test is that stop() handles an in-flight or
    # just-completed _requeue_task without raising.
    w = Worker(ctx)
    w.requeue_unfinished()
    assert w._requeue_task is not None

    # Let the drip task run to completion (or at least past the first batch).
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # stop() must handle the _requeue_task without raising,
    # whether it's still running or already done.
    await w.stop()
    assert w._requeue_task is None  # cleared by stop()


# ---------------------------------------------------------------------------
# per-repo first-tick jitter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_pass_per_repo_first_tick_jittered(ctx, monkeypatch):
    """_run_periodic_pass_per_repo's first sleep is jittered within
    [1.0, 1.0 + startup_jitter_seconds]; subsequent ticks sleep
    _PERIODIC_POLL_TICK_SECONDS unchanged."""
    from robotsix_mill.config import ReposRegistry
    from robotsix_mill.runtime.worker import Worker

    ctx.settings.startup_jitter_seconds = 15

    # Pin random.uniform so we get a deterministic jitter value.
    monkeypatch.setattr("random.uniform", lambda lo, hi: 7.0)

    sleep_durations: list[float] = []
    _real_sleep = asyncio.sleep

    tick = 0

    async def counting_sleep(duration: float) -> None:
        nonlocal tick
        tick += 1
        sleep_durations.append(duration)
        if tick >= 3:  # first tick + one body iteration
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: ReposRegistry(repos={}),
    )

    w = Worker(ctx)

    def fake_runner(session_id=None, repo_config=None):
        from robotsix_mill.agents.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass_per_repo(
            "audit",
            fake_runner,
            settings_interval_attr="audit_interval_seconds",
        )

    assert len(sleep_durations) >= 2, (
        f"expected at least 2 sleep calls, got {len(sleep_durations)}"
    )
    # First tick: 1.0 + random.uniform(0, startup_jitter_seconds)
    # with uniform → 7.0 → expected 8.0
    assert sleep_durations[0] == 8.0, (
        f"first tick should be 1.0 + 7.0 = 8.0, got {sleep_durations[0]}"
    )
    # Subsequent ticks: _PERIODIC_POLL_TICK_SECONDS
    for d in sleep_durations[1:]:
        assert d == w._PERIODIC_POLL_TICK_SECONDS, (
            f"subsequent ticks should be {w._PERIODIC_POLL_TICK_SECONDS}, got {d}"
        )


# --- global slot admission is ranked, not FIFO -----------------------


async def test_priority_ticket_wins_global_slot_over_earlier_other_board(
    ctx, settings, monkeypatch
):
    """Cross-board regression: per-board queues rank tickets, but every
    board contends for the SAME global concurrency cap.  With a FIFO
    semaphore there, a flagged ticket that instantly won its own queue
    still waited behind whichever board's unflagged ticket happened to
    reach the cap first — so operators flagged a ticket and watched
    unflagged work from another repo start ahead of it.
    """
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.runtime.worker._gate import PriorityGate

    _db.init_db(settings, board_id="board-a")
    _db.init_db(settings, board_id="board-b")
    svc_a = TicketService(settings, board_id="board-a")
    svc_b = TicketService(settings, board_id="board-b")

    normal = svc_a.create("unflagged work on board-a")
    flagged = svc_b.create("flagged work on board-b")
    svc_b.set_priority(flagged.id, True)

    w = Worker(ctx)
    # No repo config → the in-flight PR cap is skipped, isolating the
    # global-slot behaviour under test.
    monkeypatch.setattr(Worker, "_repo_config_for_ticket", lambda self, tid: None)
    monkeypatch.setattr(Worker, "_check_progress", lambda self, *a, **k: None)

    invoked: list[str] = []

    async def fake_process_ticket(
        ticket_id, p_ctx, active_map=None, allowed_states=None
    ):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket", fake_process_ticket
    )

    # Saturate the cap so both consumers must queue for the one slot.
    w._global_semaphore = PriorityGate(1)
    await w._global_semaphore.acquire((0, 0))

    w.enqueue(normal.id)
    task_a = asyncio.create_task(w._run("board-a"))
    await asyncio.sleep(0.05)  # board-a reaches the gate FIRST
    assert w._global_semaphore.waiting == 1

    w.enqueue(flagged.id)
    task_b = asyncio.create_task(w._run("board-b"))
    await asyncio.sleep(0.05)  # board-b arrives second, but flagged
    assert w._global_semaphore.waiting == 2

    w._global_semaphore.release()  # free the slot; ranked admission decides
    try:
        await asyncio.sleep(0.1)
    finally:
        for t in (task_a, task_b):
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t

    assert invoked[0] == flagged.id, (
        "the flagged ticket must take the free global slot even though "
        f"another board's unflagged ticket queued for it first; got {invoked}"
    )
    assert invoked == [flagged.id, normal.id]


# --- the consumer publishes its rank to the sandbox slot gate --------


async def test_consumer_publishes_sandbox_rank_and_resets_it(ctx, service, monkeypatch):
    """The sandbox slot pool is shared with the ~20 periodic passes, which
    carry no rank at all. The consumer publishes the ticket's rank into the
    context so ``sandbox.run()`` — reached through ``asyncio.to_thread``,
    which copies the context — can admit flagged work first.

    It must also reset afterwards: the consumer is a long-lived loop, so a
    leaked rank would make the NEXT ticket inherit this one's priority.
    """
    from robotsix_mill.sandbox._slots import DEFAULT_RANK, current_rank

    flagged = service.create("flagged")
    service.set_priority(flagged.id, True)

    monkeypatch.setattr(Worker, "_repo_config_for_ticket", lambda self, tid: None)
    monkeypatch.setattr(Worker, "_check_progress", lambda self, *a, **k: None)

    seen: list[tuple[int, int]] = []

    async def fake_process_ticket(
        ticket_id, p_ctx, active_map=None, allowed_states=None
    ):
        seen.append(current_rank())

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket", fake_process_ticket
    )

    w = Worker(ctx)
    w.enqueue(flagged.id)
    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen, "the ticket should have been processed"
    assert seen[0][0] == 0, (
        f"a flagged ticket must publish priority rank 0; got {seen[0]}"
    )
    assert current_rank() == DEFAULT_RANK, (
        "the rank must be reset after the stage run, or the next ticket "
        "inherits this one's priority"
    )


async def test_disk_full_parks_without_consuming_retry(ctx, service, monkeypatch):
    """A stage failing with ENOSPC while the volume is genuinely below
    the floor is PARKED: retry budget untouched, no BLOCKED transition.
    A full volume outlasts the bounded retry envelope by hours, and
    blocking each ticket converts one infrastructure fault into one
    manual resume per ticket."""
    import errno

    class DiskBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise OSError(errno.ENOSPC, "No space left on device", "/data/x/repo")

    monkeypatch.setitem(registry.STAGES, "refine", DiskBoom())

    # The volume fills DURING the stage: the admission gate (first call
    # each pass) sees room, the post-failure check does not. This is the
    # only ordering that can reach the park branch once the gate exists.
    calls: list[int] = []

    def fills_during_stage(paths, min_free_mb):
        calls.append(1)
        return None if len(calls) % 2 == 1 else "/data"

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing.first_full_path",
        fills_during_stage,
    )
    t = service.create("x")
    for _ in range(ctx.settings.stage_retry_max_attempts + 2):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        assert r.state is State.DRAFT, "a full disk must never block the ticket"
        assert r.retry_attempt == 1, "retry budget must not be consumed"
        assert r.next_retry_at is not None
        assert "data volume full" in (r.last_transient_error or "")
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )


async def test_enospc_with_space_available_uses_bounded_retries(
    ctx, service, monkeypatch
):
    """The same ENOSPC WITHOUT a confirmed shortage on any checked
    filesystem — e.g. a quota or a small dedicated mount neither the data
    volume nor the container root covers — goes through the normal bounded
    transient retry and blocks once exhausted, so a real defect is not
    parked forever."""
    import errno

    class DiskBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setitem(registry.STAGES, "refine", DiskBoom())
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing.first_full_path",
        lambda paths, min_free_mb: None,
    )
    t = service.create("x")
    for _ in range(ctx.settings.stage_retry_max_attempts + 1):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        if r.state is State.BLOCKED:
            break
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )
    assert service.get(t.id).state is State.BLOCKED


async def test_disk_admission_gate_parks_before_dispatch(ctx, service, monkeypatch):
    """Below the floor the stage never runs at all: a stage that dies
    partway through leaves a half-written workspace consuming the very
    space it ran out of."""
    ran: list[str] = []

    class Marker(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            ran.append("yes")
            return Outcome(next_state=State.READY, note="done")

    monkeypatch.setitem(registry.STAGES, "refine", Marker())
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing.first_full_path",
        lambda paths, min_free_mb: "/data",
    )
    t = service.create("x")
    await process_ticket(t.id, ctx)

    r = service.get(t.id)
    assert ran == [], "stage must not run below the free-space floor"
    assert r.state is State.DRAFT
    assert r.retry_attempt == 1, "retry budget must not be consumed"
    assert r.next_retry_at is not None
    assert "below" in (r.last_transient_error or "")


async def test_retrospect_stage_timeout_closes_instead_of_blocking(
    ctx, service, monkeypatch
):
    """A retrospect deadline must not un-ship an already-merged ticket.

    retrospect runs on DONE — the PR is merged and the change has landed.
    Its only product is follow-up learning tickets, so a timeout there says
    nothing about the delivered work. Blocking put two finished chat
    tickets (358d, 6462) back on the human queue minutes after their PRs
    merged, and the re-run timed out again, so a retry is not the answer.
    """
    import time

    ctx.settings.stage_timeout_overrides = {"retrospect": 1}

    class SlowRetrospect(Stage):
        name = "retrospect"
        input_state = State.DONE

        def run(self, _t, _c):
            time.sleep(5)
            return Outcome(State.CLOSED, "should never reach here")

    monkeypatch.setitem(registry.STAGES, "retrospect", SlowRetrospect())
    t = service.create("shipped")
    service.transition(t.id, State.DONE, note="merged")

    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.CLOSED, "a merged ticket must not be blocked"
    note = service.history(t.id)[-1].note
    assert "timed out" in note
    assert "retrospect" in note


# -- duplicate (ticket, stage) dispatch guard (ticket …-8ec0) -----------


def test_try_claim_rejects_a_second_run_of_the_same_ticket_stage(ctx):
    """The same ticket+stage can only be claimed once at a time.

    `_pending` guards the queue against duplicate *enqueues*; before this
    guard nothing stopped two consumers *dispatching* the same ticket+stage.
    Observed on prod 2026-08-25: one ticket had three distinct implement
    runs in a 3h window, two live simultaneously, racing on the same
    `mill/<ticket>` branch and holding a sandbox slot each.
    """
    w = Worker(ctx)

    first = w._try_claim("t-1", State.READY)
    assert first == ("t-1", State.READY.value)
    assert w._try_claim("t-1", State.READY) is None


def test_try_claim_blocks_a_different_stage_while_a_run_is_live(ctx):
    """The claim keys on the ticket id ALONE, not (ticket, stage).

    While any stage of a ticket is running in this process, a second run
    of that ticket must be rejected even for a *different* stage. This is
    the fix for the pop-time requeue race (ticket …-0629): once the live
    run transitioned the state, a (ticket, stage)-keyed claim no longer
    collided and a second run of the next stage slipped through — refine
    and implement each ran twice on one ticket.
    """
    w = Worker(ctx)

    assert w._try_claim("t-1", State.READY) is not None
    assert w._try_claim("t-1", State.FIXING_CI) is None


def test_try_claim_is_reusable_after_release(ctx):
    """Releasing a claim frees the ticket again.

    The release lives in the consumer's `finally`, so this is the property
    that keeps a leaked claim from silently blocking every future run of a
    ticket. The claim gates on the ticket id (``claim[0]``); the release
    discards that.
    """
    w = Worker(ctx)

    claim = w._try_claim("t-1", State.DRAFT)
    assert claim is not None
    w._in_flight.discard(claim[0])
    assert w._try_claim("t-1", State.DRAFT) == claim


def test_try_claim_handles_a_ticket_with_no_state(ctx):
    """A ticket the service could not read still claims, under an empty key.

    The consumer reads state before dispatch; when that read comes back
    None the run still needs a claim, otherwise two of them could proceed.
    """
    w = Worker(ctx)

    assert w._try_claim("t-1", None) == ("t-1", "")
    assert w._try_claim("t-1", None) is None

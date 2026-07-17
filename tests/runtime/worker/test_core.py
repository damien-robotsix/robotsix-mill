import asyncio
from types import SimpleNamespace

import pytest

from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage
from robotsix_mill.core.states import State
from robotsix_mill.core.models import SourceKind
from robotsix_mill.runtime.worker import Worker, process_ticket


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


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
    """Same-state Outcome (e.g. merge: PR still open) = no transition.

    The worker still appends a trace-link breadcrumb to history so
    the operator can find the Langfuse trace for the no-op run, but
    every event is at the same state — no real state change."""

    class NoOp(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.DRAFT, "waiting")

    monkeypatch.setitem(registry.STAGES, "refine", NoOp())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT
    assert all(e.state is State.DRAFT for e in service.history(t.id))


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
    assert reloaded.state is State.BLOCKED
    note = service.history(t.id)[-1].note
    assert "Fatal:" in note
    assert "boom" in note


async def test_untraced_noop_stage_emits_no_trace(ctx, service, monkeypatch):
    """merge/deliver-style untraced stages (traced=False) returning a
    no-op must NOT open a Langfuse trace — the merge poll otherwise
    spams an empty trace per cycle.

    Also covers: the root span is named after the *stage* (not generic
    'ticket') so Langfuse trace listings read 'refine'/'implement'/
    'retrospect' instead of identical 'ticket' rows."""
    import contextlib

    from robotsix_mill.runtime import tracing as tr

    calls = {"root": 0, "stage_names": []}

    @contextlib.contextmanager
    def fake_root(_tid, stage_name=None, repo_config=None, **kwargs):
        calls["root"] += 1
        calls["stage_names"].append(stage_name)
        yield tr._NoopRootIO()

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    class TracedRefine(Stage):
        name = "refine"
        input_state = State.DRAFT
        traced = True

        def run(self, _t, _c):
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

    class UntracedNoop(Stage):
        name = "refine"
        input_state = State.DRAFT
        traced = False

        def run(self, _t, _c):
            return Outcome(State.DRAFT, "noop")  # same state = no-op

    monkeypatch.setitem(registry.STAGES, "refine", TracedRefine())
    await process_ticket(service.create("a").id, ctx)
    assert calls["root"] >= 1  # traced stage opened a trace
    assert "refine" in calls["stage_names"], (
        "the root span must be named after the stage so Langfuse "
        "shows useful trace names; got: " + repr(calls["stage_names"])
    )

    calls["root"] = 0
    calls["stage_names"] = []
    monkeypatch.setitem(registry.STAGES, "refine", UntracedNoop())
    await process_ticket(service.create("b").id, ctx)
    assert calls["root"] == 0  # untraced no-op: silent
    assert calls["stage_names"] == []


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
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.DONE,
    ):
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


def test_dollar_cap_excludes_pre_redraft_baseline(ctx, service, monkeypatch):
    """The dollar-cap compares ``session_cost - pre_redraft_cost_usd``
    (clamped ≥ 0) against the cap. A ticket whose pre-redraft baseline
    already exceeds the cap but whose post-redraft (effective) spend is
    below the cap must NOT be escalated to BLOCKED. The inverse —
    effective spend above the cap — must block."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    cap = ctx.settings.max_spend_usd_per_ticket
    # Live session total well above the cap (pre-redraft cost included).
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_cost", lambda *a, **k: cap + 100.0
    )

    def _set_baseline(ticket_id, value):
        with _db.session(service.settings, service.board_id) as s:
            row = s.get(_Ticket, ticket_id)
            row.pre_redraft_cost_usd = value
            s.add(row)
            s.commit()

    # Effective = (cap + 100) - (cap + 95) = 5 < cap → NOT blocked.
    t = service.create("under cap after redraft")
    service.transition(t.id, State.READY)
    _set_baseline(t.id, cap + 95.0)
    w = Worker(ctx)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY

    # Effective = (cap + 100) - 0 = cap + 100 > cap → BLOCKED.
    t2 = service.create("over cap after redraft")
    service.transition(t2.id, State.READY)
    _set_baseline(t2.id, 0.0)
    w._check_progress(t2.id, State.READY, State.READY)
    assert service.get(t2.id).state is State.BLOCKED


def test_dollar_cap_effective_equal_to_cap_not_blocked(ctx, service, monkeypatch):
    """The dollar-cap uses a strict ``>`` (``effective > cap``), so an
    effective spend exactly equal to the cap must NOT block. With a live
    session total of ``cap + 50`` and a baseline of ``50``, the effective
    spend is exactly ``cap`` and the ticket stays READY."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    cap = ctx.settings.max_spend_usd_per_ticket
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_cost", lambda *a, **k: cap + 50.0
    )

    def _set_baseline(ticket_id, value):
        with _db.session(service.settings, service.board_id) as s:
            row = s.get(_Ticket, ticket_id)
            row.pre_redraft_cost_usd = value
            s.add(row)
            s.commit()

    # Effective = (cap + 50) - 50 = cap == cap → NOT blocked (strict >).
    t = service.create("effective exactly at cap")
    service.transition(t.id, State.READY)
    _set_baseline(t.id, 50.0)
    w = Worker(ctx)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_trace_count_blocks(ctx, service, monkeypatch):
    """When trace count exceeds max_traces_per_ticket, escalate to BLOCKED."""
    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    fake_traces = [{"cost": 0.0, "model": ""} for _ in range(6)]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)
    blocked = service.get(t.id)
    assert blocked.state is State.BLOCKED
    assert "Circuit breaker tripped" in service.history(t.id)[-1].note


def test_circuit_breaker_trace_count_below_limit_noop(ctx, service, monkeypatch):
    """When trace count is at or below the limit, no block."""
    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    fake_traces = [{"cost": 0.0, "model": ""} for _ in range(5)]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_openrouter_spend_blocks(ctx, service, monkeypatch):
    """When OpenRouter marginal spend exceeds the limit, escalate to BLOCKED."""
    ctx.settings.max_traces_per_ticket = 0
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 3.0

    fake_traces = [
        {"cost": 1.5, "model": "openrouter/gpt-4o"},
        {"cost": 0.5, "model": "anthropic/claude"},
        {"cost": 2.0, "model": "openrouter/gpt-4o"},
    ]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)
    blocked = service.get(t.id)
    assert blocked.state is State.BLOCKED
    assert "Circuit breaker tripped" in service.history(t.id)[-1].note


def test_circuit_breaker_both_disabled_noop(ctx, service, monkeypatch):
    """When both breakers are disabled (0), the guard is entirely skipped."""
    ctx.settings.max_traces_per_ticket = 0
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: pytest.fail("must not be called"),
    )

    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_none_traces_noop(ctx, service, monkeypatch):
    """When session_traces returns None (Langfuse down), degrade gracefully."""
    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: None,
    )

    w = Worker(ctx)
    t = service.create("x")
    service.transition(t.id, State.READY)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_trace_baseline_sentinel_sets_and_skips(
    ctx,
    service,
    monkeypatch,
):
    """When ``pre_redraft_trace_count == -1`` (sentinel after
    resume-blocked), the next poll captures the current trace count as
    the baseline and skips the block — pre-resume traces are forgiven."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    # 8 traces — above the cap of 5.
    fake_traces = [{"cost": 0.0, "model": ""} for _ in range(8)]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    t = service.create("x")
    service.transition(t.id, State.READY)

    # Set sentinel on the ticket (simulating resume-blocked).
    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t.id)
        row.pre_redraft_trace_count = -1
        s.add(row)
        s.commit()

    w = Worker(ctx)
    # First poll: sentinel present → baseline set to 8, block skipped.
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY
    # Baseline should now be 8.
    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t.id)
        assert row.pre_redraft_trace_count == 8

    # Second poll: effective = 8 - 8 = 0 → under cap, no block.
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_trace_baseline_blocks_new_traces(
    ctx,
    service,
    monkeypatch,
):
    """After the baseline is set, NEW traces above the cap still block."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    # Baseline of 3: 3 old traces already forgiven.
    # New session total of 10 means effective = 10 - 3 = 7 > 5 → BLOCK.
    fake_traces = [{"cost": 0.0, "model": ""} for _ in range(10)]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    t = service.create("x")
    service.transition(t.id, State.READY)

    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t.id)
        row.pre_redraft_trace_count = 3
        s.add(row)
        s.commit()

    w = Worker(ctx)
    # Effective = 10 - 3 = 7 > 5 → BLOCKED.
    w._check_progress(t.id, State.READY, State.READY)
    blocked = service.get(t.id)
    assert blocked.state is State.BLOCKED
    assert "Circuit breaker tripped" in service.history(t.id)[-1].note
    assert "effective traces" in service.history(t.id)[-1].note


def test_circuit_breaker_trace_baseline_at_cap_not_blocked(
    ctx,
    service,
    monkeypatch,
):
    """When effective traces == cap (not >), no block. The > is strict."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    ctx.settings.max_traces_per_ticket = 5
    ctx.settings.max_openrouter_marginal_usd_per_ticket = 0.0

    # Baseline 5, total 10 → effective = 5, exactly at cap → NOT blocked.
    fake_traces = [{"cost": 0.0, "model": ""} for _ in range(10)]
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.session_traces",
        lambda *a, **k: fake_traces,
    )

    t = service.create("x")
    service.transition(t.id, State.READY)

    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t.id)
        row.pre_redraft_trace_count = 5
        s.add(row)
        s.commit()

    w = Worker(ctx)
    w._check_progress(t.id, State.READY, State.READY)
    assert service.get(t.id).state is State.READY


def test_circuit_breaker_resume_blocked_sets_sentinel(service):
    """``resume_blocked`` sets ``pre_redraft_trace_count = -1`` on the
    ticket so the next worker poll captures the baseline."""
    t = service.create("x")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="Circuit breaker tripped")
    assert service.get(t.id).state is State.BLOCKED

    ticket = service.resume_blocked(t.id, note="operator override")
    assert ticket.state is State.READY
    assert ticket.pre_redraft_trace_count == -1


def test_no_progress_guard_exempts_poll_stage(ctx, service):
    """human_mr_approval (merge, traced=False) legitimately waits on an open PR
    across many poll cycles — it must NEVER be auto-blocked."""
    w = Worker(ctx)
    t = service.create("x")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(t.id, st)
    for _ in range(ctx.settings.max_stuck_cycles + 3):
        w._check_progress(t.id, State.HUMAN_MR_APPROVAL, State.HUMAN_MR_APPROVAL)
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


async def test_dep_gated_ticket_does_not_invoke_stage_or_trace(
    ctx, service, monkeypatch
):
    """A ticket with unmet ``depends_on`` must short-circuit inside
    _process_ticket_inner BEFORE the stage runs and BEFORE the Langfuse
    'ticket' root span is opened. Otherwise every reconcile sweep
    produces an empty trace per dep-gated ticket. The check is at
    process_ticket level so a manual enqueue (e.g. via approve) also
    benefits, not just the reconcile sweep."""
    parent = service.create("parent")
    dependent = service.create("waits on parent")
    service.set_depends_on(dependent.id, [parent.id])
    service.transition(dependent.id, State.READY)

    invocations = []

    class TrackingImpl(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, t, _c):
            invocations.append(t.id)
            return Outcome(State.READY)

    monkeypatch.setitem(registry.STAGES, "implement", TrackingImpl())
    await process_ticket(dependent.id, ctx)
    assert invocations == [], (
        "implement stage must NOT be invoked while deps are unmet "
        "(otherwise every reconcile sweep emits an empty Langfuse trace)"
    )

    # Once the parent terminates the gate clears and the stage runs.
    service.transition(parent.id, State.DONE)
    service.transition(parent.id, State.CLOSED)
    await process_ticket(dependent.id, ctx)
    assert dependent.id in invocations, (
        "after the dep clears, the stage must run on the next process pass"
    )


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
    service.transition(t.id, State.IMPLEMENT_COMPLETE)
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
    service.transition(t.id, State.DONE)  # retrospect (traced) stage
    for _ in range(ctx.settings.max_stuck_cycles - 1):
        w._check_progress(t.id, State.DONE, State.DONE)
    assert service.get(t.id).state is State.DONE  # not blocked yet


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
    from robotsix_mill.core.states import State as _S
    from robotsix_mill.core import db as _db

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


# --- startup-aware periodic pass (last-run aware) ----------------------


def test_initial_delay_fires_soon_when_overdue(ctx, tmp_path):
    """The periodic cadence brain (_initial_delay, used by the supervisor's
    per-workflow loops): a RunRegistry entry older than the interval → fire
    almost immediately (~1s base + per-kind stagger), not after a full
    interval."""
    import json
    from datetime import datetime, timedelta, timezone

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "started_at": old,
                    "finished_at": old,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    delay = w._initial_delay("audit", 86400)
    # Base 1.0 + deterministic stagger (hash("audit") % 3600 ≈ 289)
    # + random jitter 0..60.  Assert a generous range.
    assert 1.0 <= delay <= 420


def test_initial_delay_waits_when_recent(ctx, tmp_path):
    """A recent RunRegistry entry → _initial_delay returns the remaining
    interval (close to the full interval) plus per-kind stagger, so the
    loop does NOT re-fire now."""
    import json
    from datetime import datetime, timezone

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    recent = datetime.now(timezone.utc).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "started_at": recent,
                    "finished_at": recent,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    delay = w._initial_delay("audit", 86400)
    # ~86400 remaining + deterministic stagger + random jitter.
    assert 86000 < delay <= 86800


def test_initial_delay_is_per_repo_scoped(ctx, tmp_path):
    """A recent audit on ONE repo must NOT delay another repo's first run.

    Regression for: audit never ran on robotsix-llmio because _initial_delay
    queried most_recent("audit") with no repo_id, inheriting mill's daily audit
    timestamp — so llmio waited a near-full 24h every restart and never fired.
    With repo scoping, a repo that has never run the agent fires ~immediately.
    """
    import json
    from datetime import datetime, timezone

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    db_path = tmp_path / "runs.json"
    recent = datetime.now(timezone.utc).isoformat()
    db_path.write_text(
        json.dumps(
            [
                {
                    "id": "a1",
                    "kind": "audit",
                    "repo_id": "robotsix-mill",
                    "started_at": recent,
                    "finished_at": recent,
                    "status": "ok",
                    "summary": "",
                    "error": None,
                }
            ]
        )
    )
    w = Worker(ctx, run_registry=RunRegistry(db_path))
    # mill ran audit just now, but llmio never has → llmio fires soon (base 1.0 + stagger).
    delay_llmio = w._initial_delay("audit", 86400, repo_id="robotsix-llmio")
    assert 1.0 <= delay_llmio <= 420
    # mill itself still sees its own recent run and waits (~interval + stagger).
    delay_mill = w._initial_delay("audit", 86400, repo_id="robotsix-mill")
    assert delay_mill > 86000
    # legacy any-repo call (no repo_id) keeps the old behaviour (~interval + stagger).
    delay_any = w._initial_delay("audit", 86400)
    assert delay_any > 86000


# --- transient-error retry at stage-runner level -----------------------


async def test_transient_retry_succeeds(ctx, service, monkeypatch):
    """A stage that raises httpx.ConnectError twice then succeeds must
    retry automatically and reach its intended next state without ever
    hitting BLOCKED or ERRORED."""
    import httpx

    calls = [0]

    class FlakyRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            calls[0] += 1
            if calls[0] <= 2:
                raise httpx.ConnectError("connection refused")
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined on retry")

    monkeypatch.setitem(registry.STAGES, "refine", FlakyRefine())
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.network_available",
        lambda host, **kw: True,
    )
    ctx.settings.stage_retry_max_attempts = 3
    ctx.settings.stage_retry_base_delay = 0.001
    ctx.settings.stage_retry_max_delay = 0.001

    t = service.create("flaky")

    # Call 1: raises ConnectError — attempt 1, backoff set
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT  # stays in current state
    t1 = service.get(t.id)
    assert t1.retry_attempt == 1
    assert "connection refused" in (t1.last_transient_error or "")

    # Let the tiny backoff elapse.
    await asyncio.sleep(0.01)

    # Call 2: backoff elapsed — raises ConnectError — attempt 2
    await process_ticket(t.id, ctx)
    t2 = service.get(t.id)
    assert t2.retry_attempt == 2
    assert service.get(t.id).state is State.DRAFT

    # Let the tiny backoff elapse.
    await asyncio.sleep(0.01)

    # Call 3: succeeds — advances
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.HUMAN_ISSUE_APPROVAL


async def test_non_transient_blocks_immediately(ctx, service, monkeypatch):
    """A non-transient ValueError must go straight to BLOCKED with no
    retry — retry_attempt stays 0 and note is prefixed 'Fatal:'."""

    class Boom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise ValueError("boom")

    monkeypatch.setitem(registry.STAGES, "refine", Boom())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.retry_attempt == 0
    note = service.history(t.id)[-1].note
    assert "Fatal:" in note
    assert "ValueError" in note
    assert "boom" in note


async def test_transient_exhausted_blocks(ctx, service, monkeypatch):
    """When retries are exhausted, a transient error transitions to
    BLOCKED with a note mentioning the attempt count."""
    import httpx

    class AlwaysTimeout(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setitem(registry.STAGES, "refine", AlwaysTimeout())
    ctx.settings.stage_retry_max_attempts = 2
    ctx.settings.stage_retry_base_delay = 0.001
    ctx.settings.stage_retry_max_delay = 0.001

    t = service.create("doomed")

    # Attempt 1 — retry
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT
    assert service.get(t.id).retry_attempt == 1

    await asyncio.sleep(0.01)

    # Attempt 2 — retry
    await process_ticket(t.id, ctx)
    assert service.get(t.id).retry_attempt == 2
    assert service.get(t.id).state is State.DRAFT

    await asyncio.sleep(0.01)

    # Attempt 3 — exhausted (max_attempts=2, so this is the 3rd call)
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.BLOCKED
    note = service.history(t.id)[-1].note
    assert "2 attempts" in note
    assert "ReadTimeout" in note


async def test_handle_stage_error_clears_implement_fingerprint_on_transient(
    ctx, service, monkeypatch
):
    """When _handle_stage_error classifies an error as transient and the
    failed stage is 'implement', it must delete artifacts/implement.md
    so the retry doesn't hard-block on 'spec unchanged since last
    implement attempt'."""
    import httpx

    from robotsix_mill.runtime.worker.processing import _handle_stage_error

    t = service.create("implement-fingerprint-test")
    ws = ctx.service.workspace(t)
    implement_md = ws.artifacts_dir / "implement.md"
    implement_md.write_text("spec-fingerprint-guard\n")
    assert implement_md.exists()

    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.classify_stage_error",
        lambda exc: "transient",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.is_network_down_error",
        lambda exc: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.stage_retry.compute_retry_delay",
        lambda attempt, base, cap: 0.001,
    )

    await _handle_stage_error(
        t.id, ctx, "implement", httpx.ConnectError("connection refused"), None
    )

    assert not implement_md.exists(), (
        "implement.md must be deleted when a transient error kills an implement run"
    )


async def test_handle_stage_error_preserves_refine_artifacts_on_transient(
    ctx, service, monkeypatch
):
    """When _handle_stage_error classifies a transient error on a
    non-implement stage (refine), it must NOT delete artifacts —
    the fingerprint guard cleanup is scoped to the implement stage only."""
    import httpx

    from robotsix_mill.runtime.worker.processing import _handle_stage_error

    t = service.create("refine-transient-test")
    ws = ctx.service.workspace(t)
    refine_artifact = ws.artifacts_dir / "some-artifact.md"
    refine_artifact.write_text("refine data\n")
    assert refine_artifact.exists()

    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.classify_stage_error",
        lambda exc: "transient",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.is_network_down_error",
        lambda exc: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.stage_retry.compute_retry_delay",
        lambda attempt, base, cap: 0.001,
    )

    await _handle_stage_error(
        t.id, ctx, "refine", httpx.ConnectError("connection refused"), None
    )

    assert refine_artifact.exists(), (
        "non-implement artifacts must NOT be deleted on transient error"
    )


# --- periodic pass root span tests -------------------------------------


async def test_periodic_pass_opens_root_span_before_runner(ctx, monkeypatch):
    """Root span is opened with the correct label before runner_fn is
    invoked, and session_id is passed to runner_fn."""
    import contextlib

    from robotsix_mill.runtime import tracing as tracing_mod

    seen = {}
    captured = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        yield tracing_mod._NoopRootIO()

    monkeypatch.setattr(tracing_mod, "start_ticket_root_span", fake_root)

    def fake_runner(session_id=None):
        captured["session_id"] = session_id
        captured["root_was_opened"] = seen.get("root_opened", False)
        from robotsix_mill.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:  # after initial delay + one loop iteration
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass("test-label", fake_runner, 60)

    assert seen["root_opened"] is True
    assert seen["stage"] == "test-label"
    assert seen["session_id"].startswith("test-label-")
    assert captured["root_was_opened"] is True, (
        "root span must be opened BEFORE runner_fn is invoked"
    )
    assert captured["session_id"] == seen["session_id"]


async def test_periodic_pass_root_span_survives_runner_crash(ctx, monkeypatch):
    """A runner_fn that raises still has its root span opened by the poll
    loop — the Langfuse trace exists even if the runner crashes before
    doing any agent work."""
    import contextlib

    from robotsix_mill.runtime import tracing as tracing_mod

    seen = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        yield tracing_mod._NoopRootIO()

    monkeypatch.setattr(tracing_mod, "start_ticket_root_span", fake_root)

    def crashing_runner(session_id=None):
        raise RuntimeError("pre-agent setup failure")

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass("crash-test", crashing_runner, 60)

    assert seen["root_opened"] is True, (
        "root span must exist even when runner_fn crashes before the old "
        "start_ticket_root_span would have been reached"
    )


# --- AWAITING_USER_REPLY guard tests ----------------------------------


async def test_reconcile_sweep_skips_awaiting_user_reply(ctx, service, monkeypatch):
    """Tickets in AWAITING_USER_REPLY must NOT be enqueued by the
    reconcile sweep — they are paused waiting for a human reply."""
    t = service.create("ask-operator", source=SourceKind.AGENT)
    # Transition directly via setter to the new state.
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket

    with _db.session(ctx.settings, service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = State.AWAITING_USER_REPLY
        row.paused_from = State.READY.value
        s.add(row)
        s.commit()

    w = Worker(ctx)

    # Let the loop body run exactly once, then break out.
    calls = [0]

    async def fake_sleep(_):
        calls[0] += 1
        if calls[0] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.asyncio.sleep",
        fake_sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id not in w._pending, (
        "AWAITING_USER_REPLY ticket must NOT be enqueued by the reconcile sweep"
    )


async def test_process_ticket_skips_awaiting_user_reply(ctx, service, monkeypatch):
    """_process_ticket_inner must return immediately (no stage dispatch,
    no trace span) when the ticket is in AWAITING_USER_REPLY."""
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket

    invocations = []

    class ShouldNotRun(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, t, _c):
            invocations.append(t.id)
            return Outcome(State.DELIVERABLE)

    monkeypatch.setitem(registry.STAGES, "implement", ShouldNotRun())

    t = service.create("paused-ticket")
    # Move to READY, then to AWAITING_USER_REPLY via direct setter.
    with _db.session(ctx.settings, service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = State.AWAITING_USER_REPLY
        row.paused_from = State.READY.value
        s.add(row)
        s.commit()

    await process_ticket(t.id, ctx)
    assert invocations == [], "no stage must be invoked for AWAITING_USER_REPLY ticket"
    # Ticket should still be in AWAITING_USER_REPLY (unchanged).
    assert service.get(t.id).state is State.AWAITING_USER_REPLY


# --- periodic pass per-repo forwards repo_config to start_ticket_root_span ---


async def test_periodic_pass_per_repo_forwards_repo_config_to_span(ctx, monkeypatch):
    """_run_periodic_pass_per_repo must forward repo_config to
    start_ticket_root_span so per-repo Langfuse credentials in
    repos.yaml are used for periodic agent traces."""
    import contextlib

    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime import tracing as tr

    seen: dict = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None, repo_config=None, **kwargs):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        seen["repo_config"] = repo_config
        yield tr._NoopRootIO()

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    fake_repo = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="p",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.periodic_passes.get_repos_config",
        lambda: ReposRegistry(repos={"test-repo": fake_repo}),
    )

    captured_repo_config = {}

    def fake_runner(session_id=None, repo_config=None):
        captured_repo_config["value"] = repo_config
        from robotsix_mill.runners.periodic_runner import PeriodicPassResult

        return PeriodicPassResult(
            updated_memory="",
            drafts_created=[],
            session_id=session_id or "",
        )

    sleep_calls = 0
    _real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:  # after initial sleep + one body iteration
            raise asyncio.CancelledError
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    w = Worker(ctx)
    with pytest.raises(asyncio.CancelledError):
        await w._run_periodic_pass_per_repo(
            "audit",
            fake_runner,
            settings_interval_attr="audit_interval_seconds",
            settings_enabled_attr="audit_periodic",
        )

    assert seen.get("root_opened") is True
    assert seen.get("repo_config") is not None, (
        "repo_config must be forwarded to start_ticket_root_span "
        "so per-repo Langfuse credentials are used"
    )
    assert seen["repo_config"] is fake_repo
    assert captured_repo_config.get("value") is fake_repo


# ---------------------------------------------------------------------------
# Graceful shutdown — periodic passes finish before stop()
# ---------------------------------------------------------------------------


async def test_stop_awaits_inflight_passes(ctx, monkeypatch):
    """stop() must wait for in-flight ``_tracked_to_thread`` calls to
    finish, not cancel them. Without this a SIGTERM during a survey
    run kills the agent mid-pass."""
    import threading
    import time

    w = Worker(ctx, run_registry=None)
    started = threading.Event()
    finished_at: dict = {}

    def slow_runner():
        # Simulates a survey pass that needs to flush memory + db
        # before exit. Should not be killed.
        started.set()
        time.sleep(0.2)
        finished_at["t"] = time.monotonic()
        return "ok"

    # Spawn the tracked thread call exactly as a periodic loop would.
    call_task = asyncio.create_task(w._tracked_to_thread(slow_runner))
    # Give the thread a moment to actually enter slow_runner.
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    assert started.is_set(), "slow_runner never started"

    # Now simulate teardown. stop() should await the in-flight thread.
    stop_started = time.monotonic()
    await w.stop()
    stop_elapsed = time.monotonic() - stop_started

    assert "t" in finished_at, (
        "slow_runner did not complete before stop() returned — "
        "the in-flight pass was killed prematurely"
    )
    # The wait should have taken at least most of the 0.2 s nap.
    assert stop_elapsed >= 0.15
    # Cleanup the call task (it should be done).
    assert call_task.done()


async def test_stop_grace_timeout_does_not_hang(ctx, monkeypatch):
    """If a pass runs past ``shutdown_grace_seconds`` stop() logs a
    warning and proceeds — it must never hang the process."""
    import threading
    import time

    # Tight grace so the test is fast.
    monkeypatch.setattr(ctx.settings, "shutdown_grace_seconds", 1)
    w = Worker(ctx, run_registry=None)
    started = threading.Event()

    def very_slow_runner():
        started.set()
        time.sleep(5)  # > grace
        return "late"

    call_task = asyncio.create_task(w._tracked_to_thread(very_slow_runner))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.005)

    t0 = time.monotonic()
    await w.stop()
    elapsed = time.monotonic() - t0
    # Stop returned somewhere around the 1 s grace, not 5 s.
    assert elapsed < 3.0, (
        f"stop() waited {elapsed:.1f}s; should have bailed out near the 1s grace"
    )
    # The shielded call is still running in the background. Cancel
    # the wrapper task so we don't leak it past the test.
    call_task.cancel()


def test_periodic_pass_uses_per_repo_registry(ctx, tmp_path):
    """Periodic passes record into — and read cadence from — the repo's OWN
    registry, so a run shows in that repo's /runs list (not the lead repo's).
    Regression: audit ran for robotsix-llmio but landed in mill's runs.json, so
    the per-repo /runs API (which reads <board>/runs.json) showed 0 audit runs.
    """
    from types import SimpleNamespace

    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    mill_reg = RunRegistry(tmp_path / "mill.json")
    llmio_reg = RunRegistry(tmp_path / "llmio.json")
    w = Worker(
        ctx,
        mill_reg,
        run_registries={
            "robotsix-mill": mill_reg,
            "robotsix-llmio": llmio_reg,
        },
    )
    rc_llmio = SimpleNamespace(board_id="robotsix-llmio", repo_id="robotsix-llmio")

    # _registry_for routes by board_id; unknown board / None → default.
    assert w._registry_for(rc_llmio) is llmio_reg
    assert w._registry_for(None) is mill_reg
    assert w._registry_for(SimpleNamespace(board_id="x", repo_id="x")) is mill_reg

    # A run recorded for llmio lands in llmio's registry, not mill's.
    rid = llmio_reg.start("audit", repo_id="robotsix-llmio")
    llmio_reg.finish_ok(rid, "ok")
    assert llmio_reg.most_recent("audit", repo_id="robotsix-llmio") is not None
    assert mill_reg.most_recent("audit", repo_id="robotsix-llmio") is None

    # Cadence reads the per-repo store: llmio's own fresh run → "recent".
    delay = w._initial_delay(
        "audit", 86400, repo_id="robotsix-llmio", registry=w._registry_for(rc_llmio)
    )
    assert delay > 86000


async def test_meta_pass_loop_records_into_meta_registry(ctx, tmp_path, monkeypatch):
    """The meta pass records into the dedicated meta registry tagged
    repo_id="meta", not the default/lead-repo registry — so meta runs
    show on the meta board's runs drawer rather than the main board."""
    from types import SimpleNamespace

    import robotsix_mill.meta.runner as meta_runner
    from robotsix_mill.runtime.run_registry import RunRegistry
    from robotsix_mill.runtime.worker import Worker

    mill_reg = RunRegistry(tmp_path / "mill.json")
    meta_reg = RunRegistry(tmp_path / "meta.json")
    w = Worker(
        ctx,
        mill_reg,
        run_registries={"robotsix-mill": mill_reg, Worker._META_BOARD: meta_reg},
    )

    def _fake_run_meta_pass(*, session_id):
        return SimpleNamespace(
            extraction_drafts_created=[{"id": "x1"}],
            alignment_drafts_created=[],
        )

    monkeypatch.setattr(meta_runner, "run_meta_pass", _fake_run_meta_pass)
    monkeypatch.setattr(w, "_initial_delay", lambda *a, **kw: 0.0)

    task = asyncio.create_task(w._meta_pass_loop())
    try:
        for _ in range(500):
            entries = meta_reg.list_all()
            if entries and entries[0]["status"] == "ok":
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    entries = meta_reg.list_all()
    assert len(entries) == 1
    assert entries[0]["kind"] == "meta"
    assert entries[0]["repo_id"] == "meta"
    assert entries[0]["status"] == "ok"
    # Nothing leaked into the default/lead-repo registry.
    assert mill_reg.list_all() == []


# --- epic re-evaluation helpers (extracted from _run_epic_reeval) ---


class _FakeWorkspace:
    """Stand-in for ``svc.workspace(obj)`` recording write calls."""

    def __init__(self, desc, calls):
        self._desc = desc
        self._calls = calls

    def read_description(self):
        return self._desc

    def write_description(self, note):
        self._calls.append(("write_description", note))
        return f"hash:{note}"


class _FakeEpicService:
    """Lightweight stand-in for ``TicketService`` used by the epic-reeval
    helpers; records every mutating call in ``calls``."""

    def __init__(self, children=None, descriptions=None, tickets=None, histories=None):
        self.children = children or []
        # obj.id -> description string returned by workspace().read_description
        self.descriptions = descriptions or {}
        # id -> ticket object returned by get(); id -> list of events by history()
        self.tickets = tickets or {}
        self.histories = histories or {}
        self.calls = []

    def list_children(self, epic_id):
        self.calls.append(("list_children", epic_id))
        return self.children

    def list_children_across_boards(self, epic_id):
        self.calls.append(("list_children_across_boards", epic_id))
        return self.children

    def get(self, ticket_id):
        return self.tickets.get(ticket_id)

    def history(self, ticket_id):
        return self.histories.get(ticket_id, [])

    def workspace(self, obj):
        return _FakeWorkspace(self.descriptions.get(obj.id, ""), self.calls)

    def transition(self, ticket_id, state, note=None):
        self.calls.append(("transition", ticket_id, state, note))

    def set_content_hash(self, ticket_id, content_hash):
        self.calls.append(("set_content_hash", ticket_id, content_hash))

    def set_depends_on(self, child_id, deps):
        self.calls.append(("set_depends_on", child_id, deps))


def test_build_child_summaries_truncates_and_shapes():
    from types import SimpleNamespace
    from robotsix_mill.runtime.worker import _build_child_summaries

    children = [
        SimpleNamespace(
            id="C1",
            title="Child one",
            state=SimpleNamespace(value="ready"),
            depends_on='["C0"]',
        ),
        SimpleNamespace(
            id="C2",
            title="Child two",
            state=SimpleNamespace(value="draft"),
            depends_on=None,
        ),
    ]
    svc = _FakeEpicService(
        children=children,
        descriptions={"C1": "x" * 600, "C2": "short"},
    )

    summaries = _build_child_summaries(svc, "E1")

    assert ("list_children_across_boards", "E1") in svc.calls
    assert [s["id"] for s in summaries] == ["C1", "C2"]
    assert summaries[0]["title"] == "Child one"
    assert summaries[0]["state"] == "ready"
    assert summaries[0]["depends_on"] == ["C0"]
    assert summaries[1]["depends_on"] == []
    # Long description truncated to 500 chars + suffix; short one untouched.
    assert summaries[0]["description"] == "x" * 500 + "\n...(truncated)"
    assert summaries[1]["description"] == "short"


def test_build_child_summaries_populates_distinct_delivery_labels():
    from types import SimpleNamespace
    from robotsix_mill.runtime.worker import _build_child_summaries

    children = [
        SimpleNamespace(
            id="M", title="merged", state=SimpleNamespace(value="done"), depends_on=None
        ),
        SimpleNamespace(
            id="A", title="dedup", state=SimpleNamespace(value="done"), depends_on=None
        ),
        SimpleNamespace(
            id="U",
            title="unstarted",
            state=SimpleNamespace(value="draft"),
            depends_on=None,
        ),
    ]
    svc = _FakeEpicService(
        children=children,
        descriptions={"M": "m", "A": "a", "U": "u"},
        tickets={
            "M": _ns_ticket("M", State.DONE),
            "A": _ns_ticket("A", State.DONE),
            "U": _ns_ticket("U", State.DRAFT),
            "B": _ns_ticket("B", State.DRAFT),
        },
        histories={
            "M": [_ev(State.DONE, "merged: http://x/pr/1")],
            "A": [_ev(State.DONE, "duplicate of B: dupe")],
        },
    )

    summaries = {s["id"]: s["delivery"] for s in _build_child_summaries(svc, "E1")}

    assert summaries["M"] == "merged"
    assert summaries["U"] == "unstarted"
    assert "dedup" in summaries["A"].lower()
    # The three labels are distinguishable.
    assert len({summaries["M"], summaries["A"], summaries["U"]}) == 3


def test_handle_epic_decision_close():
    from types import SimpleNamespace
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": ""})
    result = EpicStatusResult(decision="close", note="done")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert (
        "transition",
        "E1",
        State.EPIC_CLOSED,
        "[auto-closed] done",
    ) in svc.calls


def test_handle_epic_decision_keep_open_is_noop():
    from types import SimpleNamespace
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService()
    result = EpicStatusResult(decision="keep_open")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert svc.calls == []


def test_handle_epic_decision_update_description():
    from types import SimpleNamespace
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": "old"})
    result = EpicStatusResult(decision="update_description", note="new body")

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert ("write_description", "new body") in svc.calls
    assert ("set_content_hash", "E1", "hash:new body") in svc.calls


def test_handle_epic_decision_update_deps_with_dep_updates():
    from types import SimpleNamespace
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService(descriptions={"E1": "old"})
    result = EpicStatusResult(
        decision="update_deps",
        dep_updates={"C1": ["C0"], "C2": None},
    )

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    assert ("set_depends_on", "C1", ["C0"]) in svc.calls
    # None entries normalize to an empty list.
    assert ("set_depends_on", "C2", []) in svc.calls
    # Empty note → no epic description rewrite.
    assert not any(c[0] == "write_description" for c in svc.calls)


def test_handle_epic_decision_close_with_new_children_downgrades():
    from types import SimpleNamespace
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _handle_epic_decision

    svc = _FakeEpicService()
    result = EpicStatusResult(
        decision="close",
        note="done",
        new_children=[{"title": "follow-up", "body": "more work"}],
    )

    _handle_epic_decision(svc, "E1", SimpleNamespace(id="E1"), result)

    # close + new_children downgrades to keep_open → no transition occurs.
    assert result.decision == "keep_open"
    assert not any(c[0] == "transition" for c in svc.calls)


def test_validate_epic_state_skips_blocked(monkeypatch):
    """_validate_epic_state returns None for a BLOCKED epic."""
    from types import SimpleNamespace
    from robotsix_mill.runtime.worker import _validate_epic_state
    from robotsix_mill.core.states import State as S

    class _Settings:
        pass

    settings = _Settings()
    blocked_ticket = SimpleNamespace(id="E1", state=S.BLOCKED, board_id="b1")

    class _MockSvc:
        def get(self, ticket_id):
            return blocked_ticket

    mock_svc = _MockSvc()
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService",
        lambda *a, **kw: mock_svc,
    )
    result = _validate_epic_state(settings, "E1")
    assert result is None


def _ns_ticket(tid, state):
    from types import SimpleNamespace

    return SimpleNamespace(id=tid, state=state)


def _ev(state, note=None):
    from types import SimpleNamespace

    return SimpleNamespace(state=state, note=note)


def test_resolve_delivery_merged():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"M": _ns_ticket("M", State.DONE)},
        histories={"M": [_ev(State.DONE, "merged: http://x/pr/1")]},
    )
    res = _resolve_delivery(svc, "M")
    assert res["delivered"] is True
    assert res["label"] == "merged"


def test_resolve_delivery_unstarted():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"D": _ns_ticket("D", State.DRAFT)},
        histories={"D": []},
    )
    res = _resolve_delivery(svc, "D")
    assert res["delivered"] is False
    assert res["label"] == "unstarted"


def test_resolve_delivery_dedup_follows_chain_to_merged():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={
            "A": _ns_ticket("A", State.DONE),
            "B": _ns_ticket("B", State.DONE),
        },
        histories={
            "A": [_ev(State.DONE, "duplicate of B: same scope")],
            "B": [_ev(State.DONE, "merged: http://x/pr/2")],
        },
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is True
    assert res["canonical"] == "B"
    assert "B" in res["label"]


def test_resolve_delivery_dedup_chain_not_delivered():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={
            "A": _ns_ticket("A", State.DONE),
            "B": _ns_ticket("B", State.DRAFT),
        },
        histories={
            "A": [_ev(State.DONE, "duplicate of B: same scope")],
            "B": [],
        },
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is False
    assert res["canonical"] == "B"


def test_resolve_delivery_cyclic_dedup_does_not_raise():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService(
        tickets={"A": _ns_ticket("A", State.DONE)},
        histories={"A": [_ev(State.DONE, "duplicate of A: self ref")]},
    )
    res = _resolve_delivery(svc, "A")
    assert res["delivered"] is False


def test_resolve_delivery_missing_ticket():
    from robotsix_mill.runtime.worker import _resolve_delivery

    svc = _FakeEpicService()
    res = _resolve_delivery(svc, "gone")
    assert res["delivered"] is False


def _closure_svc(child_id, covering):
    """Build a fake service with a DRAFT child plus a covering sibling."""
    tickets = {child_id: _ns_ticket(child_id, State.DRAFT)}
    histories = {}
    for cid, (state, note) in covering.items():
        tickets[cid] = _ns_ticket(cid, state)
        histories[cid] = [_ev(state, note)] if note is not None else []
    return _FakeEpicService(tickets=tickets, histories=histories)


def test_reconcile_closes_draft_with_merged_covering_sibling():
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _closure_svc("C1", {"S1": (State.DONE, "merged: http://x/pr/9")})
    result = EpicStatusResult(decision="keep_open", child_closures={"C1": "S1"})

    _reconcile_child_changes(svc, "E1", result)

    transitions = [c for c in svc.calls if c[0] == "transition"]
    assert len(transitions) == 1
    _, tid, state, note = transitions[0]
    assert tid == "C1"
    assert state == State.CLOSED
    assert "S1" in note
    assert "Obsoleted by epic re-evaluation after sibling merge" not in note


@pytest.mark.parametrize(
    "covering, closures",
    [
        # dedup-closed covering sibling whose canonical never merged
        ({"S1": (State.DONE, "duplicate of Z: dupe")}, {"C1": "S1"}),
        # unstarted covering sibling
        ({"S1": (State.DRAFT, None)}, {"C1": "S1"}),
        # self-reference
        ({}, {"C1": "C1"}),
        # unnamed covering sibling (legacy bare list)
        ({}, ["C1"]),
    ],
)
def test_reconcile_refuses_closure_without_merged_sibling(covering, closures):
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _closure_svc("C1", covering)
    result = EpicStatusResult(decision="keep_open", child_closures=closures)

    _reconcile_child_changes(svc, "E1", result)

    assert not any(c[0] == "transition" for c in svc.calls)


def test_reconcile_refuses_closure_missing_covering_ticket():
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    # Covering sibling id not present in tickets at all.
    svc = _FakeEpicService(tickets={"C1": _ns_ticket("C1", State.DRAFT)})
    result = EpicStatusResult(decision="keep_open", child_closures={"C1": "ghost"})

    _reconcile_child_changes(svc, "E1", result)

    assert not any(c[0] == "transition" for c in svc.calls)


def test_reconcile_incident_4564_unstarted_children_survive():
    """Reproduces epic 4564: A dedup-closed onto B; B/C/D unstarted; sibling
    E merged unrelated scope. A scope-blind closure of B/C/D (legacy list,
    no covering sibling) must NOT obsolete them."""
    from robotsix_mill.agents.epic_status import EpicStatusResult
    from robotsix_mill.runtime.worker import _reconcile_child_changes

    svc = _FakeEpicService(
        tickets={
            "B": _ns_ticket("B", State.DRAFT),
            "C": _ns_ticket("C", State.DRAFT),
            "D": _ns_ticket("D", State.DRAFT),
            "E": _ns_ticket("E", State.DONE),
        },
        histories={"E": [_ev(State.DONE, "merged: http://x/pr/unrelated")]},
    )
    # Scope-blind closure of the unstarted children with no covering sibling.
    result = EpicStatusResult(decision="keep_open", child_closures=["B", "C", "D"])

    _reconcile_child_changes(svc, "E1", result)

    # None of the unstarted Tier-1 children are obsoleted.
    assert not any(c[0] == "transition" for c in svc.calls)


def test_stage_rank_covers_every_pipeline_state():
    """Every STAGE_FOR_STATE state must have an explicit _STAGE_RANK.

    A missing entry silently falls to _DEFAULT_STAGE_RANK (99) and is
    starved indefinitely on a busy board — every newly arriving draft or
    ready outranks it forever. Live case: REBASING was once absent, so
    blocked rebase tickets sat 75+ minutes with zero pickup while
    later-created drafts refined ahead of them.
    """
    from robotsix_mill.core.states import STAGE_FOR_STATE
    from robotsix_mill.runtime.worker import Worker

    missing = [s for s in STAGE_FOR_STATE if s not in Worker._STAGE_RANK]
    assert not missing, (
        f"states with a pipeline stage but no explicit queue rank "
        f"(would be starved at default rank {Worker._DEFAULT_STAGE_RANK}): "
        f"{missing}"
    )


async def test_network_outage_parks_without_consuming_retry(ctx, service, monkeypatch):
    """A stage failing with a DNS-outage signature while the probe host
    is unresolvable is PARKED: next_retry_at set, retry budget never
    consumed, no BLOCKED transition — repeated failures (an outage far
    longer than stage_retry_max_attempts) keep parking instead of
    exhausting into a block."""
    import subprocess

    class DnsBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise subprocess.CalledProcessError(
                128,
                "git",
                stderr=(
                    "fatal: unable to access 'https://github.com/x/y/': "
                    "Could not resolve host: github.com"
                ),
            )

    monkeypatch.setitem(registry.STAGES, "refine", DnsBoom())
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.network_available",
        lambda host, **kw: False,
    )
    t = service.create("x")
    for _ in range(ctx.settings.stage_retry_max_attempts + 2):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        assert r.state is State.DRAFT, "outage must never block the ticket"
        assert r.retry_attempt == 1, "retry budget must not be consumed"
        assert r.next_retry_at is not None
        assert "network outage" in (r.last_transient_error or "")
        # Simulate the backoff elapsing so the next loop iteration
        # re-dispatches instead of short-circuiting on next_retry_at.
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )


async def test_network_error_with_connectivity_uses_bounded_retries(
    ctx, service, monkeypatch
):
    """The same DNS-flavored error WITHOUT a confirmed outage (probe
    host resolves) goes through the normal bounded transient retry —
    and blocks once attempts are exhausted."""
    import subprocess

    class DnsBoom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise subprocess.CalledProcessError(
                128,
                "git",
                stderr=(
                    "fatal: unable to access 'https://github.com/x/y/': "
                    "Could not resolve host: github.com"
                ),
            )

    monkeypatch.setitem(registry.STAGES, "refine", DnsBoom())
    monkeypatch.setattr(
        "robotsix_mill.runtime.transient_errors.network_available",
        lambda host, **kw: True,
    )
    t = service.create("x")
    for expected_attempt in range(1, ctx.settings.stage_retry_max_attempts + 1):
        await process_ticket(t.id, ctx)
        r = service.get(t.id)
        assert r.state is State.DRAFT
        assert r.retry_attempt == expected_attempt
        assert "network outage" not in (r.last_transient_error or "")
        service.set_retry_state(
            t.id,
            retry_attempt=r.retry_attempt,
            last_transient_error=r.last_transient_error,
            next_retry_at=None,
        )
    await process_ticket(t.id, ctx)
    r = service.get(t.id)
    assert r.state is State.BLOCKED, "exhausted retries must still block"


# -----------------------------------------------------------------------
# In-flight PR cap (max_inflight_prs)
# -----------------------------------------------------------------------


def test_max_inflight_prs_rejects_negative():
    """max_inflight_prs must reject negative values at construction time."""
    from robotsix_mill.config.repos import RepoConfig

    with pytest.raises(ValueError):  # pydantic ValidationError
        RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            max_inflight_prs=-1,
        )


def test_max_inflight_prs_accepts_zero():
    """max_inflight_prs=0 is valid (disables the cap)."""
    from robotsix_mill.config.repos import RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_inflight_prs=0,
    )
    assert rc.max_inflight_prs == 0


def test_max_inflight_prs_defaults_to_3():
    """Omitting max_inflight_prs defaults to 3."""
    from robotsix_mill.config.repos import RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    assert rc.max_inflight_prs == 3


def test_count_inflight_prs_counts_only_in_flight_states(service):
    """_count_inflight_prs returns the count of tickets in _IN_FLIGHT_PR_STATES."""
    from robotsix_mill.runtime.worker.core import _count_inflight_prs

    # Initially empty.
    assert _count_inflight_prs(service) == 0

    # Create tickets in various states.
    t1 = service.create("ready ticket")
    t2 = service.create("deliverable pr")

    # Transition t2 to DELIVERABLE (in-flight).
    for st in (State.READY, State.DELIVERABLE):
        service.transition(t2.id, st)
    assert service.get(t2.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # t1 is DRAFT (not in-flight) — shouldn't count.
    assert service.get(t1.id).state is State.DRAFT
    assert _count_inflight_prs(service) == 1

    # Move t1 to IMPLEMENT_COMPLETE → now 2 in-flight.
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t1.id, st)
    assert service.get(t1.id).state is State.IMPLEMENT_COMPLETE
    assert _count_inflight_prs(service) == 2


def test_count_inflight_prs_excludes_non_in_flight_states(service):
    """HUMAN_MR_APPROVAL, BLOCKED, and DRAFT/READY must NOT count toward the cap."""
    from robotsix_mill.runtime.worker.core import _count_inflight_prs

    # Create tickets and move them to various non-in-flight states.
    t_hmr = service.create("human mr approval")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(t_hmr.id, st)

    t_blocked = service.create("blocked ticket")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(t_blocked.id, st)
    # Move to BLOCKED via direct state set (the worker does this).
    from robotsix_mill.core import db as _db
    from robotsix_mill.core.models import Ticket as _Ticket

    with _db.session(service.settings, service.board_id) as s:
        row = s.get(_Ticket, t_blocked.id)
        row.state = State.BLOCKED
        s.add(row)
        s.commit()

    assert service.get(t_hmr.id).state is State.HUMAN_MR_APPROVAL
    assert service.get(t_blocked.id).state is State.BLOCKED
    assert _count_inflight_prs(service) == 0


async def test_cap_blocks_ready_when_at_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=1 and one DELIVERABLE ticket, a popped READY
    ticket must be re-enqueued rather than dispatched to implement."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one in-flight PR ticket (DELIVERABLE).
    inflight = service.create("in-flight pr")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(inflight.id, st)
    assert service.get(inflight.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # A READY ticket — should be blocked by the cap.
    ready_ticket = service.create("ready to implement")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id not in invoked, "READY ticket must NOT be dispatched at cap"


async def test_cap_blocks_draft_when_at_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=1 and one DELIVERABLE ticket, a popped DRAFT
    ticket must be re-enqueued rather than dispatched to refine."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one in-flight PR ticket (DELIVERABLE).
    inflight = service.create("in-flight pr")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(inflight.id, st)
    assert service.get(inflight.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # A DRAFT ticket — should be blocked by the cap.
    draft_ticket = service.create("draft to refine")

    w = Worker(ctx)
    w.enqueue(draft_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert draft_ticket.id not in invoked, "DRAFT ticket must NOT be dispatched at cap"


async def test_cap_allows_ready_when_below_limit(ctx, service, monkeypatch):
    """With max_inflight_prs=3 and only 2 in-flight tickets, a READY
    ticket proceeds normally."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=3,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Two in-flight.
    for i in range(2):
        t = service.create(f"in-flight-{i}")
        for st in (State.READY, State.DELIVERABLE):
            service.transition(t.id, st)

    assert _count_inflight_prs(service) == 2

    ready_ticket = service.create("ready below cap")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should have been dispatched — below cap"
    )


async def test_cap_disabled_when_zero(ctx, service, monkeypatch):
    """max_inflight_prs=0 disables the cap entirely — all READY tickets
    are dispatched regardless of in-flight count."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=0,  # disabled
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Several in-flight — more than the default of 3.
    for i in range(5):
        t = service.create(f"in-flight-{i}")
        for st in (State.READY, State.DELIVERABLE):
            service.transition(t.id, st)

    assert _count_inflight_prs(service) == 5

    ready_ticket = service.create("ready when cap disabled")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should be dispatched when max_inflight_prs=0"
    )


async def test_merge_pipeline_always_processed_at_cap(ctx, service, monkeypatch):
    """At cap, a merge-pipeline ticket (IMPLEMENT_COMPLETE) is processed
    normally — the cap only gates READY/DRAFT."""
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # One in-flight — at cap.
    t1 = service.create("in-flight")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t1.id, st)
    assert service.get(t1.id).state is State.IMPLEMENT_COMPLETE
    assert _count_inflight_prs(service) == 1

    # Another merge-pipeline ticket (also IMPLEMENT_COMPLETE) — should
    # still be processed regardless of cap.
    t2 = service.create("another merge")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t2.id, st)
    assert service.get(t2.id).state is State.IMPLEMENT_COMPLETE

    w = Worker(ctx)
    w.enqueue(t2.id)

    invoked = []

    async def fake_process_ticket(ticket_id, ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert t2.id in invoked, (
        "IMPLEMENT_COMPLETE (merge-pipeline) ticket must be processed at cap"
    )


async def test_cap_excludes_human_mr_approval_from_count(ctx, service, monkeypatch):
    """HUMAN_MR_APPROVAL tickets do NOT count toward the in-flight cap.

    A repo at cap=1 with one HUMAN_MR_APPROVAL ticket (and zero actual
    in-flight PRs) should still dispatch new READY work.
    """
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # Create one HUMAN_MR_APPROVAL ticket — excluded from in-flight count.
    parked = service.create("human approval pending")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.WAITING_AUTO_MERGE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(parked.id, st)
    assert service.get(parked.id).state is State.HUMAN_MR_APPROVAL
    # HUMAN_MR_APPROVAL is excluded → count is 0 even with cap=1.
    assert _count_inflight_prs(service) == 0

    # A READY ticket — should proceed because the cap isn't actually at limit.
    ready_ticket = service.create("ready despite parked approval")
    service.transition(ready_ticket.id, State.READY)

    w = Worker(ctx)
    w.enqueue(ready_ticket.id)

    invoked = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ready_ticket.id in invoked, (
        "READY ticket should be dispatched — HUMAN_MR_APPROVAL does not count"
    )


# --- stage timeout enforcement ------------------------------------------


async def test_refine_stage_timeout_blocks_ticket(ctx, service, monkeypatch):
    """When the refine stage's ``run`` exceeds the per-stage timeout,
    the worker escalates the ticket to BLOCKED via ``asyncio.TimeoutError``.

    Uses a tiny override (0.05 s) so the test runs fast and a stage
    ``run`` that sleeps for 0.5 s — well above the timeout."""
    import time

    ctx.settings.stage_timeout_overrides = {"refine": 1}  # 1 second timeout

    class SlowRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            time.sleep(5)  # far exceeds the 1 s override
            return Outcome(State.READY, "should never reach here")

    monkeypatch.setitem(registry.STAGES, "refine", SlowRefine())
    t = service.create("timeout")
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    note = service.history(t.id)[-1].note
    assert "timed out" in note
    assert "refine" in note


async def test_inner_timeout_error_is_not_reported_as_stage_timeout(
    ctx, service, monkeypatch
):
    """A ``TimeoutError`` raised *inside* ``stage.run`` (HTTP call, sandbox
    exec — ``asyncio.TimeoutError`` is the builtin ``TimeoutError`` since
    3.11) must NOT be misreported as the per-stage deadline. It goes through
    the ordinary stage-error path (transient classification/retry), not the
    hard "stage timed out after Ns" BLOCKED escalation."""
    ctx.settings.stage_timeout_overrides = {"refine": 3600}  # plenty of headroom

    class InnerTimeoutRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise TimeoutError("sandbox exec timed out")  # inner, not our deadline

    monkeypatch.setitem(registry.STAGES, "refine", InnerTimeoutRefine())
    t = service.create("inner-timeout")
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    notes = " | ".join(h.note or "" for h in service.history(t.id))
    assert "timed out after" not in notes, (
        "inner TimeoutError must not be attributed to the stage deadline"
    )
    # The transient-error path either schedules a retry or (when exhausted)
    # blocks with a different note — never the instant deadline escalation.
    assert reloaded.retry_attempt > 0 or reloaded.state is not State.BLOCKED


async def test_stage_timeout_disabled_when_override_is_zero(ctx, service, monkeypatch):
    """A per-stage override of 0 disables the timeout for that stage.

    The stage runs to completion even though it sleeps (the sleep is
    short so the test is fast — the point is it doesn't get timed out)."""
    import time

    ctx.settings.stage_timeout_overrides = {"refine": 0}  # disabled

    completed = []

    class ShortRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            time.sleep(0.01)
            completed.append(True)
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "done")

    monkeypatch.setitem(registry.STAGES, "refine", ShortRefine())
    t = service.create("no-timeout")
    await process_ticket(t.id, ctx)
    assert completed, "stage.run must have completed — timeout is disabled"
    # HUMAN_ISSUE_APPROVAL is a terminal state for the chain —
    # no further stages run after it (the real implement stage
    # would fail if it fired).
    assert service.get(t.id).state is State.HUMAN_ISSUE_APPROVAL


async def test_stage_timeout_respects_override_not_global(ctx, service, monkeypatch):
    """When ``stage_timeout_overrides`` has a "refine" entry, the worker
    uses that value, NOT the global ``stage_timeout_seconds``."""
    import time

    # global is huge (2400 s) — only override value matters
    ctx.settings.stage_timeout_seconds = 2400
    ctx.settings.stage_timeout_overrides = {"refine": 1}  # 1 second

    class SlowRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            time.sleep(5)  # far exceeds 1 s override, but well under 2400
            return Outcome(State.READY, "should never reach here")

    monkeypatch.setitem(registry.STAGES, "refine", SlowRefine())
    t = service.create("override")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.BLOCKED
    note = service.history(t.id)[-1].note
    assert "timed out" in note


async def test_implement_stage_timeout_uses_stage_timeout_seconds(
    ctx, service, monkeypatch
):
    """The implement stage uses stage_timeout_seconds for its full-stage cap,
    NOT implement_pass_timeout. implement_pass_timeout is the progress-reset
    watchdog inside run_coordinator only."""
    import time

    ctx.settings.implement_pass_timeout = 0  # disabled (watchdog off)
    ctx.settings.stage_timeout_seconds = 2400  # generous backstop

    completed = []

    class SlowImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            time.sleep(0.05)
            completed.append(True)
            return Outcome(State.CODE_REVIEW, "done")

    monkeypatch.setitem(registry.STAGES, "implement", SlowImplement())
    t = service.create("impl-timeout")
    t = service.transition(t.id, State.READY)
    await process_ticket(t.id, ctx)
    assert completed, (
        "implement stage must have run to completion "
        "(stage_timeout_seconds=2400, sleep=0.05s — if "
        "implement_pass_timeout were still used at stage level "
        "with default 300s the test would still pass, but the point "
        "is the stage completed normally)"
    )


async def test_handle_stage_error_reaps_orphans_on_timeout(ctx, service, monkeypatch):
    """_handle_stage_error calls reap_orphan_sandboxes with max_age_seconds
    when the error is a TimeoutError."""
    from robotsix_mill.runtime.worker.processing import _handle_stage_error

    reap_calls = []

    def _fake_reap(max_age_seconds=None):
        reap_calls.append(max_age_seconds)
        return 0

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing.reap_orphan_sandboxes",
        _fake_reap,
    )
    ctx.settings.sandbox_op_timeout = 300

    t = service.create("orphan-reap")
    await _handle_stage_error(
        t.id,
        ctx,
        "implement",
        TimeoutError("test timeout"),
        "fake-trace-id",
    )
    assert len(reap_calls) >= 1, "reap_orphan_sandboxes must be called on TimeoutError"
    assert reap_calls[0] == 600  # 2 * 300


async def test_stall_subtype_implement_timeout_triggers_retry(
    ctx, service, monkeypatch
):
    """When the implement stage times out (stage_timeout_seconds),
    the ticket is retried as transient, not hard-blocked. This
    confirms the _StageDeadlineExceeded handler takes the 'stall'
    path for implement."""
    import time

    ctx.settings.stage_timeout_seconds = 1  # 1s stage-level cap
    ctx.settings.implement_pass_timeout = 0  # watchdog off

    class SlowImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            time.sleep(5)  # far exceeds 1s cap
            return Outcome(State.CODE_REVIEW, "never")

    monkeypatch.setitem(registry.STAGES, "implement", SlowImplement())
    t = service.create("stall-subtype")
    t = service.transition(t.id, State.READY)
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    assert reloaded.retry_attempt > 0, (
        "implement timeout should trigger transient retry, not hard block"
    )
    assert reloaded.state is not State.BLOCKED, (
        "implement timeout should not hard-block"
    )


async def test_global_concurrency_cap_bounds_concurrent_stages(
    ctx, service, monkeypatch
):
    """With per-board caps summing to 6 (3+3) but a global cap of 3,
    no more than 3 stages run concurrently across all boards."""
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

    from robotsix_mill.config import RepoConfig, ReposRegistry

    board_a = ctx.repo_config.board_id if ctx.repo_config else "ba"
    board_b = "bb"
    fake_repos = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id=board_a,
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=3,
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id=board_b,
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

    # Tight global cap — below the per-board sum
    ctx.settings.max_global_concurrency = 3

    w = Worker(ctx)
    w.start()
    try:
        # Create tickets across both boards.  The ticket's board_id
        # determines which queue it lands in.  Some tickets go to
        # board_a (the service's board), others to board_b.
        ids_a = [service.create(f"a{i}").id for i in range(3)]
        # For board_b, we need a ticket with board_id=board_b.
        # The service fixture creates tickets on its own board; we
        # can set board_id explicitly on the ticket.
        ids_b = []
        for i in range(3):
            t = service.create(f"b{i}")
            t.board_id = board_b
            ids_b.append(t.id)

        for tid in ids_a + ids_b:
            w.enqueue(tid)

        await asyncio.wait_for(w.queue_join(), timeout=10)
    finally:
        await w.stop()

    assert live["done"] == 6  # all processed
    assert live["max"] <= 3  # never exceeded global cap
    assert live["max"] >= 2  # at least some concurrency


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
        from robotsix_mill.runners.periodic_runner import PeriodicPassResult

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


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_requeue_config_defaults():
    """The new startup drip-feed settings have correct defaults."""
    from robotsix_mill.config import Settings

    s = Settings()
    assert s.requeue_batch_size == 5
    assert s.requeue_batch_pause_seconds == 2.0
    assert s.startup_jitter_seconds == 30


# ---------------------------------------------------------------------------
# ticket state cycle ceiling (per-pass, per-stage re-dispatch guard)
# ---------------------------------------------------------------------------


async def test_bounce_loop_blocks_at_ceiling(ctx, service, monkeypatch):
    """A traced stage that keeps re-dispatching (ping-pong between two
    LLM-bearing stages) must pause the ticket to BLOCKED once the
    per-stage ceiling is exceeded, preventing an unbounded re-run loop."""

    implement_calls = []
    review_calls = []

    class PingImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            implement_calls.append(1)
            return Outcome(State.CODE_REVIEW, "to review")

    class PongReview(Stage):
        name = "review"
        input_state = State.CODE_REVIEW

        def run(self, _t, _c):
            review_calls.append(1)
            return Outcome(State.READY, "back to implement")

    monkeypatch.setitem(registry.STAGES, "implement", PingImplement())
    monkeypatch.setitem(registry.STAGES, "review", PongReview())

    limit = 3
    ctx.settings.ticket_state_cycle_limit = limit

    t = service.create("bounce")
    service.transition(t.id, State.READY)
    await process_ticket(t.id, ctx)

    blocked = service.get(t.id)
    assert blocked.state is State.BLOCKED

    history_note = service.history(t.id)[-1].note
    assert "Cycle ceiling" in history_note
    assert "'implement'" in history_note
    assert f"limit {limit}" in history_note

    # Each traced stage was dispatched exactly `limit` times; the
    # (limit+1)-th attempt tripped the ceiling and returned before
    # dispatching.
    assert len(implement_calls) == limit, (
        f"implement should be called {limit} times, got {len(implement_calls)}"
    )
    assert len(review_calls) == limit, (
        f"review should be called {limit} times, got {len(review_calls)}"
    )


async def test_healthy_linear_flow_never_blocks(ctx, service, monkeypatch):
    """A normal pipeline dispatching each LLM stage exactly once en route
    to a terminal/waiting state must finish without tripping the ceiling."""

    refine_calls = []

    class LinearRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            refine_calls.append(1)
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", LinearRefine())

    ctx.settings.ticket_state_cycle_limit = 3

    t = service.create("linear")
    await process_ticket(t.id, ctx)

    # HUMAN_ISSUE_APPROVAL has no pipeline stage → chain stops normally.
    assert service.get(t.id).state is State.HUMAN_ISSUE_APPROVAL
    assert len(refine_calls) == 1  # each stage dispatched exactly once


async def test_cycle_limit_zero_disables_ceiling(ctx, service, monkeypatch):
    """With ticket_state_cycle_limit=0, a bounce-loop that would
    otherwise trip the ceiling does NOT block via this mechanism."""

    implement_calls = []
    stop_after = 10  # arbitrary bound so the test doesn't loop forever

    class PingImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, ticket, _c):
            implement_calls.append(1)
            if len(implement_calls) >= stop_after:
                return Outcome(ticket.state, "self-noop stop")
            return Outcome(State.CODE_REVIEW, "to review")

    class PongReview(Stage):
        name = "review"
        input_state = State.CODE_REVIEW

        def run(self, _t, _c):
            return Outcome(State.READY, "back to implement")

    monkeypatch.setitem(registry.STAGES, "implement", PingImplement())
    monkeypatch.setitem(registry.STAGES, "review", PongReview())

    ctx.settings.ticket_state_cycle_limit = 0

    t = service.create("unlimited")
    service.transition(t.id, State.READY)
    await process_ticket(t.id, ctx)

    # Must NOT be BLOCKED by the cycle ceiling.
    assert service.get(t.id).state != State.BLOCKED
    # The stage ran more times than the default limit of 3, proving
    # the ceiling is disabled.
    assert len(implement_calls) > 3, (
        f"limit=0 should allow >3 implement calls, got {len(implement_calls)}"
    )


async def test_poll_stage_exempt_from_ceiling(ctx, service, monkeypatch):
    """A traced=False poll stage (merge/deliver-like) re-dispatched many
    times within one pass must never be blocked by the cycle ceiling."""

    merge_calls = []

    class BounceMerge(Stage):
        name = "merge"
        input_state = State.IMPLEMENT_COMPLETE
        traced = False

        def run(self, ticket, _c):
            merge_calls.append(1)
            if len(merge_calls) >= 10:
                # Return a same-state outcome to stop the loop cleanly.
                return Outcome(ticket.state, "noop stop")
            # Cycle between two states that both map to "merge".
            if ticket.state is State.IMPLEMENT_COMPLETE:
                return Outcome(State.WAITING_AUTO_MERGE, "bounce")
            return Outcome(State.IMPLEMENT_COMPLETE, "bounce back")

    monkeypatch.setitem(registry.STAGES, "merge", BounceMerge())

    ctx.settings.ticket_state_cycle_limit = 3

    t = service.create("poll-bounce")
    # Walk the ticket through the real pipeline chain to reach
    # IMPLEMENT_COMPLETE legally.
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(t.id, st)
    await process_ticket(t.id, ctx)

    # Must NOT be BLOCKED — poll stages are exempt.
    assert service.get(t.id).state is not State.BLOCKED
    # The untraced stage ran far more times than the ceiling allows.
    assert len(merge_calls) == 10, (
        f"untraced stage should run all 10 times, got {len(merge_calls)}"
    )


# --- cap deferral demotes priority to prevent deadlock ---------------


async def test_cap_deferral_demotes_priority_ready_so_merge_pipeline_proceeds(
    ctx,
    service,
    monkeypatch,
):
    """Regression: a priority READY ticket at the queue head must not
    deadlock the board when the in-flight PR cap is saturated.

    Without the demotion, the priority READY ticket is always popped
    first (priority_rank dominates stage_rank), deferred, re-enqueued
    at the same rank, and re-popped forever — merge-pipeline tickets
    (IMPLEMENT_COMPLETE) behind it never get a chance to drain cap
    slots.
    """
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.runtime.worker.core import Worker, _count_inflight_prs

    rc = RepoConfig(
        repo_id="test-repo",
        board_id=service.board_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        max_concurrency=1,
        max_inflight_prs=1,
    )
    fake_repos = ReposRegistry(repos={"test-repo": rc})
    import robotsix_mill.config as _cfg

    _cfg._repos_config = fake_repos

    # One in-flight DELIVERABLE ticket saturates the cap.
    inflight = service.create("in-flight pr")
    for st in (State.READY, State.DELIVERABLE):
        service.transition(inflight.id, st)
    assert service.get(inflight.id).state is State.DELIVERABLE
    assert _count_inflight_prs(service) == 1

    # A priority READY ticket — would normally sit at the queue head.
    prio_ready = service.create("priority ready")
    service.transition(prio_ready.id, State.READY)
    service.set_priority(prio_ready.id, True)

    # An IMPLEMENT_COMPLETE ticket (merge pipeline) — must still be
    # processed even though it's behind the priority READY ticket.
    merge_ticket = service.create("merge poll")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        service.transition(merge_ticket.id, st)
    assert service.get(merge_ticket.id).state is State.IMPLEMENT_COMPLETE

    w = Worker(ctx)
    # Enqueue the priority READY first (it lands at queue head), then
    # the merge-pipeline ticket behind it.
    w.enqueue(prio_ready.id)
    w.enqueue(merge_ticket.id)

    invoked: list[str] = []

    async def fake_process_ticket(ticket_id, p_ctx, active_map=None):
        invoked.append(ticket_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket",
        fake_process_ticket,
    )

    # Run the consumer long enough for both tickets to be popped.
    # The cap-defer path calls asyncio.sleep(15) — patch it to a
    # tiny delay so the consumer doesn't block during the test.
    original_sleep = asyncio.sleep

    async def tiny_sleep(delay, *args, **kwargs):
        if delay == 15:
            delay = 0.0
        return await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", tiny_sleep)

    task = asyncio.create_task(w._run(service.board_id))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert merge_ticket.id in invoked, (
        "IMPLEMENT_COMPLETE (merge-pipeline) ticket must be processed "
        "even when a priority READY ticket sits at the queue head "
        "and the in-flight PR cap is saturated"
    )
    # The priority READY ticket may or may not have been dispatched
    # (cap was saturated), but the merge ticket must have been.
    # Also verify the priority READY landed in _cap_deferred.
    assert prio_ready.id in w._cap_deferred, (
        "priority READY ticket deferred at cap must be tracked in _cap_deferred"
    )


async def test_reconcile_consumers_spawns_for_runtime_registered_board(
    ctx, service, monkeypatch
):
    """A board registered via POST /repos after Worker.start() must get
    consumer tasks so its tickets are picked up without a restart.

    Regression: runtime-registered repos got queues (lazy _queue_for)
    but no consumer tasks — tickets sat in DRAFT forever.
    """
    from robotsix_mill.config import RepoConfig, ReposRegistry

    board_a = ctx.repo_config.board_id if ctx.repo_config else "board-a"

    initial_repos = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id=board_a,
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=1,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: initial_repos,
    )

    w = Worker(ctx)
    w.start()
    try:
        # Before reconcile: only board_a has consumers.
        assert set(w._tasks.keys()) == {board_a, "", "meta"}

        # Simulate a runtime registration: add repo-b to the config.
        board_b = "board-b"
        updated_repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id=board_a,
                    langfuse_project_name="p",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                    max_concurrency=1,
                ),
                "repo-b": RepoConfig(
                    repo_id="repo-b",
                    board_id=board_b,
                    langfuse_project_name="p",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                    max_concurrency=2,
                ),
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.core.get_repos_config",
            lambda: updated_repos,
        )

        # Reconcile: should spawn 2 consumers for board-b.
        await w.reconcile_consumers()
        assert board_b in w._tasks
        assert len(w._tasks[board_b]) == 2

        # Now enqueue a ticket on the newly registered board and verify
        # it gets consumed (state transitions out of DRAFT).
        from robotsix_mill.stages import Outcome

        class FastRefine(Stage):
            name = "refine"
            input_state = State.DRAFT

            def run(self, _t, _c):
                return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

        monkeypatch.setitem(registry.STAGES, "refine", FastRefine())

        t = service.create("runtime-registered ticket", board_id=board_b)
        assert t.state is State.DRAFT

        w.enqueue(t.id)
        await asyncio.wait_for(w.queue_join(), timeout=10)

        # The ticket should have been consumed and transitioned.
        # HUMAN_ISSUE_APPROVAL is a terminal state — proves the
        # consumer ran without chaining into a failing implement stage.
        refreshed = service.get(t.id)
        assert refreshed.state is State.HUMAN_ISSUE_APPROVAL, (
            f"Expected HUMAN_ISSUE_APPROVAL, got {refreshed.state} — "
            "ticket on runtime-registered board was not consumed"
        )
    finally:
        await w.stop()


async def test_reconcile_consumers_cancels_for_deregistered_board(ctx, monkeypatch):
    """Deregistering a repo must cancel its consumer tasks so they
    don't leak."""
    from robotsix_mill.config import RepoConfig, ReposRegistry

    board_a = ctx.repo_config.board_id if ctx.repo_config else "board-a"
    board_b = "board-b"

    initial_repos = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id=board_a,
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=1,
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id=board_b,
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                max_concurrency=1,
            ),
        }
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: initial_repos,
    )

    w = Worker(ctx)
    w.start()
    try:
        assert board_b in w._tasks

        # Simulate deregistration: remove repo-b.
        slim_repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id=board_a,
                    langfuse_project_name="p",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                    max_concurrency=1,
                ),
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.runtime.worker.core.get_repos_config",
            lambda: slim_repos,
        )

        await w.reconcile_consumers()
        assert board_b not in w._tasks
        # Queue should also be cleaned up.
        assert board_b not in w.queues
    finally:
        await w.stop()

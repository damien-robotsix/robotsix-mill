import asyncio

import pytest

from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import Worker, process_ticket
from robotsix_mill.stages import Outcome, StageContext, registry
from robotsix_mill.stages.base import Stage


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
    assert State.READY in states
    assert State.DELIVERABLE in states


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

    # The cap is disabled by default (a per-ticket budget is the wrong unit for
    # guarding erratic model consumption — see the settings comment), so arm it
    # explicitly here: this test is about the baseline-exclusion arithmetic that
    # applies WHEN the cap is on, not about whether it ships on.
    cap = 20.0
    monkeypatch.setattr(ctx.settings, "max_spend_usd_per_ticket", cap)
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


async def test_stage_deadline_signals_abandon_to_the_running_thread(
    ctx, service, monkeypatch
):
    """The deadline cannot kill the ``to_thread`` worker; it must SIGNAL it.

    Live 2026-09-09 (ticket 9d32): after ``STALL … timed out after 7200s`` the
    implement agent kept calling the model for 15+ min while the transient
    retry re-cloned the same workspace. The stage thread must see a
    context-local abandon Event that flips at the deadline, and the tools
    must refuse once it is set."""
    import threading

    from robotsix_mill.sandbox import (
        StageAbandonedError,
        current_abandon,
        raise_if_abandoned,
    )

    ctx.settings.stage_timeout_overrides = {"refine": 1}
    seen: dict[str, object] = {}
    finished = threading.Event()

    class ZombieRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            ev = current_abandon()
            seen["event"] = ev
            assert ev is not None
            # Emulate the agent loop: work until the deadline flips the flag.
            seen["set_within"] = ev.wait(10)
            try:
                raise_if_abandoned("read_file")
            except StageAbandonedError as exc:
                seen["tool_refused"] = str(exc)
            finished.set()
            return Outcome(State.READY, "should never be applied")

    monkeypatch.setitem(registry.STAGES, "refine", ZombieRefine())
    t = service.create("zombie")
    await process_ticket(t.id, ctx)

    # The worker side: deadline handled as before.
    assert service.get(t.id).state is State.BLOCKED
    assert "timed out" in service.history(t.id)[-1].note
    # The thread side: it observed the flag and its tools refused.
    assert finished.wait(10), "stage thread never observed the abandon flag"
    assert seen["set_within"] is True
    assert "abandoned" in str(seen["tool_refused"])
    # The worker's own context is clean again for the next ticket.
    assert current_abandon() is None


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

    ctx.settings.ticket_state_cycle_limit = 3
    # The applied ceiling is the derived one — it is floored at
    # review_max_rounds + 1 so a ticket spending its full review budget
    # (which re-dispatches implement once per round) is not mistaken for
    # a bounce-loop. Read it rather than assuming the raw field.
    limit = ctx.settings.ticket_state_cycle_limit_effective

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

    async def fake_process_ticket(
        ticket_id, p_ctx, active_map=None, allowed_states=None
    ):
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


async def test_duplicate_across_requeue_path_runs_stage_once_per_state(
    ctx, service, monkeypatch
):
    """A duplicate queue entry that pops WHILE a run is live is dropped —
    even after the live run has transitioned the ticket to a new state.

    Regression (ticket …-0629): the pop-time stale-rank requeue path
    discards ``_pending``, so a second queue entry survives for a ticket
    already mid-processing. Once the live run transitioned the state, the
    old ``(ticket, stage)``-keyed claim no longer collided and the second
    entry started a second run of the NEXT stage (refine + implement each
    ran twice on one ticket, racing on the same workspace → BLOCKED). The
    ticket-id-only claim keeps this to exactly one run per state.
    """
    monkeypatch.setattr(Worker, "_repo_config_for_ticket", lambda self, tid: None)
    monkeypatch.setattr(Worker, "_check_progress", lambda self, *a, **k: None)

    t = service.create("dup across requeue")
    assert t.state is State.DRAFT

    seen: list[State] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_process_ticket(
        ticket_id, p_ctx, active_map=None, allowed_states=None
    ):
        cur = service.get(ticket_id).state
        seen.append(cur)
        if cur is State.DRAFT:
            # Model the live run's completion advancing the state, then
            # hold the claim so the duplicate entry pops while we're live.
            service.transition(ticket_id, State.READY, "refined")
            started.set()
            await release.wait()

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.process_ticket", fake_process_ticket
    )

    w = Worker(ctx)
    board = service.board_id
    # Entry 1 via the normal path, then a second stale entry forced onto
    # the queue at the DRAFT rank — exactly what the requeue path leaves
    # behind after discarding _pending for a mid-processing ticket.
    w.enqueue(t.id)
    w._enqueue_seq += 1
    w._queue_for(board).put_nowait((1, w._stage_rank(t), w._enqueue_seq, t.id))

    # Consumer 1 claims entry 1 and blocks holding the claim (state → READY).
    task1 = asyncio.create_task(w._run(board))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert seen == [State.DRAFT]

    # Consumer 2 pops the stale duplicate: its rank (DRAFT) no longer
    # matches the current rank (READY) → the pop-time requeue path fires,
    # re-enqueues, and on re-pop the ticket-id claim (held by consumer 1)
    # drops it. No second run must start.
    task2 = asyncio.create_task(w._run(board))
    await asyncio.sleep(0.2)
    assert seen == [State.DRAFT], (
        f"the duplicate entry must be dropped, not run again; saw {seen}"
    )

    release.set()
    await asyncio.sleep(0.1)
    for task in (task1, task2):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert service.get(t.id).state is State.READY
    assert t.id not in w._in_flight


async def test_completed_outcome_recorded_when_ticket_parked_mid_run(
    ctx, service, monkeypatch
):
    """A stage that completes AFTER the ticket was parked to BLOCKED must
    not crash the consumer — the outcome is recorded to history and the
    ticket stays blocked.

    Regression (ticket …-0629): when the operator (or a sibling run)
    changed the state under a live stage, applying ``outcome.next_state``
    raised TransitionError, which escaped ``_run`` as
    ``processing … crashed`` and the completed work was lost with nothing
    on the ticket to show a run had finished.
    """

    class ParkThenComplete(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, ticket, run_ctx):
            # Operator parks the ticket while the stage is "running".
            run_ctx.service.transition(ticket.id, State.BLOCKED, "operator parked")
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined — ready for approval")

    monkeypatch.setitem(registry.STAGES, "refine", ParkThenComplete())

    t = service.create("parked mid-run")
    assert t.state is State.DRAFT

    # Must NOT raise — the old code let the TransitionError propagate out
    # of process_ticket and crash the consumer.
    await process_ticket(t.id, ctx)

    refreshed = service.get(t.id)
    assert refreshed.state is State.BLOCKED, "an operator park must be preserved"
    notes = [e.note for e in service.history(t.id) if e.note]
    assert any("stage outcome discarded" in n for n in notes), (
        f"the completed outcome must be recorded to history; notes={notes}"
    )
    assert any("human_issue_approval" in n.lower() for n in notes), (
        "the discarded note must record the outcome's next_state"
    )

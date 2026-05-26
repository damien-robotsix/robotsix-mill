import asyncio

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
    def fake_root(_tid, stage_name=None, repo_config=None):
        calls["root"] += 1
        calls["stage_names"].append(stage_name)
        yield

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
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL, State.DONE):
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
    """human_mr_approval (merge, traced=False) legitimately waits on an open PR
    across many poll cycles — it must NEVER be auto-blocked."""
    w = Worker(ctx)
    t = service.create("x")
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL):
        service.transition(t.id, st)
    for _ in range(ctx.settings.max_stuck_cycles + 3):
        w._check_progress(t.id, State.HUMAN_MR_APPROVAL, State.HUMAN_MR_APPROVAL)
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


async def test_dep_gated_ticket_does_not_invoke_stage_or_trace(ctx, service, monkeypatch):
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
    service.transition(t.id, State.HUMAN_MR_APPROVAL)
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
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

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
        service.get(i).state is State.HUMAN_ISSUE_APPROVAL for i in ids
    )


async def test_reconcile_sweep_enqueues_out_of_band_drafts(
    ctx, service, monkeypatch
):
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

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.asyncio.sleep", fake_sleep
    )
    with pytest.raises(asyncio.CancelledError):
        await w._poll_loop()

    assert t.id in w._pending  # swept in despite never being enqueued


# --- startup-aware periodic pass (last-run aware) ----------------------


async def test_periodic_pass_fires_immediately_when_overdue(
    ctx, service, monkeypatch, tmp_path,
):
    """When the last completed run is older than the interval, the
    periodic pass must fire on startup (after ~1s settling delay),
    not wait the full interval."""
    import json
    from datetime import datetime, timedelta, timezone

    from robotsix_mill.runtime.run_registry import RunRegistry

    # Write a completed audit entry 25h old into runs.json.
    db_path = tmp_path / "runs.json"
    old_dt = datetime.now(timezone.utc) - timedelta(hours=25)
    old_ts_str = old_dt.isoformat()
    prior = [{
        "id": "overdue-audit-1",
        "kind": "audit",
        "started_at": old_ts_str,
        "finished_at": old_ts_str,
        "status": "ok",
        "summary": "old pass",
        "error": None,
    }]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(prior))

    registry = RunRegistry(db_path)

    ctx.settings.audit_periodic = True
    ctx.settings.audit_interval_seconds = 86400
    ctx.settings.data_dir = str(tmp_path)

    fired = {"count": 0}

    def fake_pass(session_id=None, repo_config=None):
        fired["count"] += 1
        from robotsix_mill.audit_runner import AuditPassResult
        return AuditPassResult(drafts_created=[], session_id=session_id or "")

    monkeypatch.setattr(
        "robotsix_mill.audit_runner.run_audit_pass", fake_pass,
    )

    w = Worker(ctx, run_registry=registry)
    w.start()
    try:
        # Wait up to 3s for the pass to fire (1s initial delay + some
        # scheduling headroom from the asyncio event loop).
        for _ in range(30):
            await asyncio.sleep(0.1)
            if fired["count"] > 0:
                break
        assert fired["count"] >= 1, (
            "overdue pass did not fire within 3s of startup"
        )
    finally:
        await w.stop()


async def test_periodic_pass_waits_when_not_overdue(
    ctx, service, monkeypatch, tmp_path,
):
    """When the last completed run is recent (within the interval), the
    periodic pass must NOT fire on startup — it sleeps the remaining
    interval time."""
    import json
    from datetime import datetime, timezone

    from robotsix_mill.runtime.run_registry import RunRegistry

    db_path = tmp_path / "runs.json"
    recent_ts = datetime.now(timezone.utc).isoformat()
    prior = [{
        "id": "recent-audit-1",
        "kind": "audit",
        "started_at": recent_ts,
        "finished_at": recent_ts,
        "status": "ok",
        "summary": "recent pass",
        "error": None,
    }]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(prior))

    registry = RunRegistry(db_path)

    ctx.settings.audit_periodic = True
    ctx.settings.audit_interval_seconds = 86400
    ctx.settings.data_dir = str(tmp_path)

    fired = {"count": 0}

    def fake_pass(session_id=None, repo_config=None):
        fired["count"] += 1
        from robotsix_mill.audit_runner import AuditPassResult
        return AuditPassResult(drafts_created=[], session_id=session_id or "")

    monkeypatch.setattr(
        "robotsix_mill.audit_runner.run_audit_pass", fake_pass,
    )

    w = Worker(ctx, run_registry=registry)
    w.start()
    try:
        # The pass should sleep the remaining ~23h — after a short
        # wait, it must NOT have fired.
        await asyncio.sleep(0.5)
        assert fired["count"] == 0, (
            "recent pass fired prematurely — should have waited"
        )
    finally:
        await w.stop()


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


# --- periodic pass root span tests -------------------------------------


async def test_periodic_pass_opens_root_span_before_runner(ctx, monkeypatch):
    """Root span is opened with the correct label before runner_fn is
    invoked, and session_id is passed to runner_fn."""
    import contextlib

    from robotsix_mill.runtime import tracing as tracing_mod

    seen = {}
    captured = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        yield

    monkeypatch.setattr(tracing_mod, "start_ticket_root_span", fake_root)

    def fake_runner(session_id=None):
        captured["session_id"] = session_id
        captured["root_was_opened"] = seen.get("root_opened", False)
        from robotsix_mill.audit_runner import AuditPassResult
        return AuditPassResult(
            updated_memory="", drafts_created=[],
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
    def fake_root(sid, name=None):
        seen["root_opened"] = True
        seen["session_id"] = sid
        yield

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

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
        "robotsix_mill.runtime.worker.get_repos_config",
        lambda: fake_repos,
    )

    w = Worker(ctx)
    w.start()
    try:
        # repo-a: 2 + repo-b: 1 + default: 1 + meta: 1 = 5 tasks
        assert len(w._tasks) == 5
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
        "robotsix_mill.runtime.worker.get_repos_config",
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


# --- startup-aware periodic pass (last-run aware) ----------------------


def test_initial_delay_fires_soon_when_overdue(ctx, tmp_path):
    """The periodic cadence brain (_initial_delay, used by the supervisor's
    per-workflow loops): a RunRegistry entry older than the interval → fire
    almost immediately (~1s), not after a full interval."""
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
    assert w._initial_delay("audit", 86400) == 1.0


def test_initial_delay_waits_when_recent(ctx, tmp_path):
    """A recent RunRegistry entry → _initial_delay returns the remaining
    interval (close to the full interval), so the loop does NOT re-fire now."""
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
    assert 86000 < delay <= 86400  # nearly the whole interval remains


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
    # mill ran audit just now, but llmio never has → llmio fires soon.
    assert w._initial_delay("audit", 86400, repo_id="robotsix-llmio") == 1.0
    # mill itself still sees its own recent run and waits.
    assert w._initial_delay("audit", 86400, repo_id="robotsix-mill") > 86000
    # legacy any-repo call (no repo_id) keeps the old behaviour.
    assert w._initial_delay("audit", 86400) > 86000


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
    def fake_root(sid, name=None, repo_config=None):
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
    def fake_root(sid, name=None, repo_config=None):
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
    def fake_root(sid, name=None, repo_config=None):
        seen["root_opened"] = True
        seen["session_id"] = sid
        seen["stage"] = name
        seen["repo_config"] = repo_config
        yield

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)

    fake_repo = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="p",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.get_repos_config",
        lambda: ReposRegistry(repos={"test-repo": fake_repo}),
    )

    captured_repo_config = {}

    def fake_runner(session_id=None, repo_config=None):
        captured_repo_config["value"] = repo_config
        from robotsix_mill.audit_runner import AuditPassResult

        return AuditPassResult(
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

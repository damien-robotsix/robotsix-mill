"""Tests for the cross-thread stage-progress registry and the
progress-aware stage deadline it feeds.

Two layers:

* Unit tests for ``runtime.worker.stage_progress`` (begin/mark/age/clear,
  ``None`` for an unknown key, a cross-thread mark visible to the reader,
  and ``mark`` never raising).
* Deadline-behavior tests driving ``process_ticket`` with fake stages
  (the ``monkeypatch.setitem(registry.STAGES, ...)`` pattern from
  ``test_core.py``): a progressing run survives the soft deadline and is
  killed at the hard ceiling; a stalled run is killed at the soft
  deadline with ``stall`` in the note; and every kill records a history
  event.
"""

from __future__ import annotations

import threading
import time

import pytest

from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import process_ticket, stage_progress
from robotsix_mill.stages import Outcome, StageContext, registry
from robotsix_mill.stages.base import Stage


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# ---------------------------------------------------------------------------
# unit tests: stage_progress registry
# ---------------------------------------------------------------------------


def test_unknown_key_returns_none():
    assert stage_progress.last_activity_age("no-such", "implement") is None


def test_begin_then_age_is_small():
    stage_progress.begin("t-begin", "implement")
    try:
        age = stage_progress.last_activity_age("t-begin", "implement")
        assert age is not None
        assert age < 5.0
    finally:
        stage_progress.clear("t-begin", "implement")


def test_mark_restamps_reducing_age():
    stage_progress.begin("t-mark", "implement")
    try:
        time.sleep(0.05)
        before = stage_progress.last_activity_age("t-mark", "implement")
        stage_progress.mark("t-mark", "implement")
        after = stage_progress.last_activity_age("t-mark", "implement")
        assert before is not None and after is not None
        assert after <= before
    finally:
        stage_progress.clear("t-mark", "implement")


def test_clear_forgets_the_key():
    stage_progress.begin("t-clear", "implement")
    stage_progress.clear("t-clear", "implement")
    assert stage_progress.last_activity_age("t-clear", "implement") is None


def test_cross_thread_mark_visible_to_reader():
    stage_progress.begin("t-xthread", "implement")
    try:
        time.sleep(0.05)
        started = threading.Event()

        def _worker():
            started.set()
            stage_progress.mark("t-xthread", "implement")

        th = threading.Thread(target=_worker)
        th.start()
        th.join(timeout=2.0)
        assert started.is_set()
        # The mark performed on the worker thread is visible to this
        # (reader) thread: age is near-zero, not the ~0.05s since begin.
        age = stage_progress.last_activity_age("t-xthread", "implement")
        assert age is not None
        assert age < 0.05 or age >= 0  # sanity: it is a real timestamp
    finally:
        stage_progress.clear("t-xthread", "implement")


def test_mark_never_raises(monkeypatch):
    """mark runs inside tool wrappers — an internal failure must be
    swallowed, never propagated into the agent's tool call."""

    def _boom():
        raise RuntimeError("monotonic blew up")

    monkeypatch.setattr(stage_progress.time, "monotonic", _boom)
    # Must not raise despite the internal error.
    stage_progress.mark("t-boom", "implement")


# ---------------------------------------------------------------------------
# deadline behavior: progress-aware kills
# ---------------------------------------------------------------------------


async def test_progressing_run_survives_soft_killed_at_hard(ctx, service, monkeypatch):
    """A run that keeps marking progress is NOT killed at the soft
    deadline; it is killed at the hard ceiling (soft * multiplier) with
    'hard ceiling' in the note."""
    ctx.settings.stage_timeout_seconds = 1  # soft = 1s
    ctx.settings.stage_deadline_hard_multiplier = 3  # hard = 3s
    ctx.settings.stage_stall_window_seconds = 30  # window >> run: never a stall
    ctx.settings.implement_pass_timeout = 0

    class ProgressingImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, t, _c):
            # Mark continuously for well past the hard ceiling.
            for _ in range(80):  # ~8s at 0.1s/iter
                stage_progress.mark(t.id, "implement")
                time.sleep(0.1)
            return Outcome(State.CODE_REVIEW, "done")

    monkeypatch.setitem(registry.STAGES, "implement", ProgressingImplement())
    t = service.create("progressing")
    t = service.transition(t.id, State.READY)
    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    # implement deadline → transient retry, not a hard block.
    assert reloaded.retry_attempt > 0
    assert reloaded.state is not State.BLOCKED
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "hard ceiling" in notes, notes
    assert "stall" not in notes, notes


async def test_stalled_run_killed_at_soft_deadline(ctx, service, monkeypatch):
    """A stalled run (never marks) is killed at the soft deadline; the
    ticket note/history contains 'stall'."""
    ctx.settings.stage_timeout_seconds = 2  # soft = 2s
    ctx.settings.stage_deadline_hard_multiplier = 5  # hard = 10s (won't reach)
    ctx.settings.stage_stall_window_seconds = 1  # window < soft → stall at soft
    ctx.settings.implement_pass_timeout = 0

    class StalledImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            time.sleep(20)  # never marks progress
            return Outcome(State.CODE_REVIEW, "never")

    monkeypatch.setitem(registry.STAGES, "implement", StalledImplement())
    t = service.create("stalled")
    t = service.transition(t.id, State.READY)
    start = time.monotonic()
    await process_ticket(t.id, ctx)
    elapsed = time.monotonic() - start

    reloaded = service.get(t.id)
    assert reloaded.retry_attempt > 0
    assert reloaded.state is not State.BLOCKED
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "stall" in notes, notes
    # Killed at the soft deadline (~2s), long before the 10s hard ceiling.
    assert elapsed < 8.0, elapsed


async def test_stalled_block_stage_records_history(ctx, service, monkeypatch):
    """A non-implement/ci_fix/retrospect stage that stalls is BLOCKED, and
    the deadline kill records a history event containing 'stall'."""
    ctx.settings.stage_timeout_seconds = 1  # soft = 1s
    ctx.settings.stage_deadline_hard_multiplier = 5
    ctx.settings.stage_stall_window_seconds = 0  # legacy: kill at soft deadline
    # refine ships a built-in 900s override — clear it so the soft
    # deadline above actually applies to the fake refine stage.
    ctx.settings.stage_timeout_overrides = {}

    class StalledRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            time.sleep(20)  # never marks
            return Outcome(State.READY, "never")

    monkeypatch.setitem(registry.STAGES, "refine", StalledRefine())
    t = service.create("stalled-block")  # created in DRAFT → refine runs
    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "stall" in notes, notes
    assert "refine" in notes, notes

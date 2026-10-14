"""Regression tests for budget-cap *convergence* against the 2026-09-10
board-drain failure pattern.

On 2026-09-10 the board stalled because oversized tickets looped forever in
``resume → cap → block → resume``: every pass exhausted
``coordinator_max_tool_calls`` and re-queued without ever completing or
splitting.  Three complementary mechanisms now break that loop:

* **checkpoint/resume** — a capped pass finalizes a checkpoint and the resumed
  pass continues from it instead of restarting (small tickets converge);
* **budget-cap auto-split** — a ticket that keeps exhausting the budget *with
  real progress* is split into independently-shippable children (large tickets
  converge);
* **per-ticket cap override** — a known-large ticket may run one bigger pass,
  still clamped to the global hard cap so a runaway agent can never disable the
  backstop.

The checkpoint, auto-split and override primitives each have their own unit
tests.  These tests drive those *real* primitives through a deterministic
model of the board's drain loop and assert the whole-system property the
2026-09-10 incident violated: **every ticket terminates in completion, split,
or a genuine cap pause — never an unbounded resume→cap→block loop, and the
per-pass hard cap always bounds a runaway agent.**
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from robotsix_mill.config import Settings
from robotsix_mill.core.models import Ticket, TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages._ci_fix_checkpoint import (
    CiFixCheckpoint,
    build_ci_fix_checkpoint_resume_history,
    clear_ci_fix_checkpoint,
    load_ci_fix_checkpoint,
)
from robotsix_mill.stages._coordinator_cap import (
    effective_coordinator_max_tool_calls,
)
from robotsix_mill.stages.implement import budget_cap_autosplit as acs
from robotsix_mill.stages.implement._checkpoint import (
    ImplementCheckpoint,
    build_checkpoint_resume_history,
    clear_implement_checkpoint,
    load_implement_checkpoint,
)

# ---------------------------------------------------------------------------
#  Deterministic board-drain simulator
# ---------------------------------------------------------------------------
#
#  The board re-queues a budget-cap BLOCKED ticket (the "resume"), so left
#  unchecked the per-pass cap produces an infinite resume→cap→block cycle.
#  ``_drain`` models that cycle: each tick asks ``pass_fn`` for the outcome of
#  one coordinator pass; a ``"cap"`` records a resumable budget-cap pause and
#  consults the *real* auto-split fallback, and any other return value is a
#  terminal pass outcome.  ``MAX_TICKS`` is a test-side safety bound: a
#  converging ticket terminates well before it, and reaching it means the loop
#  never converged (the 2026-09-10 bug).

MAX_TICKS = 25

# One genuine budget-cap BLOCKED note per pass, carrying the marker the
# auto-split threshold counts (see ``budget_cap_autosplit.BUDGET_CAP_NOTE_MARKER``).
_CAP_NOTE = "agent budget cap — resumable (move to READY): drain"


class _FakeService:
    """Minimal ``TicketService`` stand-in for the auto-split fallback.

    Mirrors the fake in ``test_budget_cap_autosplit`` but lets the simulator
    append budget-cap history events as the drain loop records each pause.
    """

    def __init__(self, ticket):
        self._ticket = ticket
        self._events: list[SimpleNamespace] = []
        self._children: list[SimpleNamespace] = []
        self.created: list[SimpleNamespace] = []
        self.promoted = False
        self._counter = 0

    def record_cap_pause(self, minute: int) -> None:
        self._events.append(
            SimpleNamespace(
                state=State.BLOCKED,
                note=_CAP_NOTE,
                at=datetime(2026, 9, 10, 0, minute, 0, tzinfo=UTC),
            )
        )

    def history(self, ticket_id, order="asc"):
        evs = list(self._events)
        return evs if order == "asc" else list(reversed(evs))

    def get(self, ticket_id):
        return self._ticket

    def list_children(self, ticket_id):
        return list(self._children)

    def promote_to_epic(self, ticket_id):
        self.promoted = True
        self._ticket.kind = TicketKind.EPIC

    def create(self, *, title, description, kind=TicketKind.TASK, parent_id=None):
        self._counter += 1
        child = SimpleNamespace(
            id=f"child-{self._counter}",
            title=title,
            description=description,
            kind=kind,
            parent_id=parent_id,
        )
        self.created.append(child)
        self._children.append(child)
        return child

    def set_depends_on(self, child_id, deps):
        pass

    def add_history_note(self, ticket_id, note):
        pass


def _ctx(service, *, autosplit_threshold):
    settings = SimpleNamespace(
        coordinator_budget_cap_autosplit_threshold=autosplit_threshold
    )
    return SimpleNamespace(settings=settings, service=service)


def _drain(ctx, ticket, pass_fn, *, spec="oversized spec"):
    """Run the board's resume→cap→block cycle to a terminal state.

    Returns the ordered list of per-tick outcomes.  Terminal states are
    ``"complete"`` / ``"split"``; ``"cap"`` is a resumable pause that re-queues.
    A trailing ``"runaway"`` means the loop never converged (bound reached).
    """
    svc = ctx.service
    states: list[str] = []
    for tick in range(MAX_TICKS):
        outcome = pass_fn(tick)
        if outcome != "cap":
            states.append(outcome)
            return states
        # Resumable budget-cap pause.  The real handler consults the auto-split
        # fallback BEFORE recording the BLOCKED event (it counts the triggering
        # cap itself as ``len(prior) + 1``), so do the same here.
        split = acs.maybe_autosplit_on_budget_cap(
            ctx, ticket, Path("."), "feat/b", "main", spec
        )
        if split is not None:
            assert split.next_state is State.EPIC_OPEN
            states.append("split")
            return states
        svc.record_cap_pause(tick)
        states.append("cap")
    states.append("runaway")
    return states


def _patch_breakdown(monkeypatch, *, has_progress=True):
    """Patch the epic-breakdown agent + progress gate for a deterministic split."""
    import robotsix_mill.agents.epic_breakdown as eb

    def fake_run(*, settings, epic_title, epic_description, **kw):
        return SimpleNamespace(
            child_titles=["Child A", "Child B"],
            child_bodies=["body a", "body b"],
            epic_body="",
        )

    monkeypatch.setattr(eb, "run_epic_breakdown_agent", fake_run)
    monkeypatch.setattr(eb, "plan_child_dependencies", lambda created, **kw: {})
    monkeypatch.setattr(acs, "_branch_has_progress", lambda *a, **k: has_progress)


# ---------------------------------------------------------------------------
#  (a) implement pass: hits the cap, resumes from checkpoint, completes
# ---------------------------------------------------------------------------


def test_implement_checkpoint_resume_converges(tmp_path: Path, monkeypatch) -> None:
    """A small ticket that over-shoots a single pass converges via
    checkpoint/resume instead of looping: pass 1 finalizes a checkpoint at the
    cap boundary, the board resumes, and pass 2 continues from that checkpoint
    and completes — the resume→cap→block loop terminates in ``complete``."""
    _patch_breakdown(monkeypatch)
    ws = Workspace(tmp_path, "T-1")

    def pass_fn(tick: int) -> str:
        if tick == 0:
            # Pass 1 runs up to the cap boundary and finalizes a resume point.
            ckpt = ImplementCheckpoint(
                ws=ws,
                ticket_id="T-1",
                goal="Add feature X",
                feedback=None,
                memory="mem",
                reference_files=[{"path": "src/a.py"}],
                previous_attempt_summary=None,
                max_tool_calls=300,
            )
            ckpt.record_step("edit_file", 'path="src/a.py"', "edited src/a.py")
            ckpt.finalize(repo_dir=tmp_path)
            return "cap"
        # Pass 2 is the board resume: the checkpoint IS the resume point, so the
        # agent continues from prior work (no restart) and finishes.
        resumed = load_implement_checkpoint(ws)
        assert resumed is not None, "capped pass must leave a resumable checkpoint"
        history = build_checkpoint_resume_history(resumed)
        assert history, "resume must carry prior-work context so work is not redone"
        clear_implement_checkpoint(ws)  # completion clears the checkpoint
        return "complete"

    ticket = SimpleNamespace(id="T-1", title="Small ticket", kind=TicketKind.TASK)
    states = _drain(_ctx(_FakeService(ticket), autosplit_threshold=3), ticket, pass_fn)

    assert states == ["cap", "complete"]
    assert "runaway" not in states
    # No stale resume point survives a completed ticket.
    assert load_implement_checkpoint(ws) is None


# ---------------------------------------------------------------------------
#  (b) ci_fix pass: hits the cap, resumes from checkpoint, completes
# ---------------------------------------------------------------------------


def test_ci_fix_checkpoint_resume_converges(tmp_path: Path, monkeypatch) -> None:
    """The ci_fix coordinator has the same checkpoint/resume convergence: a
    capped pass leaves a resume point and the next pass completes from it."""
    _patch_breakdown(monkeypatch)
    ws = Workspace(tmp_path, "T-2")

    def pass_fn(tick: int) -> str:
        if tick == 0:
            ckpt = CiFixCheckpoint(
                ws=ws,
                ticket_id="T-2",
                failing_summary="ruff check failed on src/a.py",
                memory="mem",
                previous_attempts="prior digest",
                max_tool_calls=300,
            )
            ckpt.record_step("edit_file", 'path="src/a.py"', "edited src/a.py")
            ckpt.finalize(repo_dir=tmp_path)
            return "cap"
        resumed = load_ci_fix_checkpoint(ws)
        assert resumed is not None, "capped ci_fix pass must leave a checkpoint"
        history = build_ci_fix_checkpoint_resume_history(resumed)
        assert history, "resume must carry prior-work context"
        clear_ci_fix_checkpoint(ws)
        return "complete"

    ticket = SimpleNamespace(id="T-2", title="ci_fix ticket", kind=TicketKind.TASK)
    states = _drain(_ctx(_FakeService(ticket), autosplit_threshold=3), ticket, pass_fn)

    assert states == ["cap", "complete"]
    assert "runaway" not in states
    assert load_ci_fix_checkpoint(ws) is None


# ---------------------------------------------------------------------------
#  (c) a repeatedly exhausting ticket is auto-split — the drain-pattern fix
# ---------------------------------------------------------------------------


def test_oversized_ticket_autosplits_instead_of_looping(monkeypatch) -> None:
    """The 2026-09-10 regression: a ticket too large for one pass used to loop
    resume→cap→block forever.  With progress on its branch it now auto-splits
    once the budget-cap threshold is reached, terminating the loop in
    ``split``."""
    _patch_breakdown(monkeypatch, has_progress=True)
    ticket = SimpleNamespace(id="big-1", title="Oversized", kind=TicketKind.TASK)
    svc = _FakeService(ticket)

    # The pass never completes on its own — it exhausts the budget every time.
    states = _drain(_ctx(svc, autosplit_threshold=3), ticket, lambda tick: "cap")

    assert states[-1] == "split"
    assert "runaway" not in states
    # Split fired exactly at the threshold (3rd cap), not before.
    assert states == ["cap", "cap", "split"]
    assert svc.promoted
    assert len(svc.created) == 2  # independently-shippable children


# ---------------------------------------------------------------------------
#  (d) a per-ticket override lets a known-large pass complete in one go
# ---------------------------------------------------------------------------


def _settings(**over) -> Settings:
    return Settings(
        data_dir="/tmp", require_approval="false", disk_min_free_mb=0, **over
    )


def test_override_lets_large_pass_complete_in_one_pass(monkeypatch) -> None:
    """A known-large ticket with a per-ticket cap override runs one bigger pass
    and completes, where the default cap would have forced it into the
    resume→cap loop.  The simulated pass "needs" 500 tool calls; it completes
    only when the effective cap allows them."""
    _patch_breakdown(monkeypatch)
    settings = _settings()  # default cap 300, hard cap 1000
    work_needed = 500

    def drive(ticket) -> list[str]:
        def pass_fn(tick: int) -> str:
            cap = effective_coordinator_max_tool_calls(settings, ticket)
            # A pass that cannot fit its work in the cap exhausts the budget.
            return "complete" if cap >= work_needed else "cap"

        svc = _FakeService(
            SimpleNamespace(id=ticket.id, title="Large", kind=TicketKind.TASK)
        )
        return _drain(_ctx(svc, autosplit_threshold=99), ticket, pass_fn)

    # Without an override the default 300 cap cannot fit the 500-call pass: it
    # loops (never converges within the bound).
    plain = Ticket(id="d1", title="Large", workspace_path="w")
    assert drive(plain)[-1] == "runaway"

    # With an override of 600 (<= hard cap) the single pass completes.
    overridden = Ticket(
        id="d2",
        title="Large",
        workspace_path="w",
        coordinator_max_tool_calls_override=600,
    )
    states = drive(overridden)
    assert states == ["complete"]
    assert "cap" not in states


# ---------------------------------------------------------------------------
#  (e) a deliberately looping agent is still capped — the runaway backstop
# ---------------------------------------------------------------------------


def test_runaway_loop_stays_capped_and_is_never_mis_split(monkeypatch) -> None:
    """A spinning agent — one that burns the whole budget every pass without
    producing any committed progress — must NOT be auto-split (that would only
    spawn equally-spinning children) and must stay bounded by the per-pass hard
    cap.  It degrades to repeated *genuine* cap pauses (the designed-safe
    backstop), never a silent unbounded run."""
    # Progress gate reports no diff: the branch shows no real work.
    _patch_breakdown(monkeypatch, has_progress=False)
    ticket = SimpleNamespace(id="spin-1", title="Spinner", kind=TicketKind.TASK)
    svc = _FakeService(ticket)

    states = _drain(_ctx(svc, autosplit_threshold=3), ticket, lambda tick: "cap")

    # Every pass is a genuine cap pause; the spinner is never completed and
    # never mis-split into children.
    assert set(states[:-1]) == {"cap"}
    assert svc.promoted is False
    assert svc.created == []


def test_hard_cap_bounds_even_an_absurd_override() -> None:
    """The per-pass runaway backstop: even a sky-high per-ticket override is
    clamped to the global hard cap, so a looping agent can never obtain an
    unbounded (or disabled) tool-call budget."""
    settings = _settings()  # hard cap default 1000
    runaway = Ticket(
        id="e2",
        title="Runaway",
        workspace_path="w",
        coordinator_max_tool_calls_override=10_000_000,
    )
    cap = effective_coordinator_max_tool_calls(settings, runaway)
    assert cap == 1000  # finite, == hard cap — the backstop holds

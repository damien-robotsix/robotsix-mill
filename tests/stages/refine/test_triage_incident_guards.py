"""End-to-end coverage of the live triage incident-guard branches.

Ticket 3e81 removed the ``_triage.triage_skip`` aggregator shim and its
direct tests.  Review noted several of those deleted tests were the ONLY
end-to-end coverage of two functions that are still live:

* ``deterministic_triage_pre_checks`` — the LLM-free pre-checks
  (placeholder/test-fixture guard [noop-8835], doc-only fast-path, prior
  triage-SKIP replay, prescriptive-spec shortcut).
* ``handle_triage_decision`` — the triage-verdict handler
  (NO_CHANGE / SKIP / MIGRATE short-circuits, the SKIP→NO_CHANGE no-op
  coercion [noop-8835], the ops-shaped retrospect gate, and the
  mechanical draft fast-path).

These tests re-home that coverage by exercising the two functions
*directly* (not through the removed shim).  All LLM/network collaborators
are avoided: every path asserted here is deterministic (require_approval
defaults to False, so ``_resolve_next_state`` returns READY without an
auto-approve LLM call).
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents.refine_triage import TriageResult
from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _triage

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path):
    counter = [0]

    def make(**env):
        # Default to no approval gate so paths that resolve their next
        # state (triage SKIP / NO_CHANGE / MIGRATE / doc-only / prescriptive)
        # route deterministically to READY without an auto-approve LLM call.
        env.setdefault("require_approval", False)
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body=None, **kw):
    """Create a DRAFT ticket with a substantive (non-placeholder) body."""
    if body is None:
        body = "Make the ingest dedup path deterministic. This is a real spec."
    return ctx.service.create(title, body, **kw)


def _triage_result(decision="REFINE", reason="needs refinement", **kw):
    return TriageResult(decision=decision, reason=reason, **kw)


# ===========================================================================
# deterministic_triage_pre_checks — placeholder / test-fixture guard
# (noop-8835 / dummy-2218 incident)
# ===========================================================================

# Real data shapes of the leaked junk tickets (noop-8835, dummy-2218).
_JUNK_SHAPES = [
    ("noop", "disregard placeholder"),
    ("dummy", ""),
    ("no-op", ""),
    ("  Placeholder  ", ""),
    ("TMP", "x"),
]


@pytest.mark.parametrize(("title", "body"), _JUNK_SHAPES)
def test_pre_checks_placeholder_fixture_closes_done(ctx_factory, title, body):
    """A noop-8835-style fixture ticket is SKIPped straight to DONE with
    no refine/implement run — the exact leaked-junk failure mode."""
    ctx = ctx_factory()
    t = ctx.service.create(title, body, source="agent")
    ws = ctx.service.workspace(t)

    outcome = _triage.deterministic_triage_pre_checks(ctx, t, body, ws, ctx.settings)

    assert outcome is not None
    assert outcome.next_state == State.DONE
    assert "triage SKIP" in (outcome.note or "")
    assert "placeholder" in (outcome.note or "")


def test_pre_checks_genuine_ticket_falls_through(ctx_factory):
    """A real ticket (specific title + substantive body) is not short-
    circuited — the function returns None so refine runs normally."""
    ctx = ctx_factory()
    body = "The ingest dedup path is flaky; make it deterministic."
    t = _ticket(ctx, "Fix flaky dedup in refine", body)
    ws = ctx.service.workspace(t)

    assert (
        _triage.deterministic_triage_pre_checks(ctx, t, body, ws, ctx.settings) is None
    )


# ===========================================================================
# deterministic_triage_pre_checks — doc-only fast-path
# ===========================================================================


def test_pre_checks_doc_only_draft_routes_to_implement(ctx_factory):
    """When every touched path is docs/ or .md, skip triage + refine and
    route straight to implement."""
    ctx = ctx_factory()
    draft = "Update `docs/modules.yaml` and `README.md` for the new flag."
    t = _ticket(ctx, "Update docs", draft)
    ws = ctx.service.workspace(t)

    outcome = _triage.deterministic_triage_pre_checks(ctx, t, draft, ws, ctx.settings)

    assert outcome is not None
    assert outcome.next_state != State.DONE
    assert "Documentation-only" in (outcome.note or "")


# ===========================================================================
# deterministic_triage_pre_checks — prior triage-SKIP replay
# ===========================================================================


def test_pre_checks_prior_triage_skip_short_circuits(ctx_factory):
    """A prior triage-SKIP recorded in history is replayed without a fresh
    LLM classify — the ticket was already judged implementation-ready."""
    ctx = ctx_factory()
    draft = "Refine the dedup path in `src/foo.py` — the spec is already precise."
    t = _ticket(ctx, "Tighten dedup", draft)
    ws = ctx.service.workspace(t)

    # Simulate a prior triage SKIP: DRAFT → READY carrying the SKIP note.
    ctx.service.transition(
        t.id, State.READY, note="triage SKIP: already a precise spec"
    )
    t = ctx.service.get(t.id)

    outcome = _triage.deterministic_triage_pre_checks(ctx, t, draft, ws, ctx.settings)

    assert outcome is not None
    assert "triage SKIP" in (outcome.note or "")
    assert "prior verdict" in (outcome.note or "")


# ===========================================================================
# deterministic_triage_pre_checks — prescriptive-spec shortcut
# ===========================================================================


def test_pre_checks_prescriptive_spec_threshold_met(ctx_factory):
    """A draft whose fenced code blocks meet the line threshold is treated
    as an implementation-ready spec — skip the refine LLM."""
    ctx = ctx_factory(refine_prescriptive_spec_code_lines_threshold=3)
    draft = (
        "## Problem\n\n```python\n"
        + "\n".join(f"line_{i:03d}()" for i in range(5))
        + "\n```"
    )
    t = _ticket(ctx, "Add parser", draft)
    ws = ctx.service.workspace(t)

    outcome = _triage.deterministic_triage_pre_checks(ctx, t, draft, ws, ctx.settings)

    assert outcome is not None
    assert "prescriptive spec" in (outcome.note or "")


def test_pre_checks_prescriptive_threshold_zero_disabled(ctx_factory):
    """With the threshold set to 0 the prescriptive shortcut is disabled —
    a code-heavy draft falls through to refine (None)."""
    ctx = ctx_factory(refine_prescriptive_spec_code_lines_threshold=0)
    draft = (
        "## Problem\n\n```python\n"
        + "\n".join(f"line_{i:03d}()" for i in range(10))
        + "\n```"
    )
    t = _ticket(ctx, "Add parser", draft)
    ws = ctx.service.workspace(t)

    assert (
        _triage.deterministic_triage_pre_checks(ctx, t, draft, ws, ctx.settings) is None
    )


# ===========================================================================
# handle_triage_decision — NO_CHANGE
# ===========================================================================


def test_decision_no_change_task_without_branch_routes_to_implement(ctx_factory):
    """A TASK-kind NO_CHANGE with no (merged) branch must NOT auto-close
    from DRAFT — it routes to implement to verify the claim against the
    live tree.  Regression: these sat at HUMAN_ISSUE_APPROVAL for days."""
    ctx = ctx_factory()
    t = _ticket(ctx, "Maybe already done", kind=TicketKind.TASK)
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        "draft body",
        None,
        ws,
        ctx.settings,
        _triage_result("NO_CHANGE", "deliverable already exists"),
    )

    assert outcome is not None
    assert outcome.next_state == State.READY
    assert "NO_CHANGE" in (outcome.note or "")
    assert "routing to implement" in (outcome.note or "")


# ===========================================================================
# handle_triage_decision — SKIP (and the SKIP→NO_CHANGE no-op coercion)
# ===========================================================================


def test_decision_skip_routes_to_implement(ctx_factory):
    """A genuine SKIP (draft is already an implementation-ready spec) routes
    to implement with the triage-SKIP note preserved."""
    ctx = ctx_factory()
    draft = "Edit `src/foo.py` line 10 to return the cached value instead."
    t = _ticket(ctx, "Return cached value", draft)
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        draft,
        None,
        ws,
        ctx.settings,
        _triage_result("SKIP", "spec is precise and single-scoped"),
    )

    assert outcome is not None
    assert outcome.next_state == State.READY
    assert "triage SKIP" in (outcome.note or "")


def test_decision_skip_noop_reason_coerces_to_no_change(ctx_factory):
    """A SKIP whose reason asserts there is nothing to do is coerced to
    NO_CHANGE — it must NOT route straight to implement with auto-approve
    (the live noop-8835 failure mode)."""
    ctx = ctx_factory()
    draft = "Edit `src/foo.py` — but on inspection there is no change required."
    t = _ticket(ctx, "Investigate foo", draft, kind=TicketKind.TASK)
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        draft,
        None,
        ws,
        ctx.settings,
        _triage_result("SKIP", "nothing to implement — already handled"),
    )

    assert outcome is not None
    # Coerced into the NO_CHANGE handler, not the plain "triage SKIP:" note.
    assert "NO_CHANGE" in (outcome.note or "")
    assert "triage SKIP:" not in (outcome.note or "")


# ===========================================================================
# handle_triage_decision — MIGRATE
# ===========================================================================


def test_decision_migrate_invalid_target_escalates(ctx_factory):
    """A MIGRATE to an unregistered board is not migrated — it escalates
    with an explicit invalid-target note."""
    ctx = ctx_factory()
    t = _ticket(ctx, "Wrong board", "This belongs elsewhere.")
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        "This belongs elsewhere.",
        None,
        ws,
        ctx.settings,
        _triage_result(
            "MIGRATE", "belongs on another board", target_board="no-such-board-xyz"
        ),
    )

    assert outcome is not None
    assert "MIGRATE invalid target" in (outcome.note or "")
    # The ticket stays on its original board (not migrated).
    assert ctx.service.get(t.id).board_id == "test-board"


# ===========================================================================
# handle_triage_decision — ops-shaped retrospect gate
# ===========================================================================


def test_decision_ops_shaped_retrospect_routes_to_human(ctx_factory):
    """An ops-shaped retrospect follow-up (asks to RUN against a deployed
    service / high-memory host) can't be satisfied by a mill implement run
    — it must route to a human/ops gate, ahead of the fast-path."""
    ctx = ctx_factory()
    draft = (
        "Re-run the batch job on an environment with adequate memory to "
        "confirm the OOM is gone."
    )
    t = _ticket(ctx, "Verify batch OOM fix", draft, source=SourceKind.RETROSPECT)
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        draft,
        None,
        ws,
        ctx.settings,
        _triage_result("REFINE", "looks mechanical"),
    )

    assert outcome is not None
    assert outcome.next_state == State.HUMAN_ISSUE_APPROVAL
    assert "ops-shaped retrospect follow-up" in (outcome.note or "")


# ===========================================================================
# handle_triage_decision — mechanical draft fast-path
# ===========================================================================


def test_decision_mechanical_fast_path_deterministic_source(ctx_factory):
    """A deterministic-source (auto-approve) ticket with a substantive draft
    skips the refine LLM via the mechanical fast-path."""
    ctx = ctx_factory()
    draft = (
        "Add the `foo_enabled` flag to `src/robotsix_mill/config/settings.py` "
        "and default it to False so the periodic scan stays opt-in."
    )
    t = _ticket(ctx, "Add opt-in flag", draft, source=SourceKind.AUDIT)
    ws = ctx.service.workspace(t)

    outcome = _triage.handle_triage_decision(
        ctx,
        t,
        draft,
        None,
        ws,
        ctx.settings,
        _triage_result("REFINE", "single-file flag addition"),
    )

    assert outcome is not None
    assert "mechanical draft fast-path" in (outcome.note or "")
    assert "deterministic source" in (outcome.note or "")


def test_decision_refine_falls_through(ctx_factory):
    """A plain REFINE on a non-auto-approve, non-gated ticket falls through
    to the full refine agent (None) — no short-circuit."""
    ctx = ctx_factory()
    draft = "Edit `src/foo.py` to add real behaviour; this needs a refine pass."
    t = _ticket(ctx, "Needs refinement", draft)
    ws = ctx.service.workspace(t)

    assert (
        _triage.handle_triage_decision(
            ctx,
            t,
            draft,
            None,
            ws,
            ctx.settings,
            _triage_result("REFINE", "needs a full pass"),
        )
        is None
    )

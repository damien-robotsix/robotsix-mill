"""Tests for the RefineStage dedup short-circuits and in-flight advisories.

The stage fixtures, ticket helper, and mock seams are imported from
``test_stage`` (the parent module, which retains them); only the dedup
scenario tests live here.
"""

import pytest

from robotsix_mill.agents import dedup, refining
from robotsix_mill.agents.refining import RefineResult
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage
from tests.stages.refine.test_stage import (
    _apply_default_mocks,
    _mock_dedup,
    _mock_refine_ok,
    _mock_triage_refine,
    _ticket,
)


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        from robotsix_mill.config import RepoConfig

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


# ---------------------------------------------------------------------------
# 3. dedup: duplicate → DONE
# ---------------------------------------------------------------------------


def test_dedup_duplicate_short_circuits_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, body="Fix the login form")
    # A candidate sharing tokens with the draft so the zero-overlap
    # short-circuit does NOT fire and the (mocked) dedup LLM runs.
    _ticket(ctx, title="Login form fix", body="Fix the login form")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
    monkeypatch.setattr(refining, "triage_refine", _mock_triage_refine())
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of="ticket-abc", reason="same title", already_done=None),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "duplicate" in out.note
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 3b. dedup: SKIPPED once the operator has requested changes
# ---------------------------------------------------------------------------


def test_dedup_skipped_after_operator_changes_requested(ctx_factory, monkeypatch):
    """Regression: once an operator sends a ticket back with 'changes
    requested', dedup must NOT auto-close it as a duplicate/already-done —
    the human is actively iterating it. (The auto-mail board-columns ticket
    was silently dedup-closed this way after two operator sendbacks.)"""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(
        ctx, body="The board columns should reflect the awaiting action on each mail"
    )
    # Operator sendback: DRAFT -> HUMAN_ISSUE_APPROVAL -> request_changes -> DRAFT
    ctx.service.transition(t.id, State.HUMAN_ISSUE_APPROVAL)
    ctx.service.request_changes(t.id, "use awaiting-action columns")
    t = ctx.service.get(t.id)

    _apply_default_mocks(
        monkeypatch,
        # dedup WOULD flag a duplicate — the guard must skip it entirely.
        run_dedup_check=_mock_dedup(
            duplicate_of="ticket-abc", already_done=None, reason="same idea"
        ),
        run_refine_agent=_mock_refine_ok(
            spec_markdown="## Problem\nawaiting-action cols"
        ),
    )

    out = RefineStage().run(t, ctx)

    # Refined (not closed as a duplicate) — dedup was skipped.
    assert out.next_state is State.READY
    assert "duplicate" not in (out.note or "")


# ---------------------------------------------------------------------------
# 4. dedup: already done → DONE
# ---------------------------------------------------------------------------


def test_dedup_already_done_short_circuits_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, body="Add dark mode toggle")
    # A candidate sharing tokens with the draft so the zero-overlap
    # short-circuit does NOT fire and the (mocked) dedup LLM runs.
    _ticket(ctx, title="Dark mode toggle", body="Add dark mode toggle")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
    monkeypatch.setattr(refining, "triage_refine", _mock_triage_refine())
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done="abc123", reason="commit found"),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "already implemented" in out.note
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 5. dedup exception → fall through to refine
# ---------------------------------------------------------------------------


def test_dedup_check_exception_proceeds_to_refine(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false")
    t = _ticket(ctx, body="Fix the bug")

    refine_called = []

    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        lambda *a, **k: (_ for _ in ()).throw(Exception("boom")),
    )
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        _mock_refine_ok(spec_markdown="## Problem\nDone"),
    )
    monkeypatch.setattr(refining, "triage_refine", _mock_triage_refine())
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    # track that refine was called
    orig = refining.run_refine_agent

    def _track(*a, **k):
        refine_called.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(refining, "run_refine_agent", _track)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(refine_called) == 1


# ---------------------------------------------------------------------------
# 5a. dedup: already_done candidate with UNMERGED branch → proceed to refine
# ---------------------------------------------------------------------------


def test_dedup_unmerged_candidate_proceeds_to_refine(ctx_factory, monkeypatch):
    """An ``already_done`` candidate that reached DONE via a real
    implementation but whose branch never merged to main is NOT a
    valid dedup target — refine must run rather than short-circuit to
    DONE, otherwise the new ticket closes against stranded work."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Add dark mode toggle")

    # Candidate driven to DONE via a real implementation (passes the
    # four existing rejection checks) and carrying an implement branch.
    cand = _ticket(ctx, title="Dark mode", body="Add a dark mode toggle")
    ctx.service.set_branch(cand.id, "feature/dark-mode")
    ctx.service.transition(cand.id, State.DONE, note="implemented dark mode")
    cand = ctx.service.get(cand.id)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nDo it"),
        run_dedup_check=_mock_dedup(
            duplicate_of=None, already_done=cand.id, reason="found"
        ),
        # Candidate's branch is unmerged.
        _verify_branch_merged=lambda repo_dir, ticket: False,
    )

    refine_called = []
    orig = refining.run_refine_agent

    def _track(*a, **k):
        refine_called.append(1)
        return orig(*a, **k)

    monkeypatch.setattr(refining, "run_refine_agent", _track)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(refine_called) == 1


# ---------------------------------------------------------------------------
# 5a-bis. dedup: un-refined DRAFT candidate → proceed to refine
# ---------------------------------------------------------------------------


def test_dedup_unrefined_draft_candidate_proceeds_to_refine(ctx_factory, monkeypatch):
    """A ``duplicate_of`` candidate that has never progressed past DRAFT
    (no refine-progress history) is NOT a valid dedup target — closing a
    further-along ticket into it would bury the fix in a ticket that may
    never be implemented.  Refine must run rather than short-circuit to
    DONE."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Fix the login form")

    # Candidate left in DRAFT (never refined) — no transition at all.
    cand = _ticket(ctx, title="Login form fix", body="Fix the login form")

    refine_called = []

    def _track(*a, **k):
        refine_called.append(1)
        return _mock_refine_ok(spec_markdown="## Problem\nDo it")(*a, **k)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_track,
        run_dedup_check=_mock_dedup(
            duplicate_of=cand.id, already_done=None, reason="same idea"
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "duplicate" not in (out.note or "")
    assert len(refine_called) == 1


# ---------------------------------------------------------------------------
# 5b. dedup: already_done candidate with MERGED branch → DONE (no regression)
# ---------------------------------------------------------------------------


def test_dedup_merged_candidate_short_circuits_to_done(ctx_factory, monkeypatch):
    """A valid dedup candidate whose implementation branch IS merged to
    main still short-circuits the new ticket to DONE — no regression."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Add dark mode toggle")

    cand = _ticket(ctx, title="Dark mode", body="Add a dark mode toggle")
    ctx.service.set_branch(cand.id, "feature/dark-mode")
    ctx.service.transition(cand.id, State.DONE, note="implemented dark mode")
    cand = ctx.service.get(cand.id)

    agent_called = []
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=lambda *a, **k: agent_called.append(1),
        run_dedup_check=_mock_dedup(
            duplicate_of=None, already_done=cand.id, reason="found"
        ),
        _verify_branch_merged=lambda repo_dir, ticket: True,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "already implemented" in out.note
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 5c. dedup: already_done candidate with NO branch → DONE (merge check skipped)
# ---------------------------------------------------------------------------


def test_dedup_candidate_without_branch_short_circuits_to_done(
    ctx_factory, monkeypatch
):
    """A candidate that reached DONE via implementation but never had a
    branch (e.g. closed by commit hash) must still short-circuit to
    DONE — the merge check only applies when the candidate has a
    branch, so a False ``_verify_branch_merged`` must NOT reject it."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Add dark mode toggle")

    # Candidate driven to DONE but with NO branch set.
    cand = _ticket(ctx, title="Dark mode", body="Add a dark mode toggle")
    ctx.service.transition(cand.id, State.DONE, note="implemented dark mode")
    cand = ctx.service.get(cand.id)

    agent_called = []
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=lambda *a, **k: agent_called.append(1),
        run_dedup_check=_mock_dedup(
            duplicate_of=None, already_done=cand.id, reason="found"
        ),
        # Even with the merge check returning False, the absence of a
        # branch must skip it entirely.
        _verify_branch_merged=lambda repo_dir, ticket: False,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "already implemented" in out.note
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 6d. advisory dedup against a CONCURRENT in-flight (non-DONE) ticket
# ---------------------------------------------------------------------------


def test_inflight_advisory_flags_concurrent_ready_draft(ctx_factory, monkeypatch):
    """A fresh draft overlapping a CONCURRENT in-flight ticket (READY,
    never DONE) is annotated with a ``[!warning]`` advisory naming that
    ticket and still proceeds to refine — never auto-closed.  The dedup
    guard alone cannot catch this (it only closes against DONE)."""
    ctx = ctx_factory(
        require_approval="false",
        refine_triage_enabled="false",
        refine_advisory_dedup_enabled=False,
    )

    prior = ctx.service.create(
        "rework login validation",
        # The candidate declares the shared path under ``## Scope`` so a
        # lone shared path still flags under the strict-scope rule.
        # Include ≥3 concern tokens so the concern_min_overlap=3 gate
        # (tightened in the 2026-06-09 false-positive fix) is satisfied.
        "## Scope\n\nchanges src/robotsix_mill/auth.py for "
        "`validate_input`, `sanitize`, and `normalize` in the login form",
    )
    ctx.service.transition(prior.id, State.READY, note="refined")

    t = _ticket(
        ctx,
        title="fix login form validation",
        body=(
            "Fix the login form. This also edits "
            "src/robotsix_mill/auth.py for `validate_input`, `sanitize`, "
            "and `normalize`, padded well past "
            "the 100-char trivial-draft threshold so the advisory runs."
        ),
    )

    captured = {}

    def _capture(*, settings, title, draft, **kw):
        del settings, title, kw
        captured["draft"] = draft
        return RefineResult(spec_markdown="## Problem\nFix it")

    _apply_default_mocks(monkeypatch, run_refine_agent=_capture)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "[!warning]" in captured["draft"]
    assert prior.id in captured["draft"]
    assert "src/robotsix_mill/auth.py" in captured["draft"]


def test_inflight_advisory_untouched_when_distinct(ctx_factory, monkeypatch):
    """A draft with no path/title overlap against any recent ticket is
    passed to refine unchanged — no advisory note."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")

    prior = ctx.service.create(
        "rework login validation",
        "changes src/robotsix_mill/auth.py to validate the login form",
    )
    ctx.service.transition(prior.id, State.READY, note="refined")

    t = _ticket(
        ctx,
        title="add metrics dashboard",
        body=(
            "Add a metrics dashboard. Touches "
            "src/robotsix_mill/runtime/metrics.py only, padded well past "
            "the 100-char trivial-draft threshold so the advisory runs."
        ),
    )

    captured = {}

    def _capture(*, settings, title, draft, **kw):
        del settings, title, kw
        captured["draft"] = draft
        return RefineResult(spec_markdown="## Problem\nFix it")

    _apply_default_mocks(monkeypatch, run_refine_agent=_capture)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "[!warning]" not in captured["draft"]

"""Unit tests for the RefineGatesMixin gate staticmethods.

Coverage: the five pre-refine gate methods of
:class:`robotsix_mill.stages.refine.gates.RefineGatesMixin` exercised in
isolation — ``_run_dedup_guard``, ``_is_valid_dedup_target``,
``_run_inflight_advisory``, ``_run_freshness_gate`` and
``_run_obsolescence_gate``.  Every method is a ``@staticmethod`` so each
test calls it directly on :class:`RefineStage` (no instance, no full
``RefineStage().run(...)`` pipeline).

Only the LLM collaborators are mocked (the dedup / freshness /
obsolescence agents, the in-flight overlap helpers and the
branch-merged verification facade).  The ``TicketService`` and
``Workspace`` are always real (per-test SQLite on ``tmp_path``).
"""

from __future__ import annotations

import pytest

import robotsix_mill.agents.dedup as agents_dedup
import robotsix_mill.agents.freshness as freshness_mod
import robotsix_mill.agents.obsolescence as obsolescence_mod
import robotsix_mill.core.dedup as dedup_top
import robotsix_mill.stages.refine as refine_module
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.stages.refine.helpers import (
    DEDUP_ALREADY_DONE_PREFIX,
    DEDUP_DUPLICATE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    OBSOLESCENCE_GAP_PREFIX,
)


# ---------------------------------------------------------------------------
# fixtures / helpers (minimal copies of the test_refine_stage.py versions)
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        created.append(s)
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


# A body comfortably above the 100-char trivial-draft threshold so the
# dedup pipeline runs.  Shares "login"/"form"/"fix" tokens so a
# similarly-worded candidate produces real token overlap.
_DEDUP_DRAFT = (
    "Fix the login form so the user can authenticate. This draft body is "
    "padded well past the hundred character trivial-draft threshold so the "
    "dedup guard actually runs against this ticket."
)


def _ticket(ctx, title="Fix login form", body=None, **kw):
    """Create a DRAFT ticket with a substantive (>100 char) body."""
    if body is None:
        body = _DEDUP_DRAFT
    return ctx.service.create(title, body, **kw)


def _mock_dedup(**verdict):
    def _run(
        *, settings, draft_title, draft_body, candidates_json, repo_dir=None, **kw
    ):
        del settings, draft_title, draft_body, candidates_json, repo_dir, kw
        return verdict

    return _run


# ===========================================================================
# 1. _run_dedup_guard
# ===========================================================================


def test_dedup_guard_trivial_draft_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)

    calls: list = []

    def _spy(**kw):
        calls.append(kw)
        return {"duplicate_of": "x", "already_done": None, "reason": "dup"}

    monkeypatch.setattr(agents_dedup, "run_dedup_check", _spy)

    out = RefineStage._run_dedup_guard(ctx, t, "short draft", None, ctx.settings)

    assert out is None
    assert calls == []


def test_dedup_guard_skipped_after_operator_sendback(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    # A candidate sharing tokens so the zero-overlap short-circuit would
    # not otherwise fire.
    _ticket(ctx, title="Login form fix", body="Fix the login form")

    # Operator sendback: DRAFT -> HUMAN_ISSUE_APPROVAL -> request_changes.
    ctx.service.transition(t.id, State.HUMAN_ISSUE_APPROVAL)
    ctx.service.request_changes(t.id, "use awaiting-action columns")
    t = ctx.service.get(t.id)

    calls: list = []

    def _spy(**kw):
        calls.append(kw)
        return {"duplicate_of": "ticket-abc", "already_done": None, "reason": "dup"}

    monkeypatch.setattr(agents_dedup, "run_dedup_check", _spy)

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert calls == []


def test_dedup_guard_duplicate_verdict_routes_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _ticket(ctx, title="Login form fix", body="Fix the login form")

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of="ticket-abc", already_done=None, reason="same idea"),
    )

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert out.note.startswith(DEDUP_DUPLICATE_PREFIX)


def test_dedup_guard_already_done_verdict_routes_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _ticket(ctx, title="Login form fix", body="Fix the login form")

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done="abc123", reason="commit found"),
    )

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert out.note.startswith(DEDUP_ALREADY_DONE_PREFIX)


def test_dedup_guard_invalid_target_proceeds(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    # The named target is an un-refined DRAFT candidate (invalid target),
    # which also supplies the token overlap so the LLM runs.
    cand = _ticket(ctx, title="Login form fix", body="Fix the login form")

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=cand.id, already_done=None, reason="same idea"),
    )

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_dedup_guard_no_overlap_skips_llm(ctx_factory, monkeypatch):
    # dedup_skip_on_no_overlap defaults to True; with no candidates the
    # LLM call is skipped entirely.
    ctx = ctx_factory(dedup_skip_on_no_overlap=True)
    t = _ticket(ctx)

    calls: list = []

    def _spy(**kw):
        calls.append(kw)
        return {"duplicate_of": "x", "already_done": None, "reason": "dup"}

    monkeypatch.setattr(agents_dedup, "run_dedup_check", _spy)

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert calls == []


def test_dedup_guard_check_raises_is_swallowed(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _ticket(ctx, title="Login form fix", body="Fix the login form")

    def _boom(**kw):
        raise RuntimeError("dedup boom")

    monkeypatch.setattr(agents_dedup, "run_dedup_check", _boom)

    out = RefineStage._run_dedup_guard(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


# ===========================================================================
# 2. _is_valid_dedup_target
# ===========================================================================


def test_valid_target_unknown_candidate_is_true(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)

    # A commit-hash-like id that does not resolve to a ticket.
    assert RefineStage._is_valid_dedup_target(ctx, t, "a1b2c3d4e5f6", None) is True


def test_valid_target_circular_is_false(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Other ticket")
    # Candidate was itself closed as a dedup of the current ticket.
    ctx.service.transition(
        cand.id, State.DONE, note=f"{DEDUP_DUPLICATE_PREFIX}{t.id}: circular"
    )

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_errored_candidate_is_false(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Failed attempt")
    ctx.service.transition(cand.id, State.ERRORED)

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_unrefined_draft_is_false(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    # Candidate never progressed past DRAFT (no refine-progress history).
    cand = _ticket(ctx, title="Never refined")

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_closed_never_done_is_false(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Declined as noise")
    ctx.service.transition(cand.id, State.CLOSED)

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_non_implementation_closure_is_false(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Freshness-closed")
    # Reached DONE via a non-implementation closure (freshness-closed).
    ctx.service.transition(
        cand.id, State.DONE, note=f"{FRESHNESS_STALE_PREFIX} — not found"
    )

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_unmerged_branch_is_false(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Real implementation")
    ctx.service.set_branch(cand.id, "feature/work")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: False
    )

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is False


def test_valid_target_merged_branch_is_true(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Real implementation")
    ctx.service.set_branch(cand.id, "feature/work")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is True


# ===========================================================================
# 3. _run_inflight_advisory
# ===========================================================================


def test_inflight_advisory_epic_returns_draft_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    epic = ctx.service.create("Epic", "Epic body", kind="epic")
    ws = ctx.service.workspace(epic)

    calls: list = []
    monkeypatch.setattr(
        dedup_top, "find_inflight_overlap", lambda *a, **k: calls.append(1)
    )

    out = RefineStage._run_inflight_advisory(ctx, epic, _DEDUP_DRAFT, ws, ctx.settings)

    assert out == _DEDUP_DRAFT
    assert calls == []


def test_inflight_advisory_child_returns_draft_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    parent = ctx.service.create("Parent", "Parent body", kind="epic")
    child = ctx.service.create("Child", "Child body", parent_id=parent.id)
    ws = ctx.service.workspace(child)

    calls: list = []
    monkeypatch.setattr(
        dedup_top, "find_inflight_overlap", lambda *a, **k: calls.append(1)
    )

    out = RefineStage._run_inflight_advisory(ctx, child, _DEDUP_DRAFT, ws, ctx.settings)

    assert out == _DEDUP_DRAFT
    assert calls == []


def test_inflight_advisory_trivial_draft_returns_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    calls: list = []
    monkeypatch.setattr(
        dedup_top, "find_inflight_overlap", lambda *a, **k: calls.append(1)
    )

    out = RefineStage._run_inflight_advisory(ctx, t, "short", ws, ctx.settings)

    assert out == "short"
    assert calls == []


def test_inflight_advisory_no_overlap_returns_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", lambda *a, **k: None)

    out = RefineStage._run_inflight_advisory(ctx, t, _DEDUP_DRAFT, ws, ctx.settings)

    assert out == _DEDUP_DRAFT


def test_inflight_advisory_overlap_annotates_draft(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    sentinel = "ANNOTATED DRAFT BODY with the advisory note prepended."

    monkeypatch.setattr(
        dedup_top, "find_inflight_overlap", lambda *a, **k: "overlaps ticket-xyz"
    )
    monkeypatch.setattr(
        dedup_top, "annotate_child_body", lambda body, note, **k: sentinel
    )

    out = RefineStage._run_inflight_advisory(ctx, t, _DEDUP_DRAFT, ws, ctx.settings)

    assert out == sentinel
    assert out != _DEDUP_DRAFT
    # Workspace description was updated with the annotated draft.
    assert ws.read_description() == sentinel


# ===========================================================================
# 4. _run_freshness_gate
# ===========================================================================


def test_freshness_gate_disabled_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory()  # freshness_gate_enabled defaults to False
    t = _ticket(ctx)

    calls: list = []
    monkeypatch.setattr(
        freshness_mod, "run_freshness_check", lambda **k: calls.append(1)
    )

    out = RefineStage._run_freshness_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert calls == []


def test_freshness_gate_trivial_draft_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory(freshness_gate_enabled=True)
    t = _ticket(ctx)

    calls: list = []
    monkeypatch.setattr(
        freshness_mod, "run_freshness_check", lambda **k: calls.append(1)
    )

    out = RefineStage._run_freshness_gate(ctx, t, "short", None, ctx.settings)

    assert out is None
    assert calls == []


def test_freshness_gate_stale_routes_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory(freshness_gate_enabled=True)
    t = _ticket(ctx)

    monkeypatch.setattr(
        freshness_mod,
        "run_freshness_check",
        lambda *, draft, repo_dir=None, **k: {"stale": True, "reason": "not on HEAD"},
    )

    out = RefineStage._run_freshness_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert out.note.startswith(FRESHNESS_STALE_PREFIX)


def test_freshness_gate_fresh_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory(freshness_gate_enabled=True)
    t = _ticket(ctx)

    monkeypatch.setattr(
        freshness_mod,
        "run_freshness_check",
        lambda *, draft, repo_dir=None, **k: {"stale": False, "reason": "fresh"},
    )

    out = RefineStage._run_freshness_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_freshness_gate_check_raises_is_swallowed(ctx_factory, monkeypatch):
    ctx = ctx_factory(freshness_gate_enabled=True)
    t = _ticket(ctx)

    def _boom(**k):
        raise RuntimeError("freshness boom")

    monkeypatch.setattr(freshness_mod, "run_freshness_check", _boom)

    out = RefineStage._run_freshness_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


# ===========================================================================
# 5. _run_obsolescence_gate
# ===========================================================================


def test_obsolescence_gate_disabled_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory()  # obsolescence_gate_enabled defaults to False
    t = _ticket(ctx, source="agent_check")

    calls: list = []
    monkeypatch.setattr(
        obsolescence_mod, "run_obsolescence_check", lambda **k: calls.append(1)
    )

    out = RefineStage._run_obsolescence_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert calls == []


def test_obsolescence_gate_trivial_draft_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory(obsolescence_gate_enabled=True)
    t = _ticket(ctx, source="agent_check")

    calls: list = []
    monkeypatch.setattr(
        obsolescence_mod, "run_obsolescence_check", lambda **k: calls.append(1)
    )

    out = RefineStage._run_obsolescence_gate(ctx, t, "short", None, ctx.settings)

    assert out is None
    assert calls == []


def test_obsolescence_gate_user_authored_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory(obsolescence_gate_enabled=True)
    # Default source is USER.
    t = _ticket(ctx)
    assert t.source == SourceKind.USER

    calls: list = []
    monkeypatch.setattr(
        obsolescence_mod, "run_obsolescence_check", lambda **k: calls.append(1)
    )

    out = RefineStage._run_obsolescence_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert calls == []


def test_obsolescence_gate_obsolete_routes_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory(obsolescence_gate_enabled=True)
    t = _ticket(ctx, source="agent_check")

    monkeypatch.setattr(
        obsolescence_mod,
        "run_obsolescence_check",
        lambda *, settings, draft_title, draft_body, repo_dir=None, **k: {
            "obsolete": True,
            "reason": "gap already resolved",
        },
    )

    out = RefineStage._run_obsolescence_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert out.note.startswith(OBSOLESCENCE_GAP_PREFIX)


def test_obsolescence_gate_not_obsolete_returns_none(ctx_factory, monkeypatch):
    ctx = ctx_factory(obsolescence_gate_enabled=True)
    t = _ticket(ctx, source="agent_check")

    monkeypatch.setattr(
        obsolescence_mod,
        "run_obsolescence_check",
        lambda *, settings, draft_title, draft_body, repo_dir=None, **k: {
            "obsolete": False,
            "reason": "gap still exists",
        },
    )

    out = RefineStage._run_obsolescence_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_obsolescence_gate_check_raises_is_swallowed(ctx_factory, monkeypatch):
    ctx = ctx_factory(obsolescence_gate_enabled=True)
    t = _ticket(ctx, source="agent_check")

    def _boom(**k):
        raise RuntimeError("obsolescence boom")

    monkeypatch.setattr(obsolescence_mod, "run_obsolescence_check", _boom)

    out = RefineStage._run_obsolescence_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None

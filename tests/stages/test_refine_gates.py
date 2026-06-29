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

import subprocess
from pathlib import Path

import pytest

import robotsix_mill.agents.dedup as agents_dedup
import robotsix_mill.agents.freshness as freshness_mod
import robotsix_mill.agents.obsolescence as obsolescence_mod
import robotsix_mill.core.dedup as dedup_top
import robotsix_mill.stages.refine as refine_module
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.stages.refine.helpers import (
    DEDUP_ALREADY_DONE_PREFIX,
    DEDUP_DUPLICATE_PREFIX,
    FRESHNESS_STALE_PREFIX,
    OBSOLESCENCE_GAP_PREFIX,
    REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX,
    REFINE_MILL_MISROUTE_PREFIX,
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


def test_valid_target_no_file_map_overlap_is_false(ctx_factory, monkeypatch):
    """_is_valid_dedup_target returns False when draft paths don't overlap
    with the candidate's declared scope paths."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand_body = "## Scope\n\n- `tests/foo/test_bar.py`\n\nSome other text."
    cand = _ticket(ctx, title="Other fix", body=cand_body)
    ctx.service.set_branch(cand.id, "feature/other")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    draft = "Fix tests/core/test_langfuse_client.py to handle None response"

    assert (
        RefineStage._is_valid_dedup_target(ctx, t, cand.id, None, draft=draft) is False
    )


def test_valid_target_file_map_overlap_is_true(ctx_factory, monkeypatch):
    """_is_valid_dedup_target returns True when draft paths overlap with
    the candidate's declared scope paths."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand_body = "## Scope\n\n- `tests/core/test_langfuse_client.py`\n\nSome text."
    cand = _ticket(ctx, title="Other fix", body=cand_body)
    ctx.service.set_branch(cand.id, "feature/other")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    draft = "Fix tests/core/test_langfuse_client.py to handle None response"

    assert (
        RefineStage._is_valid_dedup_target(ctx, t, cand.id, None, draft=draft) is True
    )


def test_valid_target_draft_none_skips_overlap_check(ctx_factory, monkeypatch):
    """When draft is None (default), the file-map overlap check is skipped
    and existing behaviour is preserved."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    cand_body = "## Scope\n\n- `tests/foo/test_bar.py`\n\nSome text."
    cand = _ticket(ctx, title="Other fix", body=cand_body)
    ctx.service.set_branch(cand.id, "feature/other")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)

    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    # draft=None (default) → overlap check skipped, returns True
    assert RefineStage._is_valid_dedup_target(ctx, t, cand.id, None) is True


def test_valid_target_unknown_candidate_not_ancestor_is_false(ctx_factory, monkeypatch):
    """When the candidate is a commit hash that is NOT an ancestor of
    origin/main, _is_valid_dedup_target returns False."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    class FakeResult:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())

    # repo_dir is not None → ancestry check fires
    assert (
        RefineStage._is_valid_dedup_target(ctx, t, "a1b2c3d4e5f6", Path("/tmp"))
        is False
    )


def test_valid_target_unknown_candidate_ancestry_git_error_is_true(
    ctx_factory, monkeypatch
):
    """When git merge-base check raises an exception, _is_valid_dedup_target
    returns True (best-effort — never block on git errors)."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    def _boom(*a, **kw):
        raise RuntimeError("git boom")

    monkeypatch.setattr(subprocess, "run", _boom)

    # repo_dir is not None → ancestry check fires and swallows error
    assert (
        RefineStage._is_valid_dedup_target(ctx, t, "a1b2c3d4e5f6", Path("/tmp")) is True
    )


# ===========================================================================
# 3. _run_inflight_advisory
# ===========================================================================


def test_inflight_advisory_epic_returns_draft_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    epic = ctx.service.create("Epic", "Epic body", kind=TicketKind.EPIC)
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
    parent = ctx.service.create("Parent", "Parent body", kind=TicketKind.EPIC)
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


# ===========================================================================
# 6. _run_inflight_advisory — CI fingerprint label path
# ===========================================================================

_CI_DRAFT_BODY = """\
**Workflow:** CI
**Path:** .github/workflows/ci.yml
**Branch:** main
**Run:** [1234567890](https://github.com/owner/repo/actions/runs/1234567890)
**Commit:** `abc123def456`
**Created:** 2024-01-15T10:30:45.123456+00:00

Error: process completed with exit code 1.
error: could not find `--no-emit-project` flag
"""


def test_inflight_advisory_ci_stores_fingerprint_label(ctx_factory, monkeypatch):
    """When the ticket source is CI, the fingerprint label is stored on
    the ticket via set_labels (idempotent — only once)."""
    import json

    ctx = ctx_factory()
    t = _ticket(ctx, source="ci")
    ws = ctx.service.workspace(t)

    # Suppress overlap so we only observe the label side-effect.
    monkeypatch.setattr(dedup_top, "find_inflight_overlap", lambda *a, **k: None)

    _ = RefineStage._run_inflight_advisory(ctx, t, _CI_DRAFT_BODY, ws, ctx.settings)

    # Re-fetch ticket; verify a ci_fp: label was stored.
    t2 = ctx.service.get(t.id)
    assert t2.labels is not None
    labels = json.loads(t2.labels)
    ci_fp_labels = [lbl for lbl in labels if lbl.startswith("ci_fp:")]
    assert len(ci_fp_labels) == 1
    assert len(ci_fp_labels[0]) == len("ci_fp:") + 16  # ci_fp: + 16 hex chars


def test_inflight_advisory_ci_label_storage_is_idempotent(ctx_factory, monkeypatch):
    """Calling _run_inflight_advisory twice on the same CI ticket does
    not duplicate the ci_fp: label."""
    import json

    ctx = ctx_factory()
    t = _ticket(ctx, source="ci")
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", lambda *a, **k: None)

    RefineStage._run_inflight_advisory(ctx, t, _CI_DRAFT_BODY, ws, ctx.settings)
    # Second call with same draft — must not add a duplicate label.
    RefineStage._run_inflight_advisory(ctx, t, _CI_DRAFT_BODY, ws, ctx.settings)

    t2 = ctx.service.get(t.id)
    labels = json.loads(t2.labels)
    ci_fp_labels = [lbl for lbl in labels if lbl.startswith("ci_fp:")]
    assert len(ci_fp_labels) == 1


def test_inflight_advisory_ci_passes_dedup_labels(ctx_factory, monkeypatch):
    """When the ticket source is CI, dedup_labels is computed and passed
    to find_inflight_overlap."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="ci")
    ws = ctx.service.workspace(t)

    captured: list = []

    def _spy(*a, dedup_labels=None, **k):
        captured.append(dedup_labels)
        return None

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", _spy)

    RefineStage._run_inflight_advisory(ctx, t, _CI_DRAFT_BODY, ws, ctx.settings)

    assert len(captured) == 1
    assert captured[0] is not None
    assert len(captured[0]) == 1
    assert captured[0][0].startswith("ci_fp:")


def test_inflight_advisory_non_ci_does_not_store_labels(ctx_factory, monkeypatch):
    """Non-CI tickets never get ci_fp: labels and pass dedup_labels=None."""
    ctx = ctx_factory()
    t = _ticket(ctx, source="trace_review")  # NOT CI
    ws = ctx.service.workspace(t)

    captured: list = []

    def _spy(*a, dedup_labels=None, **k):
        captured.append(dedup_labels)
        return None

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", _spy)

    RefineStage._run_inflight_advisory(ctx, t, _DEDUP_DRAFT, ws, ctx.settings)

    assert captured == [None]
    t2 = ctx.service.get(t.id)
    # No ci_fp: labels were added.
    if t2.labels:
        import json

        labels = json.loads(t2.labels)
        assert not any(lbl.startswith("ci_fp:") for lbl in labels)


def test_inflight_advisory_ci_label_preserves_existing_labels(ctx_factory, monkeypatch):
    """When the CI ticket already has other labels, the ci_fp: label is
    appended without disturbing them."""
    import json

    ctx = ctx_factory()
    t = _ticket(ctx, source="ci")
    ctx.service.set_labels(t.id, ["priority:high", "area:ci"])
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", lambda *a, **k: None)

    RefineStage._run_inflight_advisory(ctx, t, _CI_DRAFT_BODY, ws, ctx.settings)

    t2 = ctx.service.get(t.id)
    labels = json.loads(t2.labels)
    assert "priority:high" in labels
    assert "area:ci" in labels
    ci_fp_labels = [lbl for lbl in labels if lbl.startswith("ci_fp:")]
    assert len(ci_fp_labels) == 1


def test_inflight_advisory_ci_overlap_end_to_end(ctx_factory, monkeypatch):
    """End-to-end: two CI tickets with same error fingerprint are flagged
    as duplicates; two with different fingerprints are not."""
    ctx = ctx_factory()
    # First CI ticket (the "prior").
    t1 = _ticket(ctx, title="CI failure: CI on main", source="ci")
    ws1 = ctx.service.workspace(t1)
    # Write the CI draft body.
    ws1.write_description(_CI_DRAFT_BODY)

    # Run advisory on t1 — stores its fingerprint label, no overlap yet.
    monkeypatch.setattr(dedup_top, "find_inflight_overlap", lambda *a, **k: None)
    RefineStage._run_inflight_advisory(ctx, t1, _CI_DRAFT_BODY, ws1, ctx.settings)

    # Second CI ticket with SAME error fingerprint (different run metadata).
    same_error_body = (
        _CI_DRAFT_BODY.replace("1234567890", "9999999999")
        .replace("abc123def456", "deadbeef9999")
        .replace("2024-01-15T10:30:45.123456+00:00", "2024-06-15T08:00:00Z")
    )
    t2 = _ticket(ctx, title="CI failure: CI on main", source="ci")
    ws2 = ctx.service.workspace(t2)
    ws2.write_description(same_error_body)

    # Now let find_inflight_overlap run for real (not mocked).
    monkeypatch.undo()
    # But we need to control the overlap result.  The real find_inflight_overlap
    # calls find_prior_matching_ticket which checks labels.  t1 now has the
    # ci_fp label, and t2 will get the same one via _ci_draft_fingerprint.
    # So the label should match.

    result = RefineStage._run_inflight_advisory(
        ctx, t2, same_error_body, ws2, ctx.settings
    )
    # The draft should be annotated with a warning.
    assert "[!warning]" in result
    assert t1.id in result


def test_inflight_advisory_ci_different_fingerprint_no_overlap(
    ctx_factory, monkeypatch
):
    """Two CI tickets with DIFFERENT error fingerprints do NOT flag each
    other — the title-only fallback is suppressed."""
    ctx = ctx_factory()
    # First CI ticket.
    t1 = _ticket(ctx, title="CI failure: CI on main", source="ci")
    ws1 = ctx.service.workspace(t1)
    ws1.write_description(_CI_DRAFT_BODY)

    # Run advisory on t1 — stores its fingerprint.
    saved = []

    def _capture_overlap(*a, **k):
        saved.append(k)
        return None

    monkeypatch.setattr(dedup_top, "find_inflight_overlap", _capture_overlap)
    RefineStage._run_inflight_advisory(ctx, t1, _CI_DRAFT_BODY, ws1, ctx.settings)
    monkeypatch.undo()

    # Second CI ticket with DIFFERENT error.
    different_body = """\
**Workflow:** CI
**Path:** .github/workflows/ci.yml
**Branch:** main
**Run:** [5555555555](https://github.com/owner/repo/actions/runs/5555555555)
**Commit:** `ccccddddeeee`
**Created:** 2024-06-14T22:11:33Z

Error: process completed with exit code 1.
error: missing `contents:read` permission
"""
    t2 = _ticket(ctx, title="CI failure: CI on main", source="ci")
    ws2 = ctx.service.workspace(t2)
    ws2.write_description(different_body)

    result = RefineStage._run_inflight_advisory(
        ctx, t2, different_body, ws2, ctx.settings
    )
    # No annotation — fingerprints differ, title fallback suppressed.
    assert "[!warning]" not in result


# ===========================================================================
# 7. _run_mill_misroute_gate
# ===========================================================================


def test_mill_misroute_gate_disabled_returns_none(ctx_factory):
    """Gate disabled → returns None (proceed with refine)."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = False
    t = _ticket(ctx)

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_no_absent_paths_returns_none(
    ctx_factory, monkeypatch, tmp_path
):
    """No mill paths absent from checkout → gate returns None."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: [],
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_mill_board_unconfigured_returns_none(
    ctx_factory, monkeypatch, tmp_path, caplog
):
    """resolve_mill_service returns None → gate returns None, warning logged."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: ["src/robotsix_mill/foo.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: None,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    assert any(
        "mill board not configured" in rec.message
        for rec in caplog.records
        if rec.levelname == "WARNING"
    )


def test_mill_misroute_gate_already_on_mill_board_returns_none(
    ctx_factory, monkeypatch, tmp_path
):
    """Mill board equals current board → gate returns None (no self-redirect)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: ["src/robotsix_mill/foo.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: [],
    )
    mock_svc = MagicMock()
    mock_svc.board_id = ctx.service.board_id
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_success_redirects_and_returns_done(
    ctx_factory, monkeypatch, tmp_path
):
    """Misroute detected → draft created on mill board → Outcome(State.DONE)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: ["src/robotsix_mill/foo.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: [],
    )
    mock_svc = MagicMock()
    mock_svc.board_id = "mill-board"
    mock_svc.create.return_value = MagicMock(id="mill-draft-123")
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert out.note.startswith(REFINE_MILL_MISROUTE_PREFIX)
    assert "mill-draft-123" in out.note
    mock_svc.create.assert_called_once()


def test_mill_misroute_gate_resolve_service_raises_returns_none(
    ctx_factory, monkeypatch, tmp_path
):
    """resolve_mill_service raises → gate returns None (never raises)."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: ["src/robotsix_mill/foo.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mill resolve boom")),
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_create_raises_returns_none(
    ctx_factory, monkeypatch, tmp_path
):
    """mill_svc.create raises → gate returns None (never raises)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: ["src/robotsix_mill/foo.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: [],
    )
    mock_svc = MagicMock()
    mock_svc.board_id = "mill-board"
    mock_svc.create.side_effect = RuntimeError("create boom")
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_mill_paths_exist_in_checkout_returns_none(
    ctx_factory, monkeypatch, tmp_path
):
    """Mill paths exist on disk (refine on mill repo itself) → no self-redirect."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: [],
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


# ---------------------------------------------------------------------------
# 7b. _run_mill_misroute_gate — split behavior (local deliverable)
# ---------------------------------------------------------------------------


def test_mill_misroute_gate_local_deliverable_no_redirect_no_followup(
    ctx_factory, monkeypatch, tmp_path
):
    """Draft has an out-of-scope mill path → absent=[], gate returns None
    early.  (Out-of-scope variant — mirrors verification ticket
    20260624T051045Z.)"""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    # referenced_mill_paths_absent returns [] because the mill path is
    # under an out-of-scope marker (handled by paths_excluding_out_of_scope).
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: [],
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_mixed_in_scope_local_deliverable_keeps_ticket(
    ctx_factory, monkeypatch, tmp_path
):
    """Draft creates a current-board file AND lists an in-scope absent
    mill consumer path → gate returns None; follow-up created on mill board."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths = ["src/robotsix_llmio/core/sqlite_utils.py"]

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    mock_mill_svc = MagicMock()
    mock_mill_svc.board_id = "mill-board"
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_mill_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    # Gate proceeds (no redirect).
    assert out is None
    # Follow-up ticket was created on the mill board.
    mock_mill_svc.create.assert_called_once()
    call_args = mock_mill_svc.create.call_args
    assert call_args[0][0].startswith("Consumer migration for:")
    assert "src/robotsix_mill/core/db.py" in call_args[0][1]
    assert REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX in call_args[0][1]


def test_mill_misroute_gate_local_deliverable_mill_board_unresolvable_no_followup(
    ctx_factory, monkeypatch, tmp_path
):
    """When the mill board cannot be resolved, the follow-up is skipped
    but the gate still returns None (no redirect)."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths = ["src/robotsix_llmio/core/sqlite_utils.py"]

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    # resolve_mill_service returns None → can't file follow-up.
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: None,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    # Gate still proceeds (no redirect, no crash).
    assert out is None


def test_mill_misroute_gate_local_deliverable_resolve_raises_no_followup(
    ctx_factory, monkeypatch, tmp_path
):
    """When resolve_mill_service raises, the follow-up is skipped but
    the gate still returns None."""
    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths = ["src/robotsix_llmio/core/sqlite_utils.py"]

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_local_deliverable_mill_create_raises_no_redirect(
    ctx_factory, monkeypatch, tmp_path
):
    """When mill_svc.create raises, the gate still returns None (no redirect)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths = ["src/robotsix_llmio/core/sqlite_utils.py"]

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    mock_mill_svc = MagicMock()
    mock_mill_svc.board_id = "mill-board"
    mock_mill_svc.create.side_effect = RuntimeError("create boom")
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_mill_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None


def test_mill_misroute_gate_local_deliverable_same_board_skips_followup(
    ctx_factory, monkeypatch, tmp_path
):
    """When the resolved mill board is the same as current board,
    follow-up is skipped (no self-filing)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths = ["src/robotsix_llmio/core/sqlite_utils.py"]

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    mock_mill_svc = MagicMock()
    mock_mill_svc.board_id = ctx.service.board_id  # same board
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_mill_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is None
    mock_mill_svc.create.assert_not_called()


def test_mill_misroute_gate_pure_mill_no_local_still_redirects(
    ctx_factory, monkeypatch, tmp_path
):
    """No local deliverable + absent mill paths → existing redirect
    behavior preserved: returns Outcome(State.DONE)."""
    from unittest.mock import MagicMock

    ctx = ctx_factory()
    ctx.settings.refine_mill_misroute_gate_enabled = True
    t = _ticket(ctx)

    absent_paths = ["src/robotsix_mill/core/db.py"]
    local_paths: list[str] = []

    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_mill_paths_absent",
        lambda *a, **k: absent_paths,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.referenced_local_deliverable_paths",
        lambda *a, **k: local_paths,
    )
    mock_mill_svc = MagicMock()
    mock_mill_svc.board_id = "mill-board"
    mock_mill_svc.create.return_value = MagicMock(id="mill-draft-123")
    monkeypatch.setattr(
        "robotsix_mill.stages.refine.gates.resolve_mill_service",
        lambda *a, **k: mock_mill_svc,
    )

    out = RefineStage._run_mill_misroute_gate(ctx, t, _DEDUP_DRAFT, None, ctx.settings)

    assert out is not None
    assert out.next_state is State.DONE
    assert REFINE_MILL_MISROUTE_PREFIX in out.note
    assert "mill-draft-123" in out.note
    mock_mill_svc.create.assert_called_once()


# ===========================================================================
# 8. _verify_advisory_dedup
# ===========================================================================

_ADVISORY = (
    "> [!warning] Possible duplicate of {cand_id} "
    "('Some ticket title') — matched on file path `src/foo.py`\n"
    ">\n"
    "> _Advisory flag from draft-intake pre-refine dedup; "
    "verify and close as duplicate during refine if confirmed._\n"
    "\n"
)
_BODY = "## Problem\n\nThe real draft body.\n"
_DRAFT_WITH_ADVISORY = _ADVISORY + _BODY


def test_advisory_dedup_disabled_returns_draft_unchanged(ctx_factory):
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = False
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    out = RefineStage._verify_advisory_dedup(
        ctx, t, _DRAFT_WITH_ADVISORY.format(cand_id="x"), None, ws, ctx.settings
    )
    assert out == _DRAFT_WITH_ADVISORY.format(cand_id="x")


def test_advisory_dedup_no_advisory_returns_unchanged(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    calls: list = []
    monkeypatch.setattr(agents_dedup, "run_dedup_check", lambda **kw: calls.append(1))

    out = RefineStage._verify_advisory_dedup(ctx, t, _BODY, None, ws, ctx.settings)
    assert out == _BODY
    assert calls == []  # No LLM call when no advisory


def test_advisory_dedup_candidate_not_found_strips_advisory(ctx_factory):
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id="nonexistent-id"),
        None,
        ws,
        ctx.settings,
    )
    # Advisory stripped, body returned.
    assert "Possible duplicate of" not in out
    assert _BODY in out
    # Workspace description was updated with the cleaned draft.
    assert "Possible duplicate of" not in ws.read_description()
    assert _BODY in ws.read_description()


def test_advisory_dedup_unrefined_draft_target_strips_advisory(
    ctx_factory, monkeypatch
):
    """When the candidate is an un-refined DRAFT (invalid dedup target),
    the advisory is stripped and refine proceeds."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    # Candidate is an un-refined DRAFT — valid dedup target check fails.
    cand = _ticket(ctx, title="Some candidate", body=_BODY)
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=cand.id, already_done=None, reason="same idea"),
    )

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id=cand.id),
        None,
        ws,
        ctx.settings,
    )
    # Should strip advisory, not be an Outcome.
    assert not isinstance(out, Outcome)
    assert "Possible duplicate of" not in out
    assert _BODY in out


def test_advisory_dedup_confirmed_duplicate_short_circuits(ctx_factory, monkeypatch):
    """When the cheap check returns duplicate_of a valid target,
    the gate returns Outcome(State.DONE, ...)."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    # Candidate: a DONE ticket with a merged branch (valid target).
    cand = _ticket(ctx, title="Already shipped", body=_BODY)
    ctx.service.set_branch(cand.id, "feature/already-shipped")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=cand.id, already_done=None, reason="same idea"),
    )
    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id=cand.id),
        None,
        ws,
        ctx.settings,
    )
    assert isinstance(out, Outcome)
    assert out.next_state is State.DONE
    assert out.note.startswith(DEDUP_DUPLICATE_PREFIX)
    assert cand.id in out.note


def test_advisory_dedup_confirmed_already_done_short_circuits(ctx_factory, monkeypatch):
    """When the cheap check returns already_done for a valid target,
    the gate returns Outcome(State.DONE, ...)."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Already done", body=_BODY)
    ctx.service.set_branch(cand.id, "feature/already-done")
    ctx.service.transition(cand.id, State.DONE, note="implemented the thing")
    cand = ctx.service.get(cand.id)
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=cand.id, reason="commit found"),
    )
    monkeypatch.setattr(
        refine_module, "_verify_branch_merged", lambda repo_dir, ticket: True
    )

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id=cand.id),
        None,
        ws,
        ctx.settings,
    )
    assert isinstance(out, Outcome)
    assert out.next_state is State.DONE
    assert out.note.startswith(DEDUP_ALREADY_DONE_PREFIX)
    assert cand.id in out.note


def test_advisory_dedup_no_match_strips_advisory(ctx_factory, monkeypatch):
    """When the cheap check returns no duplicate_of/already_done,
    the advisory is stripped and refine proceeds."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Other thing", body="Something else entirely")
    ws = ctx.service.workspace(t)

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id=cand.id),
        None,
        ws,
        ctx.settings,
    )
    assert not isinstance(out, Outcome)
    assert "Possible duplicate of" not in out
    assert _BODY in out
    # Workspace was persisted.
    assert "Possible duplicate of" not in ws.read_description()


def test_advisory_dedup_check_raises_strips_advisory(ctx_factory, monkeypatch):
    """When run_dedup_check raises, the advisory is stripped and refine proceeds."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Some candidate", body=_BODY)
    ws = ctx.service.workspace(t)

    def _boom(**kw):
        raise RuntimeError("dedup boom")

    monkeypatch.setattr(agents_dedup, "run_dedup_check", _boom)

    out = RefineStage._verify_advisory_dedup(
        ctx,
        t,
        _DRAFT_WITH_ADVISORY.format(cand_id=cand.id),
        None,
        ws,
        ctx.settings,
    )
    assert not isinstance(out, Outcome)
    assert "Possible duplicate of" not in out
    assert _BODY in out


def test_advisory_dedup_outer_exception_returns_draft_unchanged(
    ctx_factory, monkeypatch
):
    """When something else raises (e.g. ctx.service.get raises),
    the gate returns the draft unchanged so refine always proceeds."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    draft = _DRAFT_WITH_ADVISORY.format(cand_id="will-boom")

    def _boom_get(tid):
        raise RuntimeError("service boom")

    monkeypatch.setattr(ctx.service, "get", _boom_get)

    out = RefineStage._verify_advisory_dedup(ctx, t, draft, None, ws, ctx.settings)
    # Best-effort: returns draft unchanged.
    assert out == draft


def test_advisory_dedup_draft_without_leading_blank_line_after_block(
    ctx_factory, monkeypatch
):
    """The advisory strip handles the case where there's no blank line
    between the blockquote and the body."""
    ctx = ctx_factory()
    ctx.settings.refine_advisory_dedup_enabled = True
    t = _ticket(ctx)
    cand = _ticket(ctx, title="Other thing", body="Something else entirely")
    ws = ctx.service.workspace(t)

    # No blank line between blockquote and body.
    draft_no_blank = (
        "> [!warning] Possible duplicate of {cand_id} "
        "('Some ticket title') — matched on file path `src/foo.py`\n"
        ">\n"
        "> _Advisory flag from draft-intake pre-refine dedup; "
        "verify and close as duplicate during refine if confirmed._\n"
        "## Problem\n\nThe real draft body.\n"
    ).format(cand_id=cand.id)

    monkeypatch.setattr(
        agents_dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )

    out = RefineStage._verify_advisory_dedup(
        ctx, t, draft_no_blank, None, ws, ctx.settings
    )
    assert not isinstance(out, Outcome)
    assert "Possible duplicate of" not in out
    assert "The real draft body" in out

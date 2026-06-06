"""Tests for the RefineStage (src/robotsix_mill/stages/refine.py).

Coverage: 30 test functions exercising every branch of the DRAFT→READY
pipeline — empty drafts, unmet deps, dedup short-circuits, clone
failure fallback, successful refine (autonomous & gated), auto-approve
triage, refine triage skip, split children, spec review, epic body
handling, and memory load/persist.  Mock seams follow the same
convention as test_implement.py.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from robotsix_mill.agents import dedup
from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    AutoApproveResult,
    ChildSpec,
    RefineResult,
    SpecReviewResult,
    TriageResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.vcs import git_ops


# ---------------------------------------------------------------------------
# fixtures
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


def _ticket(ctx, title="Add feature", body=None):
    """Create a DRAFT ticket — the RefineStage input state. The default
    body is comfortably above the 100-char trivial-draft threshold so
    refine's dedup pipeline actually runs (matches test_refine.py's
    _DEDUP_BODY convention). An explicit empty string is preserved (some
    tests intentionally exercise the empty-title-and-draft block path)."""
    if body is None:
        body = (
            "Add a feature. This is a substantive draft body padded "
            "past the 100-char trivial-draft threshold so refine's "
            "dedup pipeline actually runs against this ticket."
        )
    elif body and len(body) < 100:
        # Pad non-empty short test bodies up to a substantive size.
        body = (
            f"{body}. This is a substantive draft body padded past "
            "the 100-char trivial-draft threshold so refine's dedup "
            "pipeline actually runs against this ticket."
        )
    return ctx.service.create(title, body)


# ---------------------------------------------------------------------------
# helpers: mock seams
# ---------------------------------------------------------------------------


def _mock_refine_ok(spec_markdown="## Problem\nFix it", **overrides):
    """Return a *callable* that returns a canned RefineResult."""

    def _run(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        **kw,
    ):
        del (
            settings,
            title,
            draft,
            repo_dir,
            reviewer_comments,
            memory,
            epic_context,
            kw,
        )
        kwargs = dict(spec_markdown=spec_markdown)
        kwargs.update(overrides)
        return RefineResult(**kwargs)

    return _run


def _mock_refine_raises(exc):
    def _run(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        **kw,
    ):
        del (
            settings,
            title,
            draft,
            repo_dir,
            reviewer_comments,
            memory,
            epic_context,
            kw,
        )
        raise exc

    return _run


def _mock_dedup(**verdict):
    def _run(
        *, settings, draft_title, draft_body, candidates_json, repo_dir=None, **kw
    ):
        del settings, draft_title, draft_body, candidates_json, repo_dir, kw
        return verdict

    return _run


def _mock_triage_refine(decision="REFINE", reason="needs refinement"):
    def _run(*, settings, title, draft, **kw):
        del settings, title, draft, kw
        return TriageResult(decision=decision, reason=reason)

    return _run


def _mock_auto_approve(decision="NEEDS_APPROVAL", reason="design decision present"):
    def _run(*, settings, spec, **kw):
        del settings, spec, kw
        return AutoApproveResult(decision=decision, reason=reason)

    return _run


def _mock_spec_review(concise_spec="## concise", stripped_summary="stripped 3 lines"):
    def _run(*, settings, spec_markdown, **kw):
        del settings, spec_markdown, kw
        return SpecReviewResult(
            concise_spec=concise_spec, stripped_summary=stripped_summary
        )

    return _run


def _apply_default_mocks(monkeypatch, **overrides):
    """Apply all mock seams with sensible defaults so the happy refine
    path works out of the box.  Individual tests override specific mocks
    as needed."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        overrides.get("run_refine_agent", _mock_refine_ok()),
    )
    monkeypatch.setattr(
        refining, "triage_refine", overrides.get("triage_refine", _mock_triage_refine())
    )
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        overrides.get("triage_auto_approve", _mock_auto_approve()),
    )
    monkeypatch.setattr(
        refining,
        "review_spec_for_conciseness",
        overrides.get("review_spec_for_conciseness", _mock_spec_review()),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        overrides.get(
            "run_dedup_check",
            _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
        ),
    )
    monkeypatch.setattr(git_ops, "clone", overrides.get("clone", lambda *a, **k: None))
    monkeypatch.setattr(
        refine_module,
        "_verify_branch_merged",
        overrides.get("_verify_branch_merged", lambda repo_dir, ticket: True),
    )
    monkeypatch.setattr(
        refine_module,
        "load_memory",
        overrides.get("load_memory", lambda memory_file, max_chars=None: ""),
    )
    monkeypatch.setattr(
        refine_module,
        "persist_memory",
        overrides.get("persist_memory", lambda memory_file, text: None),
    )


# ---------------------------------------------------------------------------
# 1. empty title and draft
# ---------------------------------------------------------------------------


def test_empty_title_and_draft_blocks(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx, title="", body="")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "empty" in out.note


# ---------------------------------------------------------------------------
# 2. unmet dependencies → same-state no-op
# ---------------------------------------------------------------------------


def test_unmet_dependencies_noop(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    dep = ctx.service.create("Dep ticket", "Blocking change")
    t = ctx.service.create("Depender", "Please fix", depends_on=f'["{dep.id}"]')

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DRAFT
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 3. dedup: duplicate → DONE
# ---------------------------------------------------------------------------


def test_dedup_duplicate_short_circuits_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, body="Fix the login form")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
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
# 4. dedup: already done → DONE
# ---------------------------------------------------------------------------


def test_dedup_already_done_short_circuits_to_done(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, body="Add dark mode toggle")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
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
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")

    prior = ctx.service.create(
        "rework login validation",
        # The candidate declares the shared path under ``## Scope`` so a
        # lone shared path still flags under the strict-scope rule.
        "## Scope\n\nchanges src/robotsix_mill/auth.py to validate the login form",
    )
    ctx.service.transition(prior.id, State.READY, note="refined")

    t = _ticket(
        ctx,
        title="fix login form validation",
        body=(
            "Fix the login form. This also edits "
            "src/robotsix_mill/auth.py to validate input, padded well past "
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


# ---------------------------------------------------------------------------
# 7. clone failure → draft-only refine succeeds
# ---------------------------------------------------------------------------


def test_clone_failure_escalates_to_blocked_with_history_note(ctx_factory, monkeypatch):
    """A clone failure escalates to BLOCKED. The diagnostic + remediation
    hint land in the transition note (history) — v1 moved agent
    conclusions out of comments to keep comments reserved for ASK_USER
    + review threads."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///nonexistent", require_approval="false")
    t = _ticket(ctx, body="Add endpoint")

    _apply_default_mocks(
        monkeypatch,
        clone=lambda remote_url, dest, branch, token: (_ for _ in ()).throw(
            subprocess.CalledProcessError(
                1, "git", stderr=b"fatal: repository not found"
            )
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "refine clone failed" in (out.note or "")
    assert "resume-blocked" in (out.note or "")
    # No agent-authored comment.
    comments = ctx.service.list_comments(t.id)
    assert not any(c.author == "refine" for c in comments)


# ---------------------------------------------------------------------------
# 8. successful refine → READY (autonomous)
# ---------------------------------------------------------------------------


def test_successful_refine_to_ready_autonomous(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, title="Fix logout", body="The logout button does nothing")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nFix logout"),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = ctx.service.workspace(t)
    assert "## Problem" in ws.read_description()
    assert (ws.artifacts_dir / "draft-original.md").exists()
    # set_title NOT called — title unchanged
    assert ctx.service.get(t.id).title == "Fix logout"


# ---------------------------------------------------------------------------
# 9. successful refine with title override
# ---------------------------------------------------------------------------


def test_successful_refine_with_title_override(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, title="Fix thing", body="The logout button does nothing")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## P", title="Better Title"),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert ctx.service.get(t.id).title == "Better Title"


# ---------------------------------------------------------------------------
# 10. successful refine → HUMAN_ISSUE_APPROVAL (gated, auto-approve off)
# ---------------------------------------------------------------------------


def test_successful_refine_to_human_issue_approval_gated(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="false",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Implement the thing")

    _apply_default_mocks(
        monkeypatch, run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nFix")
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


# ---------------------------------------------------------------------------
# 11. auto-approve: APPROVE → READY
# ---------------------------------------------------------------------------


def test_auto_approve_approve_routes_to_ready(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Add a docstring to utils.py")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nAdd docstring"),
        triage_auto_approve=_mock_auto_approve(
            decision="APPROVE", reason="no design decisions"
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "auto-approve: APPROVE" in out.note


# ---------------------------------------------------------------------------
# 11b. auto-approve: test-gap source short-circuits to READY without LLM
# ---------------------------------------------------------------------------


def test_auto_approve_test_gap_source_short_circuits_to_ready(
    ctx_factory,
    monkeypatch,
):
    """test_gap-sourced tickets must auto-approve deterministically and
    must NOT invoke the LLM triage. Test-gap tickets only add coverage
    so there's no design risk a human reviewer can meaningfully veto;
    three triage runs on 2026-05-28 all fell back to human and were
    rubber-stamped."""
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="true",
        refine_triage_enabled="false",
    )
    t = ctx.service.create(
        "Add unit tests for foo.py",
        "Add unit tests for foo.py covering the bar branch — substantive "
        "body padded past the trivial-draft threshold so refine actually "
        "runs the auto-approve gate.",
        source="test_gap",
    )

    triage_calls: list = []

    def fail_if_called(**_):
        triage_calls.append(True)
        raise AssertionError("triage_auto_approve must not be called for test_gap")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nAdd tests"),
        triage_auto_approve=fail_if_called,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "auto-approve: APPROVE" in out.note
    assert "test_gap" in out.note


def test_auto_approve_audit_source_also_short_circuits(
    ctx_factory,
    monkeypatch,
):
    """Round 3: audit, agent_check, bc_check, completeness_check,
    module_curator, copy_paste join test_gap as deterministic
    auto-approve sources. These are mill-internal periodic agents
    whose drafts are dead-code / prompt / config / docstring
    cleanups — historically every one was rubber-stamped without
    rejection, so the LLM triage was pure toil."""
    for source in (
        "audit",
        "agent_check",
        "bc_check",
        "completeness_check",
        "module_curator",
        "copy_paste",
    ):
        ctx = ctx_factory(
            require_approval="true",
            auto_approve_enabled="true",
            refine_triage_enabled="false",
        )
        t = ctx.service.create(
            f"{source} proposal",
            "Substantive ticket body padded above the trivial-draft "
            "threshold so refine actually exercises the auto-approve "
            "gate against the source-based rule.",
            source=source,
        )
        triage_calls: list = []

        def fail_if_called(**_):
            triage_calls.append(True)
            raise AssertionError(
                f"triage_auto_approve must not be called for {source}",
            )

        _apply_default_mocks(
            monkeypatch,
            run_refine_agent=_mock_refine_ok(
                spec_markdown="## Problem\nDo a thing",
            ),
            triage_auto_approve=fail_if_called,
        )

        out = RefineStage().run(t, ctx)
        assert out.next_state is State.READY, (
            f"{source}: expected READY, got {out.next_state}"
        )
        assert source in (out.note or ""), out.note
        assert triage_calls == []
    assert triage_calls == []


# ---------------------------------------------------------------------------
# 12. auto-approve: NEEDS_APPROVAL → HUMAN_ISSUE_APPROVAL
# ---------------------------------------------------------------------------


def test_auto_approve_needs_approval_routes_to_human(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Redesign the auth module")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nRedesign auth"),
        triage_auto_approve=_mock_auto_approve(
            decision="NEEDS_APPROVAL", reason="new API design"
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "auto-approve: NEEDS_APPROVAL" in out.note


# ---------------------------------------------------------------------------
# 13. auto-approve triage failure → fallback to human
# ---------------------------------------------------------------------------


def test_auto_approve_triage_failure_falls_back_to_human(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Update config defaults")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nUpdate config"),
        triage_auto_approve=lambda *a, **k: (_ for _ in ()).throw(
            Exception("LLM timeout")
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "triage failed" in out.note


# ---------------------------------------------------------------------------
# 14. refine triage SKIP → bypasses agent
# ---------------------------------------------------------------------------


def test_refine_triage_skip_bypasses_agent(ctx_factory, monkeypatch):
    """When triage returns SKIP and the draft contains backtick-quoted
    file paths, the refine agent is bypassed and those paths are written
    to file_map.json (fast path preserved)."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    t = _ticket(ctx, body="Add docstring to foo() in `src/bar.py`")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
    monkeypatch.setattr(
        refining,
        "triage_refine",
        _mock_triage_refine(decision="SKIP", reason="already precise"),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(agent_called) == 0
    assert "triage SKIP" in out.note
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    # Fast path: file_map.json was written from extracted paths.
    file_map_path = ws.artifacts_dir / "file_map.json"
    assert file_map_path.exists()
    file_map = json.loads(file_map_path.read_text(encoding="utf-8"))
    assert len(file_map) == 1
    assert file_map[0]["file"] == "src/bar.py"
    assert file_map[0]["note"] == "from draft"


# ---------------------------------------------------------------------------
# 14b. refine triage SKIP + no paths → falls through to refine agent
# ---------------------------------------------------------------------------


def test_refine_triage_skip_no_paths_falls_through_to_refine(ctx_factory, monkeypatch):
    """When triage returns SKIP but the draft has no backtick-quoted
    file paths, do NOT write an empty file_map — fall through to the
    refine agent instead so it can produce a proper file_map."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    # Draft with no backtick-quoted paths (bare filename with no
    # directory separator won't match the regex).
    t = _ticket(ctx, body="Add docstring to foo() in bar.py")

    refine_called = []
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda *a, **k: (
            refine_called.append(1),
            RefineResult(
                spec_markdown="## Problem\nDone",
                file_map=[refining.FileMapEntry(file="src/bar.py", note="main module")],
            ),
        )[-1],
    )
    monkeypatch.setattr(
        refining,
        "triage_refine",
        _mock_triage_refine(decision="SKIP", reason="already precise"),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    # Refine agent WAS called — not bypassed.
    assert len(refine_called) == 1
    assert out.next_state is State.READY
    ws = ctx.service.workspace(t)
    # file_map.json was written by the refine agent, not an empty [].
    file_map_path = ws.artifacts_dir / "file_map.json"
    assert file_map_path.exists()
    file_map = json.loads(file_map_path.read_text(encoding="utf-8"))
    assert len(file_map) == 1
    assert file_map[0]["file"] == "src/bar.py"


# ---------------------------------------------------------------------------
# 15. refine triage exception → fall through to full refine
# ---------------------------------------------------------------------------


def test_refine_triage_exception_falls_through_to_full_refine(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    t = _ticket(ctx, body="Fix the thing")

    refine_called = []
    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda *a, **k: (_ for _ in ()).throw(Exception("timeout")),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )

    def _refine(*a, **k):
        refine_called.append(1)
        return RefineResult(spec_markdown="## Problem\nDone")

    monkeypatch.setattr(refining, "run_refine_agent", _refine)
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(refine_called) == 1


# ---------------------------------------------------------------------------
# 16. refine agent RuntimeError → BLOCKED
# ---------------------------------------------------------------------------


def test_refine_agent_runtime_error_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory(refine_triage_enabled="false")
    t = _ticket(ctx, body="Fix the thing")

    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        _mock_refine_raises(RuntimeError("OPENROUTER_API_KEY is not set")),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY" in out.note


# ---------------------------------------------------------------------------
# 17. refiner empty spec → fallback (kept original draft)
# ---------------------------------------------------------------------------


def test_refiner_empty_spec_falls_back_to_draft(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Original draft body")

    _apply_default_mocks(
        monkeypatch, run_refine_agent=_mock_refine_ok(spec_markdown="")
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "kept original draft" in out.note


# ---------------------------------------------------------------------------
# 18. refiner None spec → fallback
# ---------------------------------------------------------------------------


def test_refiner_none_spec_falls_back(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Original draft body")

    def _refine_none(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        **kw,
    ):
        del (
            settings,
            title,
            draft,
            repo_dir,
            reviewer_comments,
            memory,
            epic_context,
            kw,
        )
        return RefineResult(spec_markdown=None)

    _apply_default_mocks(monkeypatch, run_refine_agent=_refine_none)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "kept original draft" in out.note


# ---------------------------------------------------------------------------
# 18b. refiner placeholder spec ("(see spec above)") → fallback, no clobber
# ---------------------------------------------------------------------------


def test_refiner_placeholder_spec_falls_back_to_draft(ctx_factory, monkeypatch):
    """Regression: a refiner that returns a placeholder pointer like
    "(see spec above)" must NOT overwrite description.md with it — the
    placeholder blanked the ticket body on the board. Refine treats it as
    no-spec and keeps the original draft."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Original draft body")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="(see spec above)"),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "kept original draft" in out.note
    desc = ctx.service.workspace(t).read_description()
    assert "see spec above" not in desc.lower()
    assert "Original draft body" in desc


# ---------------------------------------------------------------------------
# 18c. _spec_is_degenerate unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "\n\n",
        "(see spec above)",
        "See spec above.",
        "see above",
        "**see the spec above**",
        "_(as written above)_",
        "> see description",
        "TBD",
        "TODO",
    ],
)
def test_spec_is_degenerate_true(spec):
    assert refine_module._spec_is_degenerate(spec) is True


@pytest.mark.parametrize(
    "spec",
    [
        "## Problem\nThe logout button does nothing. Fix the handler.",
        "Add a docstring to utils.py describing the return value.",
        # Starts with a pointer phrase but is a real, long spec — the
        # length cap must keep it.
        "see the spec above and then implement the new caching layer with "
        "an LRU eviction policy and a configurable max size honoring env.",
        # "above" not used as a pointer.
        "above all, the function must validate its inputs before use",
    ],
)
def test_spec_is_degenerate_false(spec):
    assert refine_module._spec_is_degenerate(spec) is False


# ---------------------------------------------------------------------------
# 19. split child shortcut → detected and resolved
# ---------------------------------------------------------------------------


def test_split_child_shortcut_detected_and_resolved(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false")
    parent = ctx.service.create("Epic parent", "Split me", kind="epic")

    # Directly set parent to CLOSED with a "split into" history event.
    from robotsix_mill.core.models import TicketEvent, Ticket as TicketModel
    from robotsix_mill.core.db import session as db_session
    from datetime import datetime, timezone

    with db_session(ctx.settings, "test-board") as sess:
        tm = sess.get(TicketModel, parent.id)
        tm.state = State.CLOSED.value
        evt = TicketEvent(
            ticket_id=parent.id,
            state=State.CLOSED.value,
            note="split into child-aaa, child-bbb",
            at=datetime.now(timezone.utc),
        )
        sess.add(evt)
        sess.commit()

    child = ctx.service.create(
        "Child ticket", "## Problem\nAlready refined spec", parent_id=parent.id
    )

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )

    out = RefineStage().run(child, ctx)

    assert out.next_state is State.READY
    assert "split child" in out.note
    assert len(agent_called) == 0


# ---------------------------------------------------------------------------
# 20. split child empty description → BLOCKED
# ---------------------------------------------------------------------------


def test_split_child_empty_description_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    parent = ctx.service.create("Epic parent", "Split me", kind="epic")

    # Directly set parent to CLOSED with a "split into" history event.
    from robotsix_mill.core.models import TicketEvent, Ticket as TicketModel
    from robotsix_mill.core.db import session as db_session
    from datetime import datetime, timezone

    with db_session(ctx.settings, "test-board") as sess:
        tm = sess.get(TicketModel, parent.id)
        tm.state = State.CLOSED.value
        evt = TicketEvent(
            ticket_id=parent.id,
            state=State.CLOSED.value,
            note="split into child-aaa, child-bbb",
            at=datetime.now(timezone.utc),
        )
        sess.add(evt)
        sess.commit()

    child = ctx.service.create("Child ticket", "", parent_id=parent.id)

    out = RefineStage().run(child, ctx)

    assert out.next_state is State.BLOCKED
    assert "empty description" in out.note


# ---------------------------------------------------------------------------
# 21. successful split → creates children and closes parent
# ---------------------------------------------------------------------------


def test_successful_split_creates_children_and_closes_parent(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Big feature: rewrite auth AND add dashboard")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown="## Aggregated spec",
            title="Umbrella Epic Title",
            children=[
                ChildSpec(title="Part A", spec_markdown="## A"),
                ChildSpec(title="Part B", spec_markdown="## B"),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "split into" in out.note

    all_tickets = ctx.service.list()
    # Find the umbrella epic that was created.
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    epic = epics[0]
    assert epic.title == "Umbrella Epic Title"
    assert epic.state is State.EPIC_OPEN
    # Children are parented to the new epic, not the original ticket.
    children = [tk for tk in all_tickets if tk.parent_id == epic.id]
    assert len(children) == 2
    for child in children:
        assert child.state is State.READY
    # Both child IDs appear in the note
    for child in children:
        assert child.id in out.note
    # No children parented to the original (closed) ticket.
    orphaned = [tk for tk in all_tickets if tk.parent_id == t.id]
    assert len(orphaned) == 0


# ---------------------------------------------------------------------------
# 22. split with depends_on → resolves indices to real IDs
# ---------------------------------------------------------------------------


def test_split_with_depends_on_resolves_indices(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Big feature")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown=None,
            children=[
                ChildSpec(title="Base", spec_markdown="## base"),
                ChildSpec(title="On top", spec_markdown="## top", depends_on=[0]),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    all_tickets = ctx.service.list()
    # Find the umbrella epic and get its children.
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    epic = epics[0]
    children = sorted(
        [tk for tk in all_tickets if tk.parent_id == epic.id],
        key=lambda tk: tk.created_at,
    )
    assert len(children) == 2
    base_id, top_id = children[0].id, children[1].id
    # The second child's depends_on should reference the first
    top_deps = json.loads(ctx.service.get(top_id).depends_on or "[]")
    assert top_deps == [base_id]


# ---------------------------------------------------------------------------
# 23. split with no valid children → BLOCKED
# ---------------------------------------------------------------------------


def test_split_no_valid_children_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory(refine_triage_enabled="false")
    t = _ticket(ctx, body="Big feature")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown=None,
            children=[
                ChildSpec(title="", spec_markdown=""),
                ChildSpec(title="Also bad", spec_markdown=""),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "no valid split children" in out.note


# ---------------------------------------------------------------------------
# 24. split with single valid child → falls back to single spec
# ---------------------------------------------------------------------------


def test_split_single_valid_child_falls_back_to_single_spec(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="One thing")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown=None,
            children=[ChildSpec(title="Only one", spec_markdown="## valid")],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "single child, no split" in out.note
    # No child tickets created
    all_tickets = ctx.service.list()
    assert len(all_tickets) == 1  # only the original
    # Original ticket's description is the child's spec_markdown
    assert ctx.service.workspace(t).read_description() == "## valid"


# ---------------------------------------------------------------------------
# 25. spec review conciseness pass
# ---------------------------------------------------------------------------


def test_spec_review_conciseness_pass(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="false",
        refine_triage_enabled="false",
        spec_review_enabled="true",
    )
    t = _ticket(ctx, body="Do the change")

    verbose = "## Problem\nVerbose spec with exploration narrative\n\nI found that..."
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown=verbose),
        review_spec_for_conciseness=_mock_spec_review(
            concise_spec="## short",
            stripped_summary="removed 3 lines",
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = ctx.service.workspace(t)
    assert ws.read_description() == "## short"
    assert (ws.artifacts_dir / "refine-verbose.md").read_text() == verbose


# ---------------------------------------------------------------------------
# 26. spec review failure → uses verbose spec
# ---------------------------------------------------------------------------


def test_spec_review_failure_uses_verbose_spec(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="false",
        refine_triage_enabled="false",
        spec_review_enabled="true",
    )
    t = _ticket(ctx, body="Do the change")

    verbose = "## Problem\nOriginal verbose spec"
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown=verbose),
        review_spec_for_conciseness=lambda *a, **k: (_ for _ in ()).throw(
            Exception("timeout")
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert ctx.service.workspace(t).read_description() == verbose


# ---------------------------------------------------------------------------
# 27. epic body applied in autonomous mode
# ---------------------------------------------------------------------------


def test_epic_body_applied_in_autonomous_mode(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    epic = ctx.service.create("Epic", "Original epic goal", kind="epic")
    child = ctx.service.create("Child", "Do part of epic", parent_id=epic.id)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            spec_markdown="## P",
            epic_body="Updated epic goal",
        ),
    )

    out = RefineStage().run(child, ctx)

    assert out.next_state is State.READY
    assert "Updated epic goal" in ctx.service.workspace(epic).read_description()


# ---------------------------------------------------------------------------
# 28. epic body stored as artifact in gated mode
# ---------------------------------------------------------------------------


def test_epic_body_stored_as_artifact_in_gated_mode(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        auto_approve_enabled="false",
        refine_triage_enabled="false",
    )
    epic = ctx.service.create("Epic", "Original epic goal", kind="epic")
    child = ctx.service.create("Child", "Do part of epic", parent_id=epic.id)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            spec_markdown="## P",
            epic_body="Updated epic goal",
        ),
    )

    out = RefineStage().run(child, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    # Epic unchanged
    assert "Original epic goal" in ctx.service.workspace(epic).read_description()
    # Artifact written
    ws = ctx.service.workspace(child)
    artifact = ws.artifacts_dir / "epic-body-proposed.md"
    assert artifact.exists()
    assert artifact.read_text() == "Updated epic goal"


# ---------------------------------------------------------------------------
# 29. memory load and persist cycle
# ---------------------------------------------------------------------------


def test_memory_load_and_persist_cycle(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Fix the widget")

    persisted: list[str] = []
    monkeypatch.setattr(
        refine_module,
        "load_memory",
        lambda memory_file, max_chars=None: "prior knowledge",
    )
    monkeypatch.setattr(
        refine_module,
        "persist_memory",
        lambda memory_file, text: persisted.append(text),
    )
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        _mock_refine_ok(spec_markdown="## P", updated_memory="new knowledge"),
    )
    monkeypatch.setattr(refining, "triage_refine", _mock_triage_refine())
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(persisted) == 1
    assert persisted[0] == "new knowledge"


# ---------------------------------------------------------------------------
# 30. no forge remote URL → skips clone
# ---------------------------------------------------------------------------


def test_no_forge_remote_url_skips_clone(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="Fix the widget")

    clone_calls = []

    _apply_default_mocks(
        monkeypatch,
        clone=lambda remote_url, dest, branch, token: clone_calls.append(1),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(clone_calls) == 0


# ---------------------------------------------------------------------------
# 31. split creates umbrella epic with title from result.title
# ---------------------------------------------------------------------------


def test_split_creates_umbrella_epic_with_result_title(ctx_factory, monkeypatch):
    """When no epic parent exists, a new umbrella epic is created and
    children are reparented to it."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, title="Big refactor", body="Rewrite auth and add dashboard")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown="## Aggregated\nBoth changes together",
            title="Auth + Dashboard Overhaul",
            children=[
                ChildSpec(title="Part A", spec_markdown="## A"),
                ChildSpec(title="Part B", spec_markdown="## B"),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED
    assert "split into" in out.note

    all_tickets = ctx.service.list()
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    epic = epics[0]
    assert epic.title == "Auth + Dashboard Overhaul"
    assert epic.state is State.EPIC_OPEN
    assert "Both changes together" in ctx.service.workspace(epic).read_description()

    children = [tk for tk in all_tickets if tk.parent_id == epic.id]
    assert len(children) == 2
    for child in children:
        assert child.state is State.READY


# ---------------------------------------------------------------------------
# 32. split epic title fallback — uses ticket title when result.title is None
# ---------------------------------------------------------------------------


def test_split_epic_title_fallback_to_ticket_title(ctx_factory, monkeypatch):
    """When result.title is None or empty, the epic title = original ticket title."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, title="Refactor core modules", body="Break this up")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown="## Plan\nDo A then B",
            title=None,  # explicitly None
            children=[
                ChildSpec(title="Part A", spec_markdown="## A"),
                ChildSpec(title="Part B", spec_markdown="## B"),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    all_tickets = ctx.service.list()
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    assert epics[0].title == "Refactor core modules"


# ---------------------------------------------------------------------------
# 33. split epic description fallback — uses draft when spec_markdown is empty
# ---------------------------------------------------------------------------


def test_split_epic_description_fallback_to_draft(ctx_factory, monkeypatch):
    """When result.spec_markdown is empty/None, epic description = original draft."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    draft_body = "Original draft: big feature request"
    t = _ticket(ctx, title="Big feature", body=draft_body)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown=None,  # no aggregate spec
            title="Feature Epic",
            children=[
                ChildSpec(title="Part A", spec_markdown="## A"),
                ChildSpec(title="Part B", spec_markdown="## B"),
            ],
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.CLOSED

    all_tickets = ctx.service.list()
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    epic = epics[0]
    assert epic.title == "Feature Epic"
    assert draft_body in ctx.service.workspace(epic).read_description()


# ---------------------------------------------------------------------------
# 34. split with existing epic parent — children reparented to it
# ---------------------------------------------------------------------------


def test_split_with_existing_epic_reparents_children(ctx_factory, monkeypatch):
    """When the ticket already belongs to an epic, children are reparented
    to the existing epic — no new epic is created."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    existing_epic = ctx.service.create("Existing Epic", "Epic description", kind="epic")
    child_of_epic = ctx.service.create(
        "Split me",
        "Break into parts",
        parent_id=existing_epic.id,
    )

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            split=True,
            spec_markdown="## Aggregated spec",
            title="Should Be Ignored",
            epic_body="Updated epic body",
            children=[
                ChildSpec(title="Part A", spec_markdown="## A"),
                ChildSpec(title="Part B", spec_markdown="## B"),
            ],
        ),
    )

    out = RefineStage().run(child_of_epic, ctx)

    assert out.next_state is State.CLOSED
    assert "split into" in out.note

    all_tickets = ctx.service.list()
    # No new epic created — only the existing one.
    epics = [tk for tk in all_tickets if tk.kind == "epic"]
    assert len(epics) == 1
    assert epics[0].id == existing_epic.id

    # Children are parented to the existing epic.
    children = [tk for tk in all_tickets if tk.parent_id == existing_epic.id]
    # The children + the original child_of_epic (which is now CLOSED).
    assert len(children) == 3  # original child + 2 new children
    new_children = [tk for tk in children if tk.id != child_of_epic.id]
    assert len(new_children) == 2
    for child in new_children:
        assert child.state is State.READY

    # Epic body write-back fired: the existing epic's description was updated.
    assert (
        "Updated epic body" in ctx.service.workspace(existing_epic).read_description()
    )

    # Original (closed) ticket has no children parented to it.
    orphaned = [tk for tk in all_tickets if tk.parent_id == child_of_epic.id]
    assert len(orphaned) == 0


# ---------------------------------------------------------------------------
# 30. promote_to_epic: refine converts ticket to epic and spawns children
# ---------------------------------------------------------------------------


def test_promote_to_epic_converts_and_spawns_children(ctx_factory, monkeypatch):
    """When refine returns ``promote_to_epic=true`` with an ``epic_body``,
    the stage:

    - flips the ticket's kind to ``epic`` (via service.promote_to_epic);
    - transitions DRAFT → EPIC_OPEN;
    - writes the epic_body to the workspace description;
    - synchronously invokes the epic-breakdown agent;
    - creates child tickets parented to THIS ticket (no umbrella copy);
    - wires a linear dependency chain across the children.

    This is the path the b2ac one-shot-migration ticket should have
    taken: the spec was manifest-driven (docs/modules.yaml lists 19
    items) and each per-module child needs its own deep spec, so refine
    should promote rather than inline-split."""
    from robotsix_mill.agents import epic_breakdown as _ebreak

    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(
        ctx,
        title="Reorganize repo into modular layout",
        body="For each module in docs/modules.yaml, create the parallel directories...",
    )

    class _FakeBreakdown:
        def __init__(self):
            self.child_titles = [
                "Migrate runners",
                "Migrate langfuse",
                "Migrate notify",
            ]
            self.child_bodies = [
                "## Migrate runners\n...",
                "## Migrate langfuse\n...",
                "## Migrate notify\n...",
            ]
            self.epic_body = "## Epic: modular layout migration\n..."

    monkeypatch.setattr(
        _ebreak,
        "run_epic_breakdown_agent",
        lambda **kw: _FakeBreakdown(),
    )

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            promote_to_epic=True,
            epic_body="## Strategic epic body: per-module migration",
            spec_markdown=None,
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.EPIC_OPEN
    assert "promoted to epic" in out.note
    assert "3 child" in out.note  # 3 children spawned

    # State transition is the worker's job (runs after stage.run);
    # the stage just flips ``kind`` synchronously and reports the next
    # state via the Outcome. Assert on the kind here; the EPIC_OPEN
    # state assertion lives on out.next_state above.
    promoted = ctx.service.get(t.id)
    assert promoted.kind == "epic"

    # Children parented to the promoted ticket (NOT an umbrella copy).
    all_tickets = ctx.service.list()
    children = [tk for tk in all_tickets if tk.parent_id == t.id]
    assert len(children) == 3
    titles = {c.title for c in children}
    assert titles == {"Migrate runners", "Migrate langfuse", "Migrate notify"}

    # Linear dependency chain: C1 depends on C0, C2 depends on C1.
    sorted_children = sorted(children, key=lambda c: c.created_at)
    deps_c1 = ctx.service.unmet_dependencies(sorted_children[1])
    assert sorted_children[0].id in deps_c1
    deps_c2 = ctx.service.unmet_dependencies(sorted_children[2])
    assert sorted_children[1].id in deps_c2

    # Epic body written to the promoted ticket's workspace.
    body = ctx.service.workspace(promoted).read_description()
    assert "Epic: modular layout migration" in body


# ---------------------------------------------------------------------------
# 31. promote_to_epic: breakdown failure leaves epic in place
# ---------------------------------------------------------------------------


def test_promote_to_epic_breakdown_failure_leaves_epic_intact(ctx_factory, monkeypatch):
    """A flaky epic-breakdown run must NOT block the refine stage —
    the ticket is still promoted to an epic so the operator can hit
    /generate-children manually. The breakdown failure is captured in
    the outcome note."""
    from robotsix_mill.agents import epic_breakdown as _ebreak

    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, body="One-shot migration of the repo")

    def _raise(**kw):
        raise RuntimeError("breakdown LLM timed out")

    monkeypatch.setattr(_ebreak, "run_epic_breakdown_agent", _raise)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            promote_to_epic=True,
            epic_body="## Epic body",
            spec_markdown=None,
        ),
    )

    out = RefineStage().run(t, ctx)

    # Promotion still landed even though breakdown failed.
    assert out.next_state is State.EPIC_OPEN
    assert "breakdown failed" in out.note

    # kind flip is synchronous; the worker handles the state move
    # to EPIC_OPEN after stage.run returns.
    promoted = ctx.service.get(t.id)
    assert promoted.kind == "epic"

    # No children spawned.
    all_tickets = ctx.service.list()
    children = [tk for tk in all_tickets if tk.parent_id == t.id]
    assert children == []


# ---------------------------------------------------------------------------
# 31b. promote_to_epic: pre-filing dedup flags an overlapping child but
#      still creates BOTH (never silently dropped).
# ---------------------------------------------------------------------------


def test_promote_to_epic_flags_overlapping_child_but_creates_both(
    ctx_factory, monkeypatch
):
    """The refine inline epic-breakdown path runs the advisory dedup
    check before filing: two children whose scopes overlap (shared
    CONTRIBUTING.md path) are BOTH created, with the later one carrying
    the ``[!warning]`` advisory block."""
    from robotsix_mill.agents import epic_breakdown as _ebreak

    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx, title="Audit Trivy SARIF handling", body="One-shot repo migration")

    class _FakeBreakdown:
        def __init__(self):
            self.child_titles = ["First Trivy child", "Second Trivy child"]
            self.child_bodies = [
                "Work documented in CONTRIBUTING.md for the first child",
                "Work documented in CONTRIBUTING.md for the second child",
            ]
            self.epic_body = None

    monkeypatch.setattr(
        _ebreak, "run_epic_breakdown_agent", lambda **kw: _FakeBreakdown()
    )
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            promote_to_epic=True,
            epic_body="## Strategic epic body",
            spec_markdown=None,
        ),
    )

    out = RefineStage().run(t, ctx)
    assert out.next_state is State.EPIC_OPEN

    children = [tk for tk in ctx.service.list() if tk.parent_id == t.id]
    assert len(children) == 2, "both children must be created, none dropped"
    bodies = [ctx.service.workspace(c).read_description() for c in children]
    flagged = [b for b in bodies if "[!warning]" in b]
    assert len(flagged) == 1
    assert "CONTRIBUTING.md" in flagged[0]


# ---------------------------------------------------------------------------
# 32. no_change_needed: refine closes ticket directly to DONE with rationale comment
# ---------------------------------------------------------------------------


def test_no_change_needed_closes_to_done_with_rationale_comment(
    ctx_factory,
    monkeypatch,
):
    """When refine returns ``no_change_needed=True`` with a rationale,
    the stage:

    - folds the rationale into the transition note (history) — v1
      moved agent conclusions out of comments;
    - transitions DRAFT → DONE directly (skipping implement, review,
      etc.).

    This is the bypass that catches the d129-style failure mode: a
    config-drift ticket whose deliverable was 'post a comment with
    findings' got stuck because implement had no way to communicate.
    Now refine handles it directly via history."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(
        ctx,
        body=(
            "## Problem\n\nThe env_sync detector flagged X as drift, but "
            "investigation shows it's a false positive — see Evidence "
            "below.\n\n## Acceptance criteria\n\nPost a comment explaining "
            "the false positive and close."
        ),
    )

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            no_change_needed=True,
            no_change_rationale=(
                "## Findings\n\nThe chain is wired correctly. "
                "Detector misread the YAML anchor."
            ),
            spec_markdown=None,
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note
    # Rationale (truncated) is folded into the transition note.
    assert "wired correctly" in (out.note or "")
    # No agent-authored comment.
    comments = ctx.service.list_comments(t.id)
    assert not any(c.author == "refine" for c in comments)

    # No epic / no split children spawned by this path.
    all_tickets = ctx.service.list()
    children = [tk for tk in all_tickets if tk.parent_id == t.id]
    assert children == []


# ---------------------------------------------------------------------------
# 33. no_change_needed without rationale falls back to normal spec path
# ---------------------------------------------------------------------------


def test_no_change_needed_empty_rationale_falls_back_to_spec(
    ctx_factory,
    monkeypatch,
):
    """Refine returning no_change_needed=true with an EMPTY rationale
    must NOT close the ticket — closing without explanation is worse
    than asking the operator to review. Falls through to the normal
    single-spec path so the spec body is the source of truth."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            no_change_needed=True,
            no_change_rationale="   ",  # whitespace-only
            spec_markdown="## Problem\nReal spec body.",
        ),
    )

    out = RefineStage().run(t, ctx)

    # We did NOT close to DONE — we fell through to the normal path
    # (READY when require_approval=false).
    assert out.next_state is not State.DONE
    # No rationale comment was filed.
    assert ctx.service.list_comments(t.id) == []


# ---------------------------------------------------------------------------
# 34. no_change_needed on redrafted ticket with unmerged branch → BLOCKED
# ---------------------------------------------------------------------------


def test_no_change_needed_unmerged_branch_blocks(
    ctx_factory,
    monkeypatch,
):
    """When a redrafted ticket (has a branch from a prior implement
    run) receives a no_change_needed verdict, but the branch is NOT
    merged to main, the ticket must route to BLOCKED — not DONE —
    so the implementation is not stranded on an orphaned branch."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)
    # Simulate a prior implement run that set a branch.
    ctx.service.set_branch(t.id, "feature/redrafted-work")
    t = ctx.service.get(t.id)  # re-fetch so t.branch reflects the update

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            no_change_needed=True,
            no_change_rationale="Already implemented on the branch.",
            spec_markdown=None,
        ),
        # Simulate the branch being unmerged.
        _verify_branch_merged=lambda repo_dir, ticket: False,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "not merged to main" in (out.note or "")
    assert "feature/redrafted-work" in (out.note or "")


# ---------------------------------------------------------------------------
# 35. no_change_needed on redrafted ticket with merged branch → DONE (no regression)
# ---------------------------------------------------------------------------


def test_no_change_needed_merged_branch_proceeds(
    ctx_factory,
    monkeypatch,
):
    """A redrafted ticket whose branch IS merged to main must still
    close as DONE via the no_change_needed path — no regression for
    the normal merged case."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)
    ctx.service.set_branch(t.id, "feature/merged-work")
    t = ctx.service.get(t.id)  # re-fetch so t.branch reflects the update

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            no_change_needed=True,
            no_change_rationale="Already implemented and merged.",
            spec_markdown=None,
        ),
        # Simulate the branch being confirmed merged.
        _verify_branch_merged=lambda repo_dir, ticket: True,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in (out.note or "")


# ---------------------------------------------------------------------------
# 36. no_change_needed without a branch (first refine) → DONE (unaffected)
# ---------------------------------------------------------------------------


def test_no_change_needed_no_branch_proceeds(
    ctx_factory,
    monkeypatch,
):
    """A ticket that has never been implemented (no branch set) must
    close normally via no_change_needed — the merge check is skipped
    entirely for first-time refines."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)
    # No branch set — this is a first-time refine.

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(
            no_change_needed=True,
            no_change_rationale="Informational ticket, no code change needed.",
            spec_markdown=None,
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in (out.note or "")


# ---------------------------------------------------------------------------
# Mill/system author comments are NOT reviewer feedback
# ---------------------------------------------------------------------------


def test_mill_author_comments_excluded_from_reviewer_feedback(
    ctx_factory,
    monkeypatch,
):
    """Auto-posted trace-link comments (author='mill') and timeout-
    escalation pings (author='system') are diagnostic notes, not human
    feedback. They must NOT be forwarded to refine as
    ``reviewer_comments`` — doing so taught the agent to ask_user
    'what did the reviewer say?' about an inaccessible Langfuse URL."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)

    # Two open top-level comments: one feedback (user), one mill-auto.
    ctx.service.add_comment(
        t.id, "Real reviewer ask: please tighten the spec.", author="user"
    )
    ctx.service.add_comment(
        t.id,
        "🔍 [Trace: refine](https://langfuse.example/traces/xyz)",
        author="mill",
    )

    captured: dict = {}

    def _capture(*, settings, title, draft, reviewer_comments=None, **kw):
        captured["reviewer_comments"] = reviewer_comments
        return RefineResult(spec_markdown="## Problem\nok")

    _apply_default_mocks(monkeypatch, run_refine_agent=_capture)

    RefineStage().run(t, ctx)

    rc = captured["reviewer_comments"]
    assert rc is not None, "user-authored open thread should be forwarded"
    assert "Real reviewer ask" in rc
    assert "Trace: refine" not in rc
    assert "langfuse" not in rc


def test_only_mill_comments_means_no_reviewer_feedback(
    ctx_factory,
    monkeypatch,
):
    """When the ONLY open top-level comments are mill-author trace
    links, refine sees no reviewer_comments at all — and the triage
    short-circuit (skipped only when reviewer_comments is None) stays
    available."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    t = _ticket(ctx)

    ctx.service.add_comment(
        t.id,
        "🔍 [Trace: refine](https://langfuse.example/traces/xyz)",
        author="mill",
    )
    ctx.service.add_comment(t.id, "timeout escalation ping", author="system")

    captured: dict = {}

    def _capture(*, settings, title, draft, reviewer_comments=None, **kw):
        captured["reviewer_comments"] = reviewer_comments
        return RefineResult(spec_markdown="## Problem\nok")

    _apply_default_mocks(monkeypatch, run_refine_agent=_capture)

    RefineStage().run(t, ctx)

    assert captured["reviewer_comments"] is None


# ---------------------------------------------------------------------------
# meta board: triage-built multi-repo workspace + board-keyed memory ledger
# ---------------------------------------------------------------------------


def test_meta_ticket_uses_triage_workspace_and_meta_memory(
    ctx_factory, monkeypatch, tmp_path
):
    """A meta-board ticket has no registered repo_config: refine must run the
    repo-triage agent, clone the triaged repos (passing them as extra_roots),
    and key the refine memory ledger on the ticket's board_id ('meta') —
    NOT crash in memory_file_for on an empty board_id."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    ctx.repo_config = None  # meta board is not a registered repo
    t = _ticket(
        ctx,
        title="Extract shared loader",
        body=(
            "Extract the duplicated YAML cascade loader into a shared library "
            "consumed by both robotsix-mill and robotsix-auto-mail."
        ),
    )
    t.board_id = "meta"

    repo_dir = tmp_path / "repos" / "robotsix-mill"
    repo_dir.mkdir(parents=True)
    extra = [repo_dir, tmp_path / "repos" / "robotsix-auto-mail"]
    monkeypatch.setattr(
        mt, "required_repos_for", lambda *, settings, spec: ["robotsix-mill"]
    )
    monkeypatch.setattr(
        mw, "build_meta_workspace", lambda settings, ws, repo_ids: (repo_dir, extra)
    )

    captured: dict = {}

    def _capture(*, settings, title, draft, repo_dir=None, **kw):
        captured["board_id"] = kw.get("board_id")
        captured["extra_roots"] = kw.get("extra_roots")
        return RefineResult(spec_markdown="## Problem\nExtract it")

    _apply_default_mocks(monkeypatch, run_refine_agent=_capture)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert captured["board_id"] == "meta"  # memory keyed on the meta board
    assert captured["extra_roots"] == extra  # multi-repo workspace threaded


def test_meta_ticket_blocks_when_no_repos_clonable(ctx_factory, monkeypatch):
    """If the triaged workspace yields no clone, refine BLOCKs the meta ticket
    with a clear note rather than proceeding with no repo_dir."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    ctx = ctx_factory(require_approval="false", refine_triage_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Cross-repo thing", body="Align practice X across repos.")
    t.board_id = "meta"

    monkeypatch.setattr(mt, "required_repos_for", lambda *, settings, spec: [])
    monkeypatch.setattr(
        mw, "build_meta_workspace", lambda settings, ws, repo_ids: (None, [])
    )
    _apply_default_mocks(monkeypatch)

    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


# ---------------------------------------------------------------------------
# _verify_branch_merged: real git repo, local-only (unpushed) branch fallback
# ---------------------------------------------------------------------------


def _git(repo, *args):
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_repo_with_origin(tmp_path):
    """Build a work repo with an ``origin/main`` remote-tracking ref.

    Creates a bare repo used as ``origin``, a work repo with an initial
    commit on ``main`` pushed to it, and fetches so ``origin/main``
    resolves locally.  Returns the work-repo ``Path``.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit on main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    return repo


def test_verify_branch_merged_local_only_unmerged_returns_false(tmp_path):
    """A branch that exists ONLY locally (absent from origin so
    ``git fetch origin <branch>`` fails) and is NOT an ancestor of
    ``origin/main`` must return ``False`` — the local-only / unpushed
    case must not slip through the fetch-failure best-effort allow."""
    from robotsix_mill.core.models import Ticket

    repo = _build_repo_with_origin(tmp_path)
    # Local-only branch carrying a NEW commit not on origin/main.
    _git(repo, "checkout", "-b", "mill/local-only")
    (repo / "feature.txt").write_text("wip\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "WIP feature commit never pushed")

    ticket = Ticket(
        id="t-local-unmerged",
        title="t",
        workspace_path="x",
        branch="mill/local-only",
    )

    assert refine_module._verify_branch_merged(repo, ticket) is False


def test_verify_branch_merged_local_only_ancestor_returns_true(tmp_path):
    """A local-only branch whose tip IS an ancestor of ``origin/main``
    (e.g. it points at the already-merged main commit) returns
    ``True`` — the local fallback confirms it is merged."""
    from robotsix_mill.core.models import Ticket

    repo = _build_repo_with_origin(tmp_path)
    # Local-only branch pointing at the main commit already on origin.
    _git(repo, "branch", "mill/merged", "main")

    ticket = Ticket(
        id="t-local-merged",
        title="t",
        workspace_path="x",
        branch="mill/merged",
    )

    assert refine_module._verify_branch_merged(repo, ticket) is True


def test_verify_branch_merged_unresolvable_branch_returns_true(tmp_path):
    """A branch that resolves on NEITHER origin nor locally returns
    ``True`` — best-effort allow is preserved when there is genuinely
    nothing to verify."""
    from robotsix_mill.core.models import Ticket

    repo = _build_repo_with_origin(tmp_path)

    ticket = Ticket(
        id="t-ghost",
        title="t",
        workspace_path="x",
        branch="mill/does-not-exist",
    )

    assert refine_module._verify_branch_merged(repo, ticket) is True

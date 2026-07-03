"""Unit tests for ``RefineAgentMixin`` (the refine-stage orchestration).

These exercise the two ``@staticmethod`` seams in
``src/robotsix_mill/stages/refine/orchestration.py`` —
``_review_spec_conciseness`` and ``_run_refine_agent`` — *directly*
with mocked collaborators, complementing the end-to-end
``RefineStage().run()`` coverage in ``test_refine_stage.py``.  Because
the methods are static, they are called as
``RefineStage._review_spec_conciseness(...)`` /
``RefineStage._run_refine_agent(...)`` with no stage instance.

All agent collaborators are mocked (no LLM/network); the workspace,
ticket service, and repo_dir are real on ``tmp_path``.
"""

from __future__ import annotations

import json

import pytest

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    ChildSpec,
    FileMapEntry,
    RefineResult,
    SpecReviewResult,
    TriageResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.stages.refine import orchestration as orch_module
from robotsix_mill.stages.refine.helpers import (
    UNMERGED_BRANCH_PREFIX,
    _AUTO_APPROVE_SOURCES,
    _summarize_spec_for_auto_approve,
)
from robotsix_mill.vcs import git_ops


# A genuine (> 120 char) spec body so ``_spec_is_degenerate`` never trips.
_REAL_SPEC = (
    "## Problem\n\nThe widget loader silently swallows IO errors so a "
    "missing config file looks like an empty config.\n\n## Scope\n\n"
    "Raise a clear error in `widget/loader.py` when the file is absent.\n\n"
    "## Acceptance criteria\n\n- A missing file raises `ConfigMissing`.\n"
    "## Out of scope\n\n- No change to the parser."
)
_CONCISE_SPEC = (
    "## Problem\n\nConcise restatement of the loader bug that is comfortably "
    "longer than the 120-char degeneracy threshold so it is treated as a "
    "real spec rather than a placeholder pointer.\n\n## Scope\n\nFix it."
)


# ---------------------------------------------------------------------------
# fixtures / helpers (adapted from test_refine_stage.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import RepoConfig, Settings

    counter = [0]

    def make(**env):
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
    """Create a DRAFT ticket with a body comfortably past the 100-char
    trivial-draft threshold (mirrors test_refine_stage.py's helper)."""
    if body is None:
        body = (
            "Add a feature. This is a substantive draft body padded "
            "past the 100-char trivial-draft threshold so refine's "
            "pipeline actually runs against this ticket."
        )
    elif body and len(body) < 100:
        body = (
            f"{body}. This is a substantive draft body padded past "
            "the 100-char trivial-draft threshold so refine's pipeline "
            "actually runs against this ticket."
        )
    return ctx.service.create(title, body, **kw)


def _mock_refine_returns(result: RefineResult):
    """A ``run_refine_agent`` stub returning a canned ``RefineResult``."""

    def _run(**kw):
        del kw
        return result

    return _run


def _mock_spec_review(concise_spec=None, stripped_summary="stripped 3 lines"):
    def _run(*, settings, spec_markdown, **kw):
        del settings, kw
        return SpecReviewResult(
            concise_spec=concise_spec if concise_spec is not None else spec_markdown,
            stripped_summary=stripped_summary,
        )

    return _run


def _mock_triage(decision="REFINE", reason="needs refinement"):
    def _run(*, settings, title, draft, **kw):
        del settings, title, draft, kw
        return TriageResult(decision=decision, reason=reason)

    return _run


def _apply_default_mocks(monkeypatch, **overrides):
    """Wire the agent seams the orchestration resolves through the package
    façade plus the agent-call collaborators, with happy-path defaults."""
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        overrides.get(
            "run_refine_agent",
            _mock_refine_returns(RefineResult(spec_markdown=_REAL_SPEC)),
        ),
    )
    monkeypatch.setattr(
        refining, "triage_refine", overrides.get("triage_refine", _mock_triage())
    )
    monkeypatch.setattr(
        refining,
        "triage_reviewer_agreement",
        overrides.get(
            "triage_reviewer_agreement",
            lambda **kw: refining.ReviewerAgreementResult(
                decision="DISAGREE", reason="default — fall through"
            ),
        ),
    )
    monkeypatch.setattr(
        refining,
        "review_spec_for_conciseness",
        overrides.get("review_spec_for_conciseness", _mock_spec_review()),
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
    # The orchestration now uses _load_refine_memory/_persist_refine_memory
    # (DB-backed) imported into the orchestration module's namespace.
    # Patch those so existing tests that set persist_memory/load_memory
    # overrides still work.
    import robotsix_mill.stages.refine.orchestration as orch_mod

    monkeypatch.setattr(
        orch_mod,
        "_load_refine_memory",
        overrides.get(
            "load_memory",
            lambda s, memory_board_id: "",
        ),
    )
    monkeypatch.setattr(
        orch_mod,
        "_persist_refine_memory",
        overrides.get(
            "persist_memory",
            lambda s, memory_board_id, text: None,
        ),
    )
    monkeypatch.setattr(
        refine_module,
        "_verify_branch_merged",
        overrides.get("_verify_branch_merged", lambda repo_dir, ticket: True),
    )


def _run_agent(ctx, ticket, tmp_path, *, draft=None, epic_ctx=None, title=None):
    """Invoke ``_run_refine_agent`` with real workspace + repo_dir."""
    ws = ctx.service.workspace(ticket)
    return RefineStage._run_refine_agent(
        ctx,
        ticket,
        draft if draft is not None else "raw draft body for the ticket",
        tmp_path,
        epic_ctx,
        title if title is not None else ticket.title,
        ws,
        ctx.settings,
    )


# ===========================================================================
# _review_spec_conciseness
# ===========================================================================


def test_review_conciseness_success_returns_concise_and_writes_verbose(
    ctx_factory, monkeypatch
):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    monkeypatch.setattr(
        refining,
        "review_spec_for_conciseness",
        _mock_spec_review(concise_spec=_CONCISE_SPEC),
    )

    out = RefineStage._review_spec_conciseness(
        ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
    )

    assert out == _CONCISE_SPEC
    verbose = ws.artifacts_dir / "refine-verbose.md"
    assert verbose.exists()
    assert verbose.read_text(encoding="utf-8") == _REAL_SPEC


def test_review_conciseness_degenerate_returns_original(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    # "tbd" is flagged degenerate by _spec_is_degenerate.
    monkeypatch.setattr(
        refining, "review_spec_for_conciseness", _mock_spec_review(concise_spec="tbd")
    )

    out = RefineStage._review_spec_conciseness(
        ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
    )

    assert out == _REAL_SPEC  # original verbose spec kept
    # Verbose artifact is still written before the degeneracy check.
    assert (ws.artifacts_dir / "refine-verbose.md").read_text(
        encoding="utf-8"
    ) == _REAL_SPEC


def test_review_conciseness_exception_returns_original(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    def _boom(*, settings, spec_markdown, **kw):
        raise RuntimeError("review backend exploded")

    monkeypatch.setattr(refining, "review_spec_for_conciseness", _boom)

    # No exception must propagate; original spec returned unchanged.
    out = RefineStage._review_spec_conciseness(
        ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
    )
    assert out == _REAL_SPEC


def test_review_conciseness_child_index_variant(ctx_factory, monkeypatch, caplog):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    monkeypatch.setattr(
        refining,
        "review_spec_for_conciseness",
        _mock_spec_review(concise_spec=_CONCISE_SPEC),
    )

    with caplog.at_level("INFO", logger="robotsix_mill.stages.refine"):
        out = RefineStage._review_spec_conciseness(
            ctx.settings, ws, t, _REAL_SPEC, "refine-verbose-child-2.md", child_index=2
        )

    assert out == _CONCISE_SPEC
    assert (ws.artifacts_dir / "refine-verbose-child-2.md").exists()
    assert any("spec review child 2" in r.message for r in caplog.records)


# ===========================================================================
# _run_refine_agent — result-mode routing & control flow
# ===========================================================================


def test_single_scope_success(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(monkeypatch)

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("refined")
    ws = ctx.service.workspace(t)
    assert ws.description_path.read_text(encoding="utf-8") == _REAL_SPEC
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_single_scope_degenerate_spec_keeps_draft(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(spec_markdown="(see spec above)")
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert "no usable spec" in out.note


def test_split_two_children_creates_tickets(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.CLOSED
    assert out.note.startswith("split into ")
    child_ids = out.note.removeprefix("split into ").split(", ")
    assert len(child_ids) == 2
    for cid in child_ids:
        assert ctx.service.get(cid) is not None


def test_split_no_children_degrades_to_single(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(split=True, children=None, spec_markdown=_REAL_SPEC)
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "split degraded" in out.note
    assert (
        ctx.service.workspace(t).description_path.read_text(encoding="utf-8")
        == _REAL_SPEC
    )


def test_split_single_valid_child_falls_back(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[ChildSpec(title="Only child", spec_markdown=_REAL_SPEC)],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "single child, no split" in out.note


def test_promote_to_epic(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(promote_to_epic=True, epic_body=_REAL_SPEC)
        ),
    )

    import robotsix_mill.agents.epic_breakdown as epic_breakdown

    class _Breakdown:
        child_titles: list[str] = []
        child_bodies: list[str] = []
        epic_body = ""

    monkeypatch.setattr(
        epic_breakdown, "run_epic_breakdown_agent", lambda **kw: _Breakdown()
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.EPIC_OPEN
    assert ctx.service.get(t.id).kind == TicketKind.EPIC


def test_no_change_needed_closes_to_done(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                no_change_needed=True,
                no_change_rationale=(
                    "The reported condition is handled by the existing guard "
                    "clause in the loader; the body already documents the full "
                    "investigation and there is nothing to change."
                ),
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    # For a TASK ticket without a branch, the no-change-needed path
    # must NOT auto-close to DONE — it routes to READY (or
    # HUMAN_ISSUE_APPROVAL when gated) so implement can verify the
    # "no change needed" claim against the live tree.
    assert out.next_state is not State.DONE
    assert (
        "no change needed" in out.note.lower()
        or "routing to implement" in out.note.lower()
    )


def test_no_change_needed_empty_rationale_degrades(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                no_change_needed=True,
                no_change_rationale="",
                spec_markdown=_REAL_SPEC,
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is not State.DONE
    assert out.note.startswith("refined")


def test_no_change_needed_unmerged_branch_blocks(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.set_branch(t.id, "feat/orphan")
    t = ctx.service.get(t.id)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                no_change_needed=True,
                no_change_rationale=(
                    "The implementation is complete; nothing further to change "
                    "in the loader behaviour for this ticket."
                ),
            )
        ),
        _verify_branch_merged=lambda repo_dir, ticket: False,
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.BLOCKED
    assert out.note.startswith(UNMERGED_BRANCH_PREFIX)


def test_pause_detected_awaits_user_reply(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                spec_markdown=_REAL_SPEC,
                new_messages=b"[]",
                conversation_state=b'{"messages": []}',
            )
        ),
    )
    # Pause helper imported into the orchestration namespace.
    monkeypatch.setattr(orch_module, "check_for_pause", lambda new_messages: True)

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.AWAITING_USER_REPLY
    assert ctx.service.get(t.id).state is State.AWAITING_USER_REPLY
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "refine_conversation_state.json").exists()


def test_agent_runtime_error_blocks(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)

    def _raise(**kw):
        raise RuntimeError("OPENROUTER_API_KEY not set")

    _apply_default_mocks(monkeypatch, run_refine_agent=_raise)

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.BLOCKED
    assert out.note == "OPENROUTER_API_KEY not set"


def test_triage_skip_does_not_run_agent(ctx_factory, monkeypatch, tmp_path):
    # refine_triage_enabled defaults to True.
    ctx = ctx_factory()
    assert ctx.settings.refine_triage_enabled is True
    t = _ticket(ctx)

    called: list[int] = []

    def _record(**kw):
        called.append(1)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_record,
        triage_refine=_mock_triage(decision="SKIP", reason="already a precise spec"),
    )

    out = _run_agent(ctx, t, tmp_path, draft="A precise draft with no file paths.")

    assert out.note.startswith("triage SKIP")
    assert called == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "file_map.json").exists()


def test_gitignored_file_map_blocks(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                spec_markdown=_REAL_SPEC,
                file_map=[FileMapEntry(file="x.py", note="n")],
            )
        ),
    )
    monkeypatch.setattr(git_ops, "ignored_paths", lambda repo, paths: ["x.py"])

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.BLOCKED
    assert "x.py" in out.note


# ===========================================================================
# _collect_reviewer_comments
# ===========================================================================


def test_collect_reviewer_comments_user_open_thread(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    c = ctx.service.add_comment(t.id, "Please clarify the error class.", author="user")

    reviewer_comments, open_thread_ids = RefineStage._collect_reviewer_comments(ctx, t)

    assert reviewer_comments is not None
    assert f"[id={c.id}" in reviewer_comments
    assert "Please clarify the error class." in reviewer_comments
    assert open_thread_ids == {c.id}


def test_collect_reviewer_comments_non_feedback_authors_filtered(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "trace link: https://example/x", author="mill")
    ctx.service.add_comment(t.id, "timeout escalation ping", author="system")

    reviewer_comments, open_thread_ids = RefineStage._collect_reviewer_comments(ctx, t)

    assert reviewer_comments is None
    assert open_thread_ids == set()


def test_collect_reviewer_comments_closed_thread_excluded(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    c = ctx.service.add_comment(t.id, "Resolved already.", author="user")
    ctx.service.close_thread(c.id, ticket_id=t.id)

    reviewer_comments, open_thread_ids = RefineStage._collect_reviewer_comments(ctx, t)

    assert reviewer_comments is None
    assert open_thread_ids == set()


def test_collect_reviewer_comments_reply_to_closed_excluded(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    # An open top-level thread that must survive.
    open_c = ctx.service.add_comment(t.id, "Still open feedback.", author="user")
    # A second thread that gets closed, with a reply hanging off it.
    closed_c = ctx.service.add_comment(t.id, "Closed feedback.", author="user")
    reply = ctx.service.add_comment(
        t.id, "Reply under the closed thread.", author="user", parent_id=closed_c.id
    )
    ctx.service.close_thread(closed_c.id, ticket_id=t.id)

    reviewer_comments, open_thread_ids = RefineStage._collect_reviewer_comments(ctx, t)

    assert reviewer_comments is not None
    assert "Still open feedback." in reviewer_comments
    # The closed thread and its reply are both excluded.
    assert "Closed feedback." not in reviewer_comments
    assert "Reply under the closed thread." not in reviewer_comments
    assert open_thread_ids == {open_c.id}
    assert reply.id not in open_thread_ids


# ===========================================================================
# _split_child_fast_path
# ===========================================================================


def _spy_refine(monkeypatch, **overrides):
    """Apply default mocks with a call-recording ``run_refine_agent`` spy.

    Returns the ``calls`` list so a test can assert the full refine agent
    was (or was not) invoked.
    """
    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(monkeypatch, run_refine_agent=_spy, **overrides)
    return calls


def test_split_child_parent_closed_short_circuits(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Umbrella")
    child = _ticket(ctx, title="Child", parent_id=parent.id)
    ctx.service.transition(
        parent.id, State.CLOSED, note=f"split into {child.id}, other-child"
    )
    calls = _spy_refine(monkeypatch)

    out = _run_agent(ctx, child, tmp_path, draft=_REAL_SPEC)

    assert out.note.startswith("split child — spec already refined")
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    ws = ctx.service.workspace(child)
    file_map = ws.artifacts_dir / "file_map.json"
    assert json.loads(file_map.read_text(encoding="utf-8")) == []
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert calls == []  # full refine agent never ran


def test_split_child_own_history_split_from_short_circuits(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    child = _ticket(ctx, title="Reparented child")
    # A reparented child carries the "split from" note in its own history
    # (its direct parent is the umbrella epic, not a CLOSED ticket).
    ctx.service.transition(
        child.id,
        State.HUMAN_ISSUE_APPROVAL,
        note="split from 20250101T000000Z-parent-aa",
    )
    child = ctx.service.get(child.id)
    calls = _spy_refine(monkeypatch)

    out = _run_agent(ctx, child, tmp_path, draft=_REAL_SPEC)

    assert out.note.startswith("split child — spec already refined")
    assert calls == []


def test_split_child_with_reviewer_comment_runs_full_agent(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Umbrella")
    child = _ticket(ctx, title="Child", parent_id=parent.id)
    ctx.service.transition(parent.id, State.CLOSED, note=f"split into {child.id}")
    ctx.service.add_comment(
        child.id, "Reviewer wants a different scope.", author="user"
    )
    calls = _spy_refine(monkeypatch)

    out = _run_agent(ctx, child, tmp_path, draft=_REAL_SPEC)

    # Open reviewer comment forces the full refine agent even for a split child.
    assert len(calls) == 1
    assert out.note.startswith("refined")
    assert (
        ctx.service.workspace(child).description_path.read_text(encoding="utf-8")
        == _REAL_SPEC
    )


def test_split_child_empty_description_blocks(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Umbrella")
    child = _ticket(ctx, title="Child", parent_id=parent.id)
    ctx.service.transition(parent.id, State.CLOSED, note=f"split into {child.id}")
    _spy_refine(monkeypatch)

    out = _run_agent(ctx, child, tmp_path, draft="   ")

    assert out.next_state is State.BLOCKED
    assert out.note == "split child has empty description"


def test_parent_closed_non_split_does_not_short_circuit(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Umbrella")
    child = _ticket(ctx, title="Child", parent_id=parent.id)
    # Parent CLOSED for a non-split reason (e.g. retrospect) — must NOT
    # be treated as a split child.
    ctx.service.transition(parent.id, State.CLOSED, note="retrospected: complete")
    calls = _spy_refine(monkeypatch)

    out = _run_agent(ctx, child, tmp_path, draft=_REAL_SPEC)

    assert len(calls) == 1  # full refine agent ran
    assert out.note.startswith("refined")


# ===========================================================================
# _reviewer_agreement_guard
# ===========================================================================


def test_reviewer_agreement_guard_agree_short_circuits(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(
        t.id,
        "Confirmed — the refine machinery doesn't exist here. LGTM.",
        author="user",
    )
    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_spy,
        triage_reviewer_agreement=lambda **kw: refining.ReviewerAgreementResult(
            decision="AGREE",
            reason="Reviewer confirmed the draft's misrouting finding.",
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    # For a TASK ticket without a branch, the reviewer-agreement guard
    # must NOT auto-close to DONE — it routes to READY (or
    # HUMAN_ISSUE_APPROVAL when gated) so implement can verify.
    assert out.next_state is not State.DONE
    assert "reviewer agreement" in out.note.lower()
    assert "Reviewer confirmed" in out.note
    assert calls == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert (ws.artifacts_dir / "file_map.json").exists()


def test_reviewer_agreement_guard_disagree_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(
        t.id, "Please also update the README with this finding.", author="user"
    )
    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_spy,
        triage_reviewer_agreement=lambda **kw: refining.ReviewerAgreementResult(
            decision="DISAGREE",
            reason="Reviewer requested a README update — full refine needed.",
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert len(calls) == 1  # full refine agent ran
    assert out.note.startswith("refined")


def test_reviewer_agreement_guard_gate_disabled_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory(reviewer_agreement_gate_enabled=False)
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "Confirmed — no change needed.", author="user")
    called: list[int] = []

    def _fake_triage(**kw):
        called.append(1)
        return refining.ReviewerAgreementResult(
            decision="AGREE", reason="Would have agreed."
        )

    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_spy,
        triage_reviewer_agreement=_fake_triage,
    )

    out = _run_agent(ctx, t, tmp_path)

    # Gate disabled — triage_reviewer_agreement never called.
    assert called == []
    # Full refine agent ran.
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_reviewer_agreement_guard_exception_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "Confirmed — no change needed.", author="user")

    def _boom(**kw):
        raise RuntimeError("backend unavailable")

    calls: list[dict] = []

    def _spy(**kw):
        calls.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_spy,
        triage_reviewer_agreement=_boom,
    )

    out = _run_agent(ctx, t, tmp_path)

    # Exception in guard → falls through to full refine.
    assert len(calls) == 1
    assert out.note.startswith("refined")


# ===========================================================================
# _triage_skip — MAINTENANCE + SKIP path-extraction
# ===========================================================================


def test_triage_no_change_verdict_returns_done(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="NO_CHANGE",
            reason="The file `.robotsix-mill/periodic/audit.yaml` already exists with the expected content.",
        ),
    )

    out = _run_agent(
        ctx, t, tmp_path, draft="Create `.robotsix-mill/periodic/audit.yaml`."
    )

    # For a TASK ticket without a branch, triage NO_CHANGE must NOT
    # auto-close to DONE — it routes to READY (or HUMAN_ISSUE_APPROVAL
    # when gated) so implement can verify the "no change" claim.
    assert out.next_state is not State.DONE
    assert "NO_CHANGE" in out.note
    assert "routing to implement" in out.note.lower()
    assert calls == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_triage_skip_always_routes_to_implement(ctx_factory, monkeypatch, tmp_path):
    """SKIP must ALWAYS route to implement, never to State.DONE, even for
    reasons that previously matched _triage_reason_rejects."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="SKIP",
            reason="the entire gap assertion is factually wrong — no change is needed",
        ),
    )

    out = _run_agent(ctx, t, tmp_path, draft="Some draft.")

    # SKIP now routes to implement (not DONE) — the old reject-gate is removed.
    assert out.next_state is not State.DONE
    assert out.note.startswith("triage SKIP")
    assert calls == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_triage_presence_file_regression(ctx_factory, monkeypatch, tmp_path):
    """Regression: a config-only `.robotsix-mill/periodic/<agent>.yaml`
    presence-file draft whose file is absent does NOT close to State.DONE.
    The triage mock returns SKIP (the correct verdict for a verified-absent
    deliverable) which routes to implement — never DONE."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="SKIP",
            reason="precise draft — routes to implement",
        ),
    )

    out = _run_agent(
        ctx, t, tmp_path, draft="Create `.robotsix-mill/periodic/copy_paste.yaml`."
    )

    # SKIP routes to implement, not DONE
    assert out.next_state not in (State.DONE, State.CLOSED)
    assert out.note.startswith("triage SKIP")
    assert calls == []  # full refine agent never invoked


def test_triage_maintenance_routes_to_maintenance(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory(maintenance_triage_enabled=True)
    t = _ticket(ctx)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="MAINTENANCE", reason="restart the worker"),
    )

    out = _run_agent(ctx, t, tmp_path, draft="Please restart the deploy.")

    assert out.next_state is State.MAINTENANCE
    assert out.note.startswith("maintenance triage (LLM):")
    assert calls == []  # full refine agent never ran
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_triage_maintenance_ci_source_falls_through_to_refine(
    ctx_factory, monkeypatch, tmp_path
):
    """A CI-failure ticket (source == ci) is NEVER routed to the read-only
    maintenance agent even when triage says MAINTENANCE — CI failures are
    code/config fixes the maintenance agent cannot make. It must fall
    through to the full refine agent instead (regression: GHCR
    docker-release `packages: write` tickets mis-triaged to MAINTENANCE
    and dead-ended in a 'needs a human' block)."""
    ctx = ctx_factory(maintenance_triage_enabled=True)
    t = _ticket(ctx, source=SourceKind.CI)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="MAINTENANCE", reason="looks like an ops permissions issue"
        ),
    )

    out = _run_agent(ctx, t, tmp_path, draft="CI failure: Release image on main.")

    assert out.next_state is not State.MAINTENANCE
    assert not out.note.startswith("maintenance triage (LLM):")
    assert calls != []  # full refine agent DID run for the CI ticket


def test_triage_skip_extracts_backtick_paths(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="SKIP", reason="already precise"),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Raise a clear error in `src/foo/bar.py` when the file is absent.",
    )

    assert out.note.startswith("triage SKIP")
    assert calls == []
    ws = ctx.service.workspace(t)
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert {"file": "src/foo/bar.py", "note": "from draft"} in file_map


# ===========================================================================
# _triage_skip — mechanical draft fast-path
# ===========================================================================


def test_mechanical_draft_fast_path_skips_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When a mill-internal automated ticket passes triage with REFINE but
    auto-approve confirms it is purely mechanical, the expensive refine
    agent is skipped entirely and the draft passes through as the spec."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="test_gap")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(
            decision="REFINE", reason="needs minor wording polish"
        ),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical rename — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert out.note.startswith("mechanical draft fast-path")
    assert "APPROVE" in out.note
    assert calls == []  # full refine agent never invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert {"file": "src/foo/bar.py", "note": "from draft"} in file_map


def test_mechanical_draft_fast_path_triggered_for_user_tickets(
    ctx_factory, monkeypatch, tmp_path
):
    """Human-written tickets with mechanical drafts now take the
    mechanical fast-path when auto-approve confirms no design
    decisions — the expensive refine agent is skipped."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="user")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="APPROVE",
            reason="Mechanical rename — no design decisions",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    # Should hit the fast-path; full refine agent should NOT be invoked.
    assert out.note.startswith("mechanical draft fast-path")
    assert "APPROVE" in out.note
    assert calls == []  # full refine agent never invoked


def test_mechanical_draft_fast_path_not_triggered_for_ci_tickets(
    ctx_factory, monkeypatch, tmp_path
):
    """CI-failure tickets never take the mechanical fast-path."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="ci")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Fix the SHA in `.github/workflows/ci.yml`.",
    )

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_not_triggered_when_auto_approve_disabled(
    ctx_factory, monkeypatch, tmp_path
):
    """The fast-path is gated on auto_approve_enabled=True."""
    ctx = ctx_factory(auto_approve_enabled=False)
    t = _ticket(ctx, source="test_gap")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_short_circuits_on_needs_approval(
    ctx_factory, monkeypatch, tmp_path
):
    """When auto-approve returns NEEDS_APPROVAL, the mechanical fast-path
    still short-circuits — the draft is preserved as-is and the expensive
    refine agent is skipped.  The ticket routes to HUMAN_ISSUE_APPROVAL
    via _resolved_outcome so the human reviewer sees why."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="periodic")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )
    from robotsix_mill.agents.refining import AutoApproveResult

    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="New API contract introduced",
        ),
    )

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Add a new public endpoint with authentication.",
    )

    # Fast-path short-circuit fires even on NEEDS_APPROVAL.
    assert out.note.startswith("mechanical draft fast-path")
    assert "NEEDS_APPROVAL" in out.note
    assert "New API contract introduced" in out.note
    assert calls == []  # full refine agent was NOT invoked
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_mechanical_draft_fast_path_falls_through_on_auto_approve_error(
    ctx_factory, monkeypatch, tmp_path
):
    """When the auto-approve call raises, fall through gracefully."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="periodic")
    calls = _spy_refine(
        monkeypatch,
        triage_refine=_mock_triage(decision="REFINE", reason="needs refinement"),
    )

    def _boom(**kw):
        raise RuntimeError("OpenRouter transient error")

    monkeypatch.setattr(refining, "triage_auto_approve", _boom)

    out = _run_agent(
        ctx,
        t,
        tmp_path,
        draft="Rename `old_func` to `new_func` in `src/foo/bar.py`.",
    )

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


# ===========================================================================
# _no_change_path — external-fix claim re-verification branch
# ===========================================================================


def test_no_change_external_fix_claim_routes_to_implement(
    ctx_factory, monkeypatch, tmp_path
):
    ctx = ctx_factory()
    t = _ticket(ctx)  # no branch
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                no_change_needed=True,
                no_change_rationale=(
                    "The fix was **already shipped** in commit abc1234; "
                    "nothing to change."
                ),
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.next_state is not State.DONE
    assert "unverified 'already implemented' claim routed to implement" in out.note
    desc = ctx.service.workspace(t).description_path.read_text(encoding="utf-8")
    assert "re-verify before closing" in desc
    assert "## Acceptance criteria" in desc


# ===========================================================================
# _apply_agent_side_effects
# ===========================================================================


def test_side_effect_applies_agent_title(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(spec_markdown=_REAL_SPEC, title="New title")
        ),
    )

    _run_agent(ctx, t, tmp_path)

    assert ctx.service.get(t.id).title == "New title"


def test_side_effect_writes_reference_files(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(spec_markdown=_REAL_SPEC, reference_files=["a.py", "b.py"])
        ),
    )

    _run_agent(ctx, t, tmp_path)

    ws = ctx.service.workspace(t)
    ref_path = ws.artifacts_dir / "reference_files.json"
    assert ref_path.exists()
    assert json.loads(ref_path.read_text(encoding="utf-8")) == [
        {"path": "a.py"},
        {"path": "b.py"},
    ]


def test_side_effect_persists_updated_memory(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    persisted: list[str] = []
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(spec_markdown=_REAL_SPEC, updated_memory="new memory")
        ),
        persist_memory=lambda s, memory_board_id, text: persisted.append(text),
    )

    _run_agent(ctx, t, tmp_path)

    assert persisted == ["new memory"]


def test_side_effect_writes_file_map(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                spec_markdown=_REAL_SPEC,
                file_map=[FileMapEntry(file="x.py", note="n")],
            )
        ),
    )
    # Non-gitignored repo — the deliverable path is git-tracked.
    monkeypatch.setattr(git_ops, "ignored_paths", lambda repo, paths: [])

    _run_agent(ctx, t, tmp_path)

    ws = ctx.service.workspace(t)
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert file_map == [{"file": "x.py", "note": "n"}]


# ===========================================================================
# _multi_scope_path
# ===========================================================================


def test_multi_scope_resolves_depends_on_chain(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="C0", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="C1", spec_markdown=_REAL_SPEC, depends_on=[0]),
                    ChildSpec(title="C2", spec_markdown=_REAL_SPEC, depends_on=[1]),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.CLOSED
    child_ids = out.note.removeprefix("split into ").split(", ")
    assert len(child_ids) == 3

    def _deps(cid):
        raw = ctx.service.get(cid).depends_on
        return json.loads(raw) if raw else []

    assert _deps(child_ids[0]) == []
    assert _deps(child_ids[1]) == [child_ids[0]]
    assert _deps(child_ids[2]) == [child_ids[1]]


def test_multi_scope_reparents_under_existing_epic(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    epic = ctx.service.create("Epic umbrella", "epic body", kind=TicketKind.EPIC)
    t = _ticket(ctx, parent_id=epic.id)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path, epic_ctx="")

    assert out.next_state is State.CLOSED
    child_ids = out.note.removeprefix("split into ").split(", ")
    for cid in child_ids:
        assert ctx.service.get(cid).parent_id == epic.id
    # No NEW umbrella epic was created — only the pre-existing one.
    epics = [tk for tk in ctx.service.list() if tk.kind == TicketKind.EPIC]
    assert len(epics) == 1
    assert epics[0].id == epic.id


def test_multi_scope_creates_new_umbrella_epic(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)  # no parent
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.CLOSED
    child_ids = out.note.removeprefix("split into ").split(", ")
    epics = [tk for tk in ctx.service.list() if tk.kind == TicketKind.EPIC]
    assert len(epics) == 1
    umbrella = epics[0]
    for cid in child_ids:
        assert ctx.service.get(cid).parent_id == umbrella.id


def test_multi_scope_no_valid_children_blocks(ctx_factory, monkeypatch, tmp_path):
    ctx = ctx_factory()
    t = _ticket(ctx)
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="Has title", spec_markdown="   "),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.BLOCKED
    assert out.note == "refiner produced no valid split children"


def test_split_children_inherit_parent_board_id(ctx_factory, monkeypatch, tmp_path):
    """When a ticket is split, children are stamped with the parent's
    ``board_id`` — not the service's bound board_id."""
    ctx = ctx_factory()
    t = _ticket(ctx, board_id="parent-board")
    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_returns(
            RefineResult(
                split=True,
                children=[
                    ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                    ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
                ],
            )
        ),
    )

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state is State.CLOSED
    child_ids = out.note.removeprefix("split into ").split(", ")
    assert len(child_ids) == 2
    for cid in child_ids:
        assert ctx.service.get(cid).board_id == "parent-board"

    # The umbrella epic should also carry the parent's board_id.
    # list() is bound to "test-board", so find the epic via the
    # child's parent_id instead.
    child = ctx.service.get(child_ids[0])
    assert child.parent_id is not None
    epic = ctx.service.get(child.parent_id)
    assert epic.kind == TicketKind.EPIC
    assert epic.board_id == "parent-board"


# ===========================================================================
# _ack_threads / reviewer-comment suppression of the conciseness review
# ===========================================================================


def test_ack_threads_and_review_suppression_single_scope(
    ctx_factory, monkeypatch, tmp_path
):
    # spec_review_enabled so the conciseness review *would* run absent
    # reviewer comments — proving the suppression is reviewer-driven.
    ctx = ctx_factory(spec_review_enabled=True)
    t = _ticket(ctx)
    c = ctx.service.add_comment(t.id, "Please tighten the scope.", author="user")

    acked: list[set[int]] = []
    monkeypatch.setattr(
        orch_module,
        "acknowledge_unanswered_threads",
        lambda ctx_, ticket_, thread_ids: acked.append(set(thread_ids)),
    )

    review_calls: list[int] = []

    def _review(*, settings, spec_markdown, **kw):
        review_calls.append(1)
        return SpecReviewResult(concise_spec=_CONCISE_SPEC, stripped_summary="x")

    _apply_default_mocks(monkeypatch, review_spec_for_conciseness=_review)

    out = _run_agent(ctx, t, tmp_path)

    assert out.note.startswith("refined")
    # Open thread acknowledged at outcome time.
    assert acked == [{c.id}]
    # Conciseness review skipped because reviewer comments were present.
    assert review_calls == []
    assert (
        ctx.service.workspace(t).description_path.read_text(encoding="utf-8")
        == _REAL_SPEC
    )


# ===========================================================================
# Trivial-scope routing tests (AC: orchestration routing)
# ===========================================================================


def test_trivial_scope_routes_to_cheap_model(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=True and the feature flag is on,
    run_refine_agent receives refine_level=s.refine_trivial_model_level."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial one-liner",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level}, "
        f"got {refine_kwargs.get('refine_level')}"
    )
    # Cheap route: refining model is the subscription alias (sonnet).
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"on cheap route, got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"on cheap route, got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_non_trivial_scope_routes_to_none(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=False, refine_level is None (Opus default)."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file refactor",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None for non-trivial, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_flag_off_forces_none_regardless_of_verdict(ctx_factory, monkeypatch, tmp_path):
    """When refine_trivial_routing_enabled=False, refine_level is always None
    even when triage returns trivial_scope=True."""
    ctx = ctx_factory(refine_trivial_routing_enabled=False)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial but flag off",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when flag is off, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_missing_verdict_defaults_to_false(ctx_factory, monkeypatch, tmp_path):
    """When no triage artifact is written (e.g. reviewer sendback, triage
    disabled), _read_triage_trivial returns False, refine_level=None."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="normal refine",
            complexity="needs-exploration",
            trivial_scope=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when trivial_scope is None, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_trivial_scope_true_persisted_to_artifact(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=True, the artifact file records it."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial",
            complexity="simple",
            trivial_scope=True,
        ),
    )

    _run_agent(ctx, t, tmp_path)

    import json
    from robotsix_mill.stages.refine.orchestration import _read_triage_trivial

    ws = ctx.service.workspace(t)
    data = json.loads(
        (ws.artifacts_dir / "triage_complexity.json").read_text(encoding="utf-8")
    )
    assert data.get("trivial_scope") is True
    assert _read_triage_trivial(ws) is True


def test_triage_findings_forwarded_to_run_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When triage returns exploration_findings, the value reaches
    run_refine_agent via the triage_findings keyword argument."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file",
            complexity="needs-exploration",
            exploration_findings="- Verified `src/foo.py` exists (342 lines)\n",
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("triage_findings") == (
        "- Verified `src/foo.py` exists (342 lines)\n"
    )


def test_triage_findings_none_not_forwarded_to_run_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When triage does not populate exploration_findings (None),
    run_refine_agent receives triage_findings=None."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple change",
            complexity="simple",
            exploration_findings=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("triage_findings") is None


# ===========================================================================
# Findings-present downgrade: Opus -> cheaper Claude alias
# ===========================================================================


def test_findings_present_downgrades_opus_to_sonnet(ctx_factory, monkeypatch, tmp_path):
    """When complexity="needs-exploration" and triage produced substantial
    exploration findings, the refine_model is downgraded from Opus to the
    findings model alias (default "sonnet")."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file refactor",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_findings
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_findings!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "refine_level must be None (still level 3 / Claude-SDK)"
    )


def test_short_findings_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When exploration_findings is below refine_findings_downgrade_min_chars,
    the Opus model is kept."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="too short",
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )


def test_no_findings_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When exploration_findings is None (absent or unparseable artifact),
    the Opus model is kept."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )


def test_findings_downgrade_flag_off_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When refine_findings_downgrade_enabled=False, substantial findings
    do NOT trigger the downgrade -- Opus is kept (prior behaviour)."""
    ctx = ctx_factory(refine_findings_downgrade_enabled=False)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r} "
        f"(flag off), got {refine_kwargs.get('refine_model')!r}"
    )


def test_simple_complexity_unaffected_by_findings(ctx_factory, monkeypatch, tmp_path):
    """Regression: complexity="simple" is unaffected by the findings
    downgrade -- the elif chain keeps the simple path intact."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple fix",
            complexity="simple",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_default
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_default!r} "
        f"(simple path), got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") == (
        ctx.settings.refine_request_limit_simple
    ), "request_limit_override must be set on the simple path"


# ===========================================================================
# Re-refine round counter → force cheap model after threshold
# ===========================================================================


def _add_sendback_event(ctx, ticket, body="fix the scope"):
    """Create a "changes requested:" history event mirroring what
    ``Service.request_changes`` writes.  Uses ``add_history_note``
    because the ticket is already in DRAFT — ``transition`` would
    reject DRAFT→DRAFT as a no-op transition."""
    ctx.service.add_history_note(
        ticket.id,
        f"changes requested: {body}",
    )


def test_re_refine_counter_forces_cheap_after_threshold(
    ctx_factory, monkeypatch, tmp_path
):
    """A ticket with ≥ max_re_refine_cycles_before_cheap sendback events
    and a non-trivial triage verdict routes to the cheap model."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=2)
    t = _ticket(ctx)

    # Simulate 2 prior "changes requested" sendbacks — at threshold.
    _add_sendback_event(ctx, t, "first round feedback")
    _add_sendback_event(ctx, t, "second round feedback")

    # Add an open reviewer comment so the sendback path activates.
    ctx.service.add_comment(t.id, "Please revise the scope.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    # Triage returns non-trivial so the trivial-routing block leaves
    # refine_level=None — the counter must force the downgrade.
    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"(cheap) after {ctx.settings.max_re_refine_cycles_before_cheap} "
        f"sendbacks, got {refine_kwargs.get('refine_level')}"
    )
    # Forced-cheap route: refining model is the subscription alias.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"on forced-cheap route, got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") == max(
        int(
            ctx.settings.refine_request_limit_simple
            * ctx.settings.refine_dynamic_limit_multiplier
        ),
        ctx.settings.refine_dynamic_limit_min,
    ), (
        f"Expected dynamic request_limit_override for forced-cheap route, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )
    # Exploration sub-agents must be disabled on sendback.
    assert refine_kwargs.get("include_explore") is False
    assert refine_kwargs.get("include_parallel_explore") is False


def test_re_refine_below_threshold_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """A ticket with fewer than max_re_refine_cycles_before_cheap sendbacks
    and a non-trivial verdict keeps refine_level=None (full Opus)."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=2)
    t = _ticket(ctx)

    # Only 1 prior sendback — below the default threshold of 2.
    _add_sendback_event(ctx, t, "one round of feedback")

    ctx.service.add_comment(t.id, "Please adjust.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None (full Opus) when below threshold, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_re_refine_first_run_trivial_stays_cheap(ctx_factory, monkeypatch, tmp_path):
    """Regression: a re-refine where triage_complexity.json from the
    first round has trivial_scope=true stays on the cheap model
    regardless of the re-refine counter."""
    ctx = ctx_factory(
        max_re_refine_cycles_before_cheap=2,
        refine_trivial_routing_enabled=True,
    )
    t = _ticket(ctx)

    # Write the first-run triage artifact (simulating a prior round).
    ws = ctx.service.workspace(t)
    import json as _json

    (ws.artifacts_dir / "triage_complexity.json").write_text(
        _json.dumps({"complexity": "simple", "trivial_scope": True}),
        encoding="utf-8",
    )

    # Simulate 2 prior sendbacks (at threshold) — but the persisted
    # trivial verdict should keep the cheap model regardless.
    _add_sendback_event(ctx, t, "feedback 1")
    _add_sendback_event(ctx, t, "feedback 2")

    ctx.service.add_comment(t.id, "Revise.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    # Triage is skipped (reviewer_comments present), so the triage mock
    # is irrelevant — the trivial-routing block reads from the artifact.
    _apply_default_mocks(monkeypatch, run_refine_agent=_run)

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"(cheap) from persisted first-run trivial verdict, "
        f"got {refine_kwargs.get('refine_level')}"
    )
    # Persisted trivial verdict → cheap route with subscription alias.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"from persisted trivial verdict, got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"from persisted trivial verdict, got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_re_refine_counter_disabled_by_zero(ctx_factory, monkeypatch, tmp_path):
    """When max_re_refine_cycles_before_cheap=0, the counter-forced
    downgrade is disabled — even with many sendbacks, refine_level
    stays None (full Opus) for non-trivial tickets."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=0)
    t = _ticket(ctx)

    # Many sendbacks, but threshold is 0 (disabled).
    for i in range(5):
        _add_sendback_event(ctx, t, f"feedback {i}")

    ctx.service.add_comment(t.id, "Revise again.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when counter is disabled (threshold=0), "
        f"got {refine_kwargs.get('refine_level')}"
    )


# ===========================================================================
# MIGRATE triage branch tests
# ===========================================================================


def _install_migrate_spy(monkeypatch, ctx):
    """Install a spy on ``ctx.service.migrate`` and return the calls list."""
    calls: list[tuple] = []

    def _spy(ticket_id, target_board, note=None):
        calls.append((ticket_id, target_board, note))
        return ctx.service.get(ticket_id)

    monkeypatch.setattr(ctx.service, "migrate", _spy)
    return calls


def _mock_repos_config(monkeypatch, extra_repos=None):
    """Patch ``get_repos_config`` with a registry that includes the default
    ``test-board`` / ``test-repo`` plus any *extra_repos*."""
    from robotsix_mill.config import ReposRegistry, RepoConfig

    repos = {
        "test-repo": RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    }
    if extra_repos:
        repos.update(extra_repos)
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config",
        lambda: ReposRegistry(repos=repos),
    )


def _mock_triage_migrate(
    monkeypatch, target_board="other-board", reason="belongs elsewhere"
):
    """Install a triage_refine stub that returns MIGRATE with *target_board*."""
    from robotsix_mill.agents.refining import TriageResult

    def _triage(*, settings, title, draft, **kw):
        return TriageResult(
            decision="MIGRATE",
            reason=reason,
            target_board=target_board,
        )

    monkeypatch.setattr("robotsix_mill.agents.refining.triage_refine", _triage)


def test_triage_migrate_success_calls_migrate_and_returns_draft(
    ctx_factory, monkeypatch, tmp_path
):
    """A MIGRATE triage with a valid target board on a fresh ticket calls
    ctx.service.migrate and returns Outcome(State.DRAFT, ...)."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )
    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="other-board")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 1
    assert migrate_calls[0][0] == t.id
    assert migrate_calls[0][1] == "other-board"
    assert migrate_calls[0][2] == "belongs elsewhere"
    assert out.next_state is State.DRAFT
    assert "migrated to board 'other-board'" in out.note
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert (ws.artifacts_dir / "file_map.json").exists()


def test_triage_migrate_invalid_target_none_escalates_to_human(
    ctx_factory, monkeypatch, tmp_path
):
    """When target_board is None, migrate is NOT called and the ticket
    escalates to human (same as SKIP path)."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    _mock_repos_config(monkeypatch)
    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board=None, reason="should go elsewhere")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "MIGRATE invalid target" in out.note


def test_triage_migrate_invalid_target_unknown_escalates(
    ctx_factory, monkeypatch, tmp_path
):
    """When target_board names a board not in the registry, escalate."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    _mock_repos_config(monkeypatch)
    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="no-such-board")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "MIGRATE invalid target" in out.note


def test_triage_migrate_same_board_escalates_to_human(
    ctx_factory, monkeypatch, tmp_path
):
    """When target_board equals the current board, migrate is NOT called."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    _mock_repos_config(monkeypatch)
    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="test-board", reason="same board")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "MIGRATE invalid target" in out.note


def test_triage_migrate_anti_bounce_prior_migration_escalates(
    ctx_factory, monkeypatch, tmp_path
):
    """A ticket whose history already contains a migration event must NOT
    be migrated again — escalate to human."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )
    # Add a prior migration event to the ticket history.
    ctx.service.add_history_note(
        t.id,
        "migrated from board 'auto-mail' to 'test-board'",
    )
    t = ctx.service.get(t.id)

    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="other-board")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "anti-bounce" in out.note.lower()


def test_triage_migrate_anti_bounce_target_is_prior_board_escalates(
    ctx_factory, monkeypatch, tmp_path
):
    """When the chosen target_board is a board the ticket has previously
    been on, migrate is blocked — prevents ping-pong."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )
    # Add a prior migration event FROM other-board TO test-board.
    # This means other-board is a prior board of this ticket.
    ctx.service.add_history_note(
        t.id,
        "migrated from board 'other-board' to 'test-board'",
    )
    t = ctx.service.get(t.id)

    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(
        monkeypatch, target_board="other-board", reason="back to other-board"
    )
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "anti-bounce" in out.note.lower()


def test_triage_migrate_value_error_escalates_to_human(
    ctx_factory, monkeypatch, tmp_path
):
    """When ctx.service.migrate raises ValueError, the branch catches it
    and escalates to human instead of crashing."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )

    def _failing_migrate(ticket_id, target_board, note=None):
        raise ValueError("unknown target board")

    monkeypatch.setattr(ctx.service, "migrate", _failing_migrate)
    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="other-board")

    out = _run_agent(ctx, t, tmp_path)

    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "MIGRATE failed" in out.note


def test_triage_migrate_resolves_repo_id_to_board(ctx_factory, monkeypatch, tmp_path):
    """target_board can be a repo_id; the validation resolves it to the
    corresponding board_id before migrating."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )
    _apply_default_mocks(monkeypatch)
    # Pass repo_id "other-repo" as target_board — should resolve to "other-board"
    _mock_triage_migrate(
        monkeypatch, target_board="other-repo", reason="belongs to other-repo"
    )
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    assert len(migrate_calls) == 1
    # Should be resolved to board_id "other-board", not the raw repo_id.
    assert migrate_calls[0][1] == "other-board"
    assert out.next_state is State.DRAFT


def test_triage_migrate_note_suffix_parsing_and_anti_bounce(
    ctx_factory, monkeypatch, tmp_path
):
    """Migration notes with suffixes like ' (was implementing)' or ': note'
    are parsed correctly for prior-board extraction, and the anti-bounce
    cap blocks a second migration."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    from robotsix_mill.config import RepoConfig

    _mock_repos_config(
        monkeypatch,
        extra_repos={
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        },
    )
    # Migration note WITH state suffix AND user note suffix — the
    # parsing must handle " (was <state>)" and ": <note>" correctly.
    ctx.service.add_history_note(
        t.id,
        "migrated from board 'auto-mail' to 'third-board' (was implementing): operator requested",
    )
    t = ctx.service.get(t.id)

    _apply_default_mocks(monkeypatch)
    _mock_triage_migrate(monkeypatch, target_board="other-board")
    migrate_calls = _install_migrate_spy(monkeypatch, ctx)

    out = _run_agent(ctx, t, tmp_path)

    # Anti-bounce: the ticket already has ≥ 1 migration event, so this
    # second automated reroot is blocked.
    assert len(migrate_calls) == 0
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert "anti-bounce" in out.note.lower()


# ===========================================================================
# _short_circuit_for_internal_failure
# ===========================================================================


def test_short_circuit_pytest_failure_returns_outcome(
    ctx_factory, monkeypatch, tmp_path
):
    """A draft containing a pytest failure output short-circuits refine:
    the method returns an Outcome (not None), the full refine agent is NOT
    invoked, draft-original.md and an empty file_map.json are written,
    complexity is set to 'simple', and the spec contains the failure excerpt."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = (
        "CI run failed.\n\n"
        "============================= FAILURES =============================\n"
        "FAILED tests/test_x.py::test_foo - AssertionError: expected 1 got 2\n"
        "========================= short test summary ========================\n"
        "FAILED tests/test_x.py::test_foo\n"
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent never invoked.
    assert calls == []

    # An Outcome is returned, not None.
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")

    # Artifacts written.
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert (ws.artifacts_dir / "file_map.json").exists()
    file_map = json.loads(
        (ws.artifacts_dir / "file_map.json").read_text(encoding="utf-8")
    )
    assert file_map == []

    # Complexity recorded "simple".
    complexity_path = ws.artifacts_dir / "triage_complexity.json"
    assert complexity_path.exists()
    complexity_data = json.loads(complexity_path.read_text(encoding="utf-8"))
    assert complexity_data["complexity"] == "simple"

    # The spec markdown contains the failure excerpt.
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "FAILURES" in spec
    assert "test_foo" in spec
    assert "internal toolchain failure" in spec.lower()


def test_short_circuit_mypy_failure_returns_outcome(ctx_factory, monkeypatch, tmp_path):
    """A draft containing a mypy error short-circuits refine."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = (
        "mypy run failed.\n\n"
        'src/foo.py:12: error: Argument 1 to "bar" has incompatible type '
        '"int"; expected "str"  [arg-type]\n'
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert calls == []
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")

    ws = ctx.service.workspace(t)
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "[arg-type]" in spec


def test_short_circuit_ruff_failure_returns_outcome(ctx_factory, monkeypatch, tmp_path):
    """A draft containing ruff failure output short-circuits refine."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = "ruff check failed.\n\nF401 imported but unused\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert calls == []
    assert out is not None
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)
    assert out.note.startswith("short-circuited refine")


def test_short_circuit_no_failure_markers_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """When the draft has no internal toolchain failure markers, the
    short-circuit returns None and the full refine agent runs."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    draft = "We should add a new feature to the widget loader. It should handle edge cases better."

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent ran.
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_short_circuit_reviewer_comments_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """When reviewer comments are present, the short-circuit is skipped
    even when the draft contains failure markers — human-flagged changes
    always get full refinement."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(
        t.id, "Please also handle the edge case with None input.", author="user"
    )
    calls = _spy_refine(monkeypatch)

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent ran despite the failure markers — reviewer comments
    # take priority.
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_short_circuit_empty_draft_falls_through(ctx_factory, monkeypatch, tmp_path):
    """An empty draft (or whitespace-only) does not short-circuit."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    calls = _spy_refine(monkeypatch)

    _run_agent(ctx, t, tmp_path, draft="   ")

    # Full refine agent ran.
    assert len(calls) == 1


def test_short_circuit_routes_to_implement_not_done_or_maintenance(
    ctx_factory, monkeypatch, tmp_path
):
    """The short-circuited outcome MUST route toward implement
    (READY / HUMAN_ISSUE_APPROVAL), never to DONE or MAINTENANCE."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    _spy_refine(monkeypatch)

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out.next_state not in (State.DONE, State.MAINTENANCE, State.CLOSED)
    assert out.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)


def test_short_circuit_with_evidence_file(ctx_factory, monkeypatch, tmp_path):
    """When ws.artifacts_dir / 'evidence.txt' exists, its content is
    embedded in the generated spec."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    _spy_refine(monkeypatch)

    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "evidence.txt").write_text(
        "Traceback (most recent call last):\n  File ...\nRuntimeError: boom\n",
        encoding="utf-8",
    )

    draft = "CI run failed.\n\nFAILED tests/test_x.py::test_foo - AssertionError\n"

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    assert out is not None
    ws = ctx.service.workspace(t)
    spec = ws.description_path.read_text(encoding="utf-8")
    assert "evidence.txt" in spec
    assert "RuntimeError: boom" in spec


def test_short_circuit_static_method_direct_call(ctx_factory, tmp_path):
    """Drive _short_circuit_for_internal_failure directly (not through
    _run_refine_agent) to test the gating logic in isolation."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Case 1: Internal failure draft, no reviewer comments → Outcome returned.
    draft = "FAILED tests/test_x.py::test_foo - AssertionError\n"
    outcome = RefineStage._short_circuit_for_internal_failure(
        ctx, t, draft, ws, ctx.settings, reviewer_comments=None
    )
    assert outcome is not None
    assert outcome.next_state in (State.READY, State.HUMAN_ISSUE_APPROVAL)

    # Case 2: Reviewer comments present → None returned.
    outcome2 = RefineStage._short_circuit_for_internal_failure(
        ctx, t, draft, ws, ctx.settings, reviewer_comments="Please fix scope."
    )
    assert outcome2 is None

    # Case 3: No failure markers → None returned.
    outcome3 = RefineStage._short_circuit_for_internal_failure(
        ctx,
        t,
        "Add a new feature to the loader.",
        ws,
        ctx.settings,
        reviewer_comments=None,
    )
    assert outcome3 is None

    # Case 4: Empty draft → None returned.
    outcome4 = RefineStage._short_circuit_for_internal_failure(
        ctx, t, "", ws, ctx.settings, reviewer_comments=None
    )
    assert outcome4 is None


# ===========================================================================
# Complexity-gated Claude alias routing (subscription tier routing)
# ===========================================================================


def test_subscription_tier_routing_simple_uses_sonnet(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=True and
    complexity='simple', run_refine_agent receives refine_model='sonnet'
    and request_limit_override=40."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple one-liner",
            complexity="simple",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == "sonnet", (
        f"Expected refine_model='sonnet' for simple complexity, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "Expected no refine_level downgrade for non-trivial ticket"
    )
    assert refine_kwargs.get("request_limit_override") == 40, (
        f"Expected request_limit_override=40 for simple path, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_subscription_tier_routing_needs_exploration_uses_opus(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=True and
    complexity='needs-exploration', run_refine_agent receives
    refine_model='opus' and no request_limit_override."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file change needs deep exploration",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == "opus", (
        f"Expected refine_model='opus' for needs-exploration, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "Expected no refine_level downgrade for non-trivial ticket"
    )
    # Dynamic limit fires for non-simple complexity: base=80, ×1.5 → 120.
    assert refine_kwargs.get("request_limit_override") == max(
        int(
            ctx.settings.refine_request_limit
            * ctx.settings.refine_dynamic_limit_multiplier
        ),
        ctx.settings.refine_dynamic_limit_min,
    ), "Expected dynamic request_limit_override for needs-exploration path"


def test_subscription_tier_routing_disabled_no_alias(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=False,
    run_refine_agent receives refine_model=None (Opus-always,
    today's behaviour)."""
    ctx = ctx_factory()
    # Disable the feature flag.
    ctx.settings.refine_subscription_tier_routing_enabled = False
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple change",
            complexity="simple",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") is None, (
        f"Expected refine_model=None when feature flag is off, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") is None, (
        "Expected no request_limit_override when feature flag is off"
    )


def test_dynamic_limit_fires_on_large_draft(ctx_factory, monkeypatch, tmp_path):
    """When the draft exceeds refine_dynamic_limit_spec_chars, the dynamic
    request_limit_override is applied even for simple-complexity tickets."""
    ctx = ctx_factory()

    # Build a draft > 3000 chars so the length trigger fires.
    long_draft = "x" * (ctx.settings.refine_dynamic_limit_spec_chars + 1)
    t = _ticket(ctx, body=long_draft)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple change",
            complexity="simple",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path, draft=long_draft)

    # Even though complexity is "simple" (which normally sets
    # request_limit_override to refine_request_limit_simple via the
    # tier-routing path), the large draft triggers the dynamic override.
    expected = max(
        int(
            ctx.settings.refine_request_limit_simple
            * ctx.settings.refine_dynamic_limit_multiplier
        ),
        ctx.settings.refine_dynamic_limit_min,
    )
    assert refine_kwargs.get("request_limit_override") == expected, (
        f"Expected dynamic request_limit_override={expected} for large draft, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_trivial_scope_unchanged_by_tier_routing(ctx_factory, monkeypatch, tmp_path):
    """trivial_scope=True routes to refines via the cheap route (level 3
    subscription with sonnet), bypassing the subscription-tier complexity
    ladder — the cheap route is independent of tier routing."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="single-line mechanical change",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    # Trivial → level 3 (Claude subscription)
    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"for trivial ticket, "
        f"got {refine_kwargs.get('refine_level')!r}"
    )
    # Cheap route → sonnet on the subscription, not the tier-routing ladder.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"for trivial (subscription cheap route), "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"for trivial cheap route, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )


# ===========================================================================
# Deterministic-source mechanical fast-path
# ===========================================================================


def test_mechanical_fast_path_deterministic_source_skips_llm(
    ctx_factory, monkeypatch, tmp_path
):
    """A ticket whose source is in _AUTO_APPROVE_SOURCES (e.g. "audit")
    short-circuits both triage_auto_approve and run_refine_agent, returning
    READY with a "deterministic source" / "skipped refine LLM" note."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="audit")
    calls = _spy_refine(monkeypatch)

    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="ok")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    draft = "some draft with file paths like `src/foo.py` and `tests/test_bar.py`"
    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # triage_auto_approve was never called (deterministic shortcut).
    assert auto_approve_calls == []
    # run_refine_agent was never called.
    assert calls == []
    # Routed to READY.
    assert out.next_state == State.READY
    # The source is in the deterministic set (guard against drift).
    assert "audit" in _AUTO_APPROVE_SOURCES
    # Note carries the expected markers.
    assert "deterministic source" in out.note
    assert "skipped refine LLM" in out.note

    # Artifacts written.
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()

    file_map_path = ws.artifacts_dir / "file_map.json"
    assert file_map_path.exists()
    file_map = json.loads(file_map_path.read_text(encoding="utf-8"))
    assert {"file": "src/foo.py", "note": "from draft"} in file_map
    assert {"file": "tests/test_bar.py", "note": "from draft"} in file_map


def test_mechanical_fast_path_non_deterministic_calls_bounded_triage(
    ctx_factory, monkeypatch, tmp_path
):
    """A non-deterministic-source mill-internal ticket calls triage_auto_approve
    with the *bounded* summary (truncated via _summarize_spec_for_auto_approve),
    not the full raw draft."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="retrospect")
    calls = _spy_refine(monkeypatch)

    title = "Test bounded triage spec"
    # Build a draft long enough that truncation actually occurs (>2000 chars).
    long_draft = "x" * 2500

    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="mechanical")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    out = _run_agent(ctx, t, tmp_path, draft=long_draft, title=title)

    # triage_auto_approve was called (at least once from the fast-path;
    # _resolve_next_state may also call it again for non-deterministic sources).
    assert len(auto_approve_calls) >= 1
    # The first call (from the mechanical fast-path) receives the bounded summary.
    expected_spec = _summarize_spec_for_auto_approve(f"{t.title}\n\n{long_draft}")
    assert auto_approve_calls[0]["spec"] == expected_spec
    # Truncation actually happened (the passed spec is shorter than the raw draft).
    assert len(auto_approve_calls[0]["spec"]) < len(long_draft)
    # run_refine_agent was never called.
    assert calls == []
    # The outcome routes to implement.
    assert out.next_state == State.READY


def test_mechanical_fast_path_exception_falls_through(
    ctx_factory, monkeypatch, tmp_path
):
    """When triage_auto_approve raises inside the mechanical fast-path,
    the exception is caught and the full refine agent runs (fall-through)."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="retrospect")

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(refining, "triage_auto_approve", _boom)

    calls = _spy_refine(monkeypatch)

    out = _run_agent(ctx, t, tmp_path)

    # Full refine agent ran (fall-through after exception).
    assert len(calls) == 1
    assert out.note.startswith("refined")


# ===========================================================================
# CI-source mechanical fast-path admission (for complete-spec CI tickets)
# ===========================================================================


def test_ci_source_complete_spec_admitted_to_fast_path(
    ctx_factory, monkeypatch, tmp_path
):
    """A source="ci" ticket whose draft is a complete spec (## Problem +
    ## Scope) is admitted to the mechanical draft fast-path: the full
    refine agent is NOT called, and the outcome routes via the bounded
    auto-approve branch."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source=SourceKind.CI)
    calls = _spy_refine(monkeypatch)

    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="mechanical")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    draft = (
        "## Problem\n\nThe CI publish workflow fails because the GHCR "
        "token is missing `packages: write` scope.\n\n"
        "## Scope\n\nAdd `packages: write` to the `GITHUB_TOKEN` "
        "permissions block in `.github/workflows/publish.yml`.\n\n"
        "## Acceptance criteria\n\n- The publish workflow passes on main.\n"
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent never invoked.
    assert calls == []

    # triage_auto_approve was called (the bounded safety gate).
    assert len(auto_approve_calls) >= 1

    # The outcome is READY (APPROVE from auto-approve).
    assert out.next_state == State.READY

    # The note reflects the mechanical fast-path.
    assert "mechanical draft fast-path" in out.note
    assert "auto-approve APPROVE" in out.note

    # Artifacts written.
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    assert (ws.artifacts_dir / "file_map.json").exists()


def test_ci_source_incomplete_draft_falls_through_to_refine(
    ctx_factory, monkeypatch, tmp_path
):
    """A source="ci" ticket whose draft has no ## Scope heading (and is
    NOT an internal toolchain failure — so the _short_circuit_for_internal_failure
    path doesn't intercept it) falls through to the full refine agent."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source=SourceKind.CI)
    calls = _spy_refine(monkeypatch)

    auto_approve_calls: list[dict] = []

    def _spy_auto_approve(**kw):
        auto_approve_calls.append(kw)
        return refining.AutoApproveResult(decision="APPROVE", reason="mechanical")

    monkeypatch.setattr(refining, "triage_auto_approve", _spy_auto_approve)

    # Vague CI ticket — no ## Problem / ## Scope headings, and no
    # internal toolchain failure markers (so _short_circuit_for_internal_failure
    # also returns None).
    draft = (
        "The CI pipeline for the publish workflow is broken. "
        "We need to investigate what changed and fix it."
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent was invoked (fell through past both the mechanical
    # fast-path AND the internal-failure short-circuit).
    assert len(calls) == 1
    assert out.note.startswith("refined")


def test_user_source_with_complete_spec_admitted_to_fast_path(
    ctx_factory, monkeypatch, tmp_path
):
    """A source="user" ticket with a complete spec is now admitted to
    the mechanical fast-path; the full refine agent is skipped."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="user")
    calls = _spy_refine(monkeypatch)

    # The fast-path calls triage_auto_approve on the draft itself
    # (skipping refine).  _resolve_next_state also calls it on the
    # outcome spec — mock both to prevent real LLM calls.
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **kw: refining.AutoApproveResult(
            decision="APPROVE", reason="mechanical"
        ),
    )

    draft = (
        "## Problem\n\nThe widget does not retry on 503.\n\n"
        "## Scope\n\nAdd retry logic to loader.py.\n\n"
        "## Acceptance criteria\n\n- Retries up to 3 times.\n"
    )

    out = _run_agent(ctx, t, tmp_path, draft=draft)

    # Full refine agent was NOT invoked — mechanical fast-path skipped it.
    assert len(calls) == 0
    assert "mechanical draft fast-path" in out.note.lower()


# ===========================================================================
# Delta-reuse on sendback re-entry (refine_delta_reuse_enabled)
# ===========================================================================


def _setup_sendback_ticket(ctx, prior_spec=_REAL_SPEC):
    """Create a ticket with a prior refined spec, a sendback event, and an
    open reviewer comment — simulating a sendback re-entry scenario."""
    t = _ticket(ctx)
    # Write the prior refined spec to description.md (as if a prior refine
    # pass completed).
    ws = ctx.service.workspace(t)
    ws.write_description(prior_spec)
    # Transition to HUMAN_ISSUE_APPROVAL then request_changes back to DRAFT
    # so the ticket history contains a "changes requested:" event.
    ctx.service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")
    ctx.service.request_changes(
        t.id, "Please narrow the scope to only the loader.", author="user"
    )
    # Re-fetch to get updated state.
    return ctx.service.get(t.id)


def test_delta_reuse_skips_reviewer_agreement(ctx_factory, monkeypatch, tmp_path):
    """When delta-reuse is enabled and this is a sendback re-entry,
    triage_reviewer_agreement must NOT be called."""
    ctx = ctx_factory(refine_delta_reuse_enabled=True)
    t = _setup_sendback_ticket(ctx)

    reviewer_agreement_called: list[dict] = []

    def _record_agreement(**kw):
        reviewer_agreement_called.append(kw)
        return refining.ReviewerAgreementResult(decision="DISAGREE", reason="recorded")

    _apply_default_mocks(
        monkeypatch,
        triage_reviewer_agreement=_record_agreement,
    )

    out = _run_agent(ctx, t, tmp_path)

    assert reviewer_agreement_called == []  # never called
    assert out.note.startswith("refined")


def test_delta_reuse_legacy_path_when_toggle_off(ctx_factory, monkeypatch, tmp_path):
    """When refine_delta_reuse_enabled=False, triage_reviewer_agreement
    still runs on sendback (legacy behaviour)."""
    ctx = ctx_factory(refine_delta_reuse_enabled=False)
    t = _setup_sendback_ticket(ctx)

    reviewer_agreement_called: list[dict] = []

    def _record_agreement(**kw):
        reviewer_agreement_called.append(kw)
        return refining.ReviewerAgreementResult(decision="DISAGREE", reason="recorded")

    _apply_default_mocks(
        monkeypatch,
        triage_reviewer_agreement=_record_agreement,
    )

    out = _run_agent(ctx, t, tmp_path)

    assert len(reviewer_agreement_called) == 1  # called in legacy path
    assert out.note.startswith("refined")


def test_delta_reuse_preserves_auto_approve_routing(ctx_factory, monkeypatch, tmp_path):
    """Even on delta-reuse, the post-refine auto-approve gate still runs
    and produces a valid next state (READY or HUMAN_ISSUE_APPROVAL)."""
    ctx = ctx_factory(
        refine_delta_reuse_enabled=True,
        auto_approve_enabled=True,
    )
    t = _setup_sendback_ticket(ctx)

    auto_approve_called: list[dict] = []

    def _record_auto_approve(**kw):
        auto_approve_called.append(kw)
        return refining.AutoApproveResult(
            decision="APPROVE", reason="no design decisions"
        )

    _apply_default_mocks(monkeypatch)
    monkeypatch.setattr(refining, "triage_auto_approve", _record_auto_approve)

    out = _run_agent(ctx, t, tmp_path)

    # The auto-approve gate runs inside _resolve_next_state after the
    # refine agent completes.  Only fires when require_approval=True
    # AND auto_approve_enabled=True.
    assert out.next_state is State.READY
    assert len(auto_approve_called) == 1


def test_delta_reuse_agent_receives_prior_spec(ctx_factory, monkeypatch, tmp_path):
    """On delta-reuse, the refine agent is fed the prior refined spec as
    its 'draft' input, not the original draft body."""
    ctx = ctx_factory(refine_delta_reuse_enabled=True)
    prior_spec = (
        "## Problem\n\nPrior refined spec for the loader.\n\n"
        "## Scope\n\nFix loader.py to raise on missing config.\n\n"
        "## Acceptance criteria\n\n- ConfigMissing raised.\n"
    )
    t = _setup_sendback_ticket(ctx, prior_spec=prior_spec)

    agent_input: list[dict] = []

    def _capture_input(**kw):
        agent_input.append(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_capture_input,
    )

    # Simulate what RefineStage.run() does: pass ws.read_description()
    # as the draft. On sendback re-entry this is the prior refined spec.
    out = _run_agent(ctx, t, tmp_path, draft=prior_spec)

    assert len(agent_input) == 1
    # The agent's 'draft' parameter must contain the prior spec, not the
    # original draft body from ticket creation.
    captured_draft = agent_input[0]["draft"]
    assert "Prior refined spec for the loader" in captured_draft
    assert "ConfigMissing raised" in captured_draft
    # Reviewer comments must also be present.
    assert agent_input[0]["reviewer_comments"]
    assert "narrow the scope" in agent_input[0]["reviewer_comments"]
    assert out.note.startswith("refined")


def test_delta_reuse_triage_refine_not_called(ctx_factory, monkeypatch, tmp_path):
    """On delta-reuse sendback, triage_refine must NOT be invoked
    from either RefineStage.run() or the _run_refine_agent fallback."""
    ctx = ctx_factory(refine_delta_reuse_enabled=True)
    t = _setup_sendback_ticket(ctx)

    triage_refine_called: list[dict] = []

    def _record_triage(**kw):
        triage_refine_called.append(kw)
        return TriageResult(decision="REFINE", reason="recorded")

    _apply_default_mocks(
        monkeypatch,
        triage_refine=_record_triage,
    )

    out = _run_agent(ctx, t, tmp_path)

    # triage_refine is called from _triage_skip. On sendback, _triage_skip
    # returns None immediately (reviewer_comments blocks it). The fallback
    # call from _run_refine_agent is also skipped due to delta-reuse guard.
    assert triage_refine_called == []
    assert out.note.startswith("refined")

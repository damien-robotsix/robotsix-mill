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
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.stages.refine import orchestration as orch_module
from robotsix_mill.stages.refine.helpers import UNMERGED_BRANCH_PREFIX
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

    created = []

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        created.append(s)
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


def _mock_spec_review(concise_spec=_CONCISE_SPEC, stripped_summary="stripped 3 lines"):
    def _run(*, settings, spec_markdown, **kw):
        del settings, spec_markdown, kw
        return SpecReviewResult(
            concise_spec=concise_spec, stripped_summary=stripped_summary
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
    monkeypatch.setattr(refining, "review_spec_for_conciseness", _mock_spec_review())

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
    monkeypatch.setattr(refining, "review_spec_for_conciseness", _mock_spec_review())

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
    assert ctx.service.get(t.id).kind == "epic"


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

    assert out.next_state is State.DONE
    assert out.note.startswith("no change needed — ")


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

    assert out.next_state is State.DONE
    assert out.note.startswith("reviewer agreement — no change needed:")
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

    assert out.next_state is State.DONE
    assert "NO_CHANGE" in out.note
    assert out.note.startswith("triage NO_CHANGE:")
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


def test_mechanical_draft_fast_path_not_triggered_for_user_tickets(
    ctx_factory, monkeypatch, tmp_path
):
    """Human-written tickets never take the mechanical fast-path, even
    when the draft is mechanical and auto-approve is enabled."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="user")
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

    # Should NOT hit the fast-path; should run the full refine agent.
    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


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


def test_mechanical_draft_fast_path_falls_through_on_needs_approval(
    ctx_factory, monkeypatch, tmp_path
):
    """When auto-approve returns NEEDS_APPROVAL, fall through to the full
    refine agent."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="test_gap")
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

    assert not out.note.startswith("mechanical draft fast-path")
    assert len(calls) == 1  # full refine agent WAS invoked


def test_mechanical_draft_fast_path_falls_through_on_auto_approve_error(
    ctx_factory, monkeypatch, tmp_path
):
    """When the auto-approve call raises, fall through gracefully."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="test_gap")
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
    epic = ctx.service.create("Epic umbrella", "epic body", kind="epic")
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
    epics = [tk for tk in ctx.service.list() if tk.kind == "epic"]
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
    epics = [tk for tk in ctx.service.list() if tk.kind == "epic"]
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

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

"""Unit tests for the triage helpers in
``src/robotsix_mill/stages/refine/_triage.py``.

Covers the module-level functions: ``_count_code_block_lines``,
``_triage_outcome``, ``_parse_prior_boards``, ``_anti_bounce_escalate``,
``is_sendback_reentry``, ``split_child_fast_path``, and ``triage_skip``.
All LLM/agent collaborators are mocked (no network).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.agents.refining import (
    AutoApproveResult,
    TriageResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _triage
from robotsix_mill.stages.refine import _reconcile
from robotsix_mill.stages.refine import _result_paths
from robotsix_mill.stages.refine.helpers import OPERATOR_SENDBACK_PREFIX


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path):
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
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body=None, **kw):
    """Create a DRAFT ticket."""
    if body is None:
        body = "Add a feature. This is a substantive draft body."
    return ctx.service.create(title, body, **kw)


# ===========================================================================
# _count_code_block_lines
# ===========================================================================


def test_count_code_block_lines_empty():
    assert _triage._count_code_block_lines("") == 0


def test_count_code_block_lines_no_fences():
    assert _triage._count_code_block_lines("just some\ntext\nno fences") == 0


def test_count_code_block_lines_simple_single_block():
    text = "```\nline1\nline2\nline3\n```"
    assert _triage._count_code_block_lines(text) == 3


def test_count_code_block_lines_with_language_hint():
    text = "```python\nimport os\n\ndef foo():\n    pass\n```"
    assert _triage._count_code_block_lines(text) == 4


def test_count_code_block_lines_nested_fences_inner_counted():
    # Inner fences toggle depth: ``` opens, inner ``` closes (not nested)
    # Then next ``` opens again.
    text = "```\nouter line 1\n```\ninner still outer\n```\nouter line 2\n```"
    assert _triage._count_code_block_lines(text) == 2


def test_count_code_block_lines_consecutive_separate_blocks():
    text = "```\na\nb\n```\nnot code\n```\nc\nd\ne\n```"
    # First block: 2 lines. Second block: 3 lines. Total = 5.
    assert _triage._count_code_block_lines(text) == 5


def test_count_code_block_lines_unclosed_fence_counts_remaining():
    text = "```\nline1\nline2\nline3"
    # Fence opens, depth=1, all three subsequent lines are inside.
    assert _triage._count_code_block_lines(text) == 3


def test_count_code_block_lines_empty_fence_block():
    text = "```\n```"
    assert _triage._count_code_block_lines(text) == 0


# ===========================================================================
# _triage_outcome
# ===========================================================================


def test_triage_outcome_basic(ctx_factory, tmp_path):
    """Writes draft-original.md and empty file_map, returns resolved Outcome."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "This is a draft."

    with patch.object(
        _result_paths, "resolved_outcome", return_value=State.DONE
    ) as mock_resolved:
        with patch.object(_reconcile, "write_file_map") as mock_wfm:
            _triage._triage_outcome(ctx, ws, draft, t.id, "test reason")

    # draft-original.md written
    draft_path = ws.artifacts_dir / "draft-original.md"
    assert draft_path.exists()
    assert draft_path.read_text(encoding="utf-8") == draft

    # write_file_map called with empty list
    mock_wfm.assert_called_once_with(ws, [], only_if_absent=True)

    # resolved_outcome called with correct positional args
    mock_resolved.assert_called_once()
    args = mock_resolved.call_args.args
    assert args[0] is ctx
    assert args[1] == draft
    assert args[2] == t.id
    assert args[3] == "test reason"

    # kwargs
    kwargs = mock_resolved.call_args.kwargs
    assert kwargs.get("source") is None
    assert kwargs.get("triage_note") is None


def test_triage_outcome_empty_draft_writes_placeholder(ctx_factory, tmp_path):
    """Empty draft writes the placeholder string."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    with patch.object(_result_paths, "resolved_outcome", return_value=State.DONE):
        with patch.object(_reconcile, "write_file_map"):
            _triage._triage_outcome(ctx, ws, "", t.id, "reason")

    draft_path = ws.artifacts_dir / "draft-original.md"
    assert "(title-only ticket, no body provided)" in draft_path.read_text(
        encoding="utf-8"
    )


def test_triage_outcome_extract_paths_from_draft_with_paths(ctx_factory, tmp_path):
    """extract_paths_from_draft=True with matching paths writes them to file_map."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "Edit `src/foo.py` and `tests/test_bar.py` for this change."

    with patch.object(_result_paths, "resolved_outcome", return_value=State.DONE):
        with patch.object(_reconcile, "write_file_map") as mock_wfm:
            _triage._triage_outcome(
                ctx, ws, draft, t.id, "reason", extract_paths_from_draft=True
            )

    expected = [
        {"file": "src/foo.py", "note": "from draft"},
        {"file": "tests/test_bar.py", "note": "from draft"},
    ]
    mock_wfm.assert_called_once_with(ws, expected, only_if_absent=True)


def test_triage_outcome_extract_paths_from_draft_no_paths(ctx_factory, tmp_path):
    """extract_paths_from_draft=True with no matching paths writes empty file_map."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "No file paths in this draft."

    with patch.object(_result_paths, "resolved_outcome", return_value=State.DONE):
        with patch.object(_reconcile, "write_file_map") as mock_wfm:
            _triage._triage_outcome(
                ctx, ws, draft, t.id, "reason", extract_paths_from_draft=True
            )

    mock_wfm.assert_called_once_with(ws, [], only_if_absent=True)


def test_triage_outcome_write_file_map_args(ctx_factory, tmp_path):
    """write_file_map_args passed explicitly are forwarded."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "draft"
    args = [{"file": "a.py", "note": "explicit"}]

    with patch.object(_result_paths, "resolved_outcome", return_value=State.DONE):
        with patch.object(_reconcile, "write_file_map") as mock_wfm:
            _triage._triage_outcome(
                ctx, ws, draft, t.id, "reason", write_file_map_args=args
            )

    mock_wfm.assert_called_once_with(ws, args, only_if_absent=True)


def test_triage_outcome_passes_source_and_triage_note(ctx_factory, tmp_path):
    """source and triage_note kwargs are forwarded to resolved_outcome."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    with patch.object(
        _result_paths, "resolved_outcome", return_value=State.DONE
    ) as mock_resolved:
        with patch.object(_reconcile, "write_file_map"):
            _triage._triage_outcome(
                ctx,
                ws,
                "draft",
                t.id,
                "reason",
                source="audit",
                triage_note="auto-approved",
            )

    kwargs = mock_resolved.call_args.kwargs
    assert kwargs["source"] == "audit"
    assert kwargs["triage_note"] == "auto-approved"


# ===========================================================================
# _parse_prior_boards
# ===========================================================================


class _FakeEvent:
    """Minimal fake history event with ``note`` and ``state`` attributes."""

    def __init__(self, note="", state=None):
        self.note = note
        self.state = state


def test_parse_prior_boards_empty_history():
    svc = MagicMock()
    svc.history.return_value = []
    boards, count = _triage._parse_prior_boards(svc, "ticket-1")
    assert boards == set()
    assert count == 0


def test_parse_prior_boards_single_migration():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note="migrated from board other-board to target-board (was open)"),
    ]
    boards, count = _triage._parse_prior_boards(svc, "ticket-1")
    assert boards == {"target-board"}
    assert count == 1


def test_parse_prior_boards_multiple_migrations():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note="migrated from board A to B (was draft)"),
        _FakeEvent(note="migrated from board B to C"),
        _FakeEvent(note="regular comment"),
    ]
    boards, count = _triage._parse_prior_boards(svc, "ticket-1")
    assert boards == {"B", "C"}
    assert count == 2


def test_parse_prior_boards_non_migration_notes_ignored():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note="created ticket"),
        _FakeEvent(note="changes requested: please fix the scope"),
        _FakeEvent(note="refined spec"),
    ]
    boards, count = _triage._parse_prior_boards(svc, "ticket-1")
    assert boards == set()
    assert count == 0


# ===========================================================================
# _anti_bounce_escalate
# ===========================================================================


def test_anti_bounce_no_prior_migrations_returns_none(ctx_factory, tmp_path):
    """When no prior migrations, the guard returns None (safe to proceed)."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    triage = MagicMock()
    triage.reason = "migrate to repo-x"

    with patch.object(
        _triage, "_parse_prior_boards", return_value=(set(), 0)
    ) as mock_parse:
        result = _triage._anti_bounce_escalate(
            ctx, ws, "draft", t, triage, "target-board"
        )

    assert result is None
    mock_parse.assert_called_once_with(ctx.service, t.id)


def test_anti_bounce_has_prior_migration_returns_outcome(ctx_factory, tmp_path):
    """When there is a prior migration, the guard escalates to human."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    triage = MagicMock()
    triage.reason = "migrate to repo-x"

    with patch.object(
        _triage, "_parse_prior_boards", return_value=({"target-board"}, 1)
    ):
        with patch.object(
            _result_paths,
            "resolved_outcome",
            return_value=State.HUMAN_ISSUE_APPROVAL,
        ) as mock_resolved:
            with patch.object(_reconcile, "write_file_map"):
                result = _triage._anti_bounce_escalate(
                    ctx, ws, "draft", t, triage, "target-board"
                )

    assert result is not None
    # resolved_outcome(ctx, spec, ticket_id, base_note, ...)
    base_note = mock_resolved.call_args.args[3]
    assert "anti-bounce blocked" in base_note


def test_anti_bounce_target_in_prior_boards_returns_outcome(ctx_factory, tmp_path):
    """When resolved board is in prior boards, escalates."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    triage = MagicMock()
    triage.reason = "migrate back"

    with patch.object(
        _triage, "_parse_prior_boards", return_value=({"target-board"}, 0)
    ):
        with patch.object(
            _result_paths,
            "resolved_outcome",
            return_value=State.HUMAN_ISSUE_APPROVAL,
        ) as mock_resolved:
            with patch.object(_reconcile, "write_file_map"):
                result = _triage._anti_bounce_escalate(
                    ctx, ws, "draft", t, triage, "target-board"
                )

    assert result is not None
    assert "anti-bounce blocked" in mock_resolved.call_args.args[3]


def test_anti_bounce_history_read_error_escalates(ctx_factory, tmp_path):
    """When history cannot be read, escalate to human."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    triage = MagicMock()
    triage.reason = "migrate"

    with patch.object(
        _triage,
        "_parse_prior_boards",
        side_effect=RuntimeError("DB connection lost"),
    ):
        with patch.object(
            _result_paths,
            "resolved_outcome",
            return_value=State.HUMAN_ISSUE_APPROVAL,
        ) as mock_resolved:
            with patch.object(_reconcile, "write_file_map"):
                result = _triage._anti_bounce_escalate(
                    ctx, ws, "draft", t, triage, "any-board"
                )

    assert result is not None
    assert "anti-bounce error" in mock_resolved.call_args.args[3]


# ===========================================================================
# is_sendback_reentry
# ===========================================================================


def test_is_sendback_reentry_true():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note="created ticket", state=State.DRAFT),
        _FakeEvent(
            note=f"{OPERATOR_SENDBACK_PREFIX} please fix the scope",
            state=State.DRAFT,
        ),
    ]
    assert _triage.is_sendback_reentry(svc, "ticket-1") is True


def test_is_sendback_reentry_false_no_sendback():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note="created ticket", state=State.DRAFT),
        _FakeEvent(note="refined spec", state=State.HUMAN_ISSUE_APPROVAL),
    ]
    assert _triage.is_sendback_reentry(svc, "ticket-1") is False


def test_is_sendback_reentry_false_wrong_state():
    """A sendback note on a non-DRAFT event is not detected."""
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(
            note=f"{OPERATOR_SENDBACK_PREFIX} fix it",
            state=State.HUMAN_ISSUE_APPROVAL,
        ),
    ]
    assert _triage.is_sendback_reentry(svc, "ticket-1") is False


def test_is_sendback_reentry_false_no_note():
    svc = MagicMock()
    svc.history.return_value = [
        _FakeEvent(note=None, state=State.DRAFT),
    ]
    assert _triage.is_sendback_reentry(svc, "ticket-1") is False


# ===========================================================================
# split_child_fast_path
# ===========================================================================


def test_split_child_parent_closed_with_split_note(ctx_factory, tmp_path):
    """Parent CLOSED with 'split into' note → short-circuits."""
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Parent")
    child = _ticket(ctx, title="Child", parent_id=parent.id)

    # Build a parent with CLOSED state
    parent = ctx.service.get(parent.id)
    ctx.service.transition(parent.id, State.CLOSED, note="split into child-1")
    parent = ctx.service.get(parent.id)

    ws = ctx.service.workspace(child)

    with patch.object(_reconcile, "write_triage_complexity") as mock_complexity:
        with patch.object(
            _result_paths, "resolved_outcome", return_value=State.READY
        ) as mock_resolved:
            with patch.object(_reconcile, "write_file_map"):
                result = _triage.split_child_fast_path(
                    ctx, child, "refined spec", ws, None
                )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "split child" in base_note
    mock_complexity.assert_called_once_with(ws, "simple")


def test_split_child_own_history_split_from(ctx_factory, tmp_path):
    """Ticket's own history has 'split from' note → short-circuits."""
    ctx = ctx_factory()
    child = _ticket(ctx, title="Reparented child", parent_id=None)

    # Add "split from" note to own history
    ctx.service.transition(
        child.id,
        State.HUMAN_ISSUE_APPROVAL,
        note="split from 20250101T000000Z-parent-aa",
    )
    child = ctx.service.get(child.id)

    ws = ctx.service.workspace(child)

    with patch.object(_reconcile, "write_triage_complexity"):
        with patch.object(
            _result_paths, "resolved_outcome", return_value=State.READY
        ) as mock_resolved:
            with patch.object(_reconcile, "write_file_map"):
                result = _triage.split_child_fast_path(
                    ctx, child, "refined spec", ws, None
                )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "split child" in base_note


def test_split_child_reviewer_comments_blocks(ctx_factory, tmp_path):
    """Even a valid split child must fall through when reviewer comments exist."""
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Parent")
    child = _ticket(ctx, title="Child", parent_id=parent.id)

    ctx.service.transition(parent.id, State.CLOSED, note="split into child-1")

    ws = ctx.service.workspace(child)
    result = _triage.split_child_fast_path(
        ctx, child, "refined spec", ws, "Reviewer wants changes"
    )

    assert result is None  # falls through


def test_split_child_empty_spec_blocks(ctx_factory, tmp_path):
    """Split child with empty spec returns BLOCKED."""
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Parent")
    child = _ticket(ctx, title="Child", parent_id=parent.id)

    ctx.service.transition(parent.id, State.CLOSED, note="split into child-1")

    ws = ctx.service.workspace(child)

    with patch.object(_reconcile, "write_triage_complexity"):
        result = _triage.split_child_fast_path(ctx, child, "   ", ws, None)

    assert result is not None
    assert result.next_state == State.BLOCKED
    assert result.note == "split child has empty description"


def test_split_child_parent_closed_non_split_no_short_circuit(ctx_factory, tmp_path):
    """Parent CLOSED for non-split reason → no short-circuit."""
    ctx = ctx_factory()
    parent = _ticket(ctx, title="Parent")
    child = _ticket(ctx, title="Child", parent_id=parent.id)

    ctx.service.transition(parent.id, State.CLOSED, note="retrospected: complete")

    ws = ctx.service.workspace(child)
    result = _triage.split_child_fast_path(ctx, child, "draft", ws, None)

    assert result is None


def test_split_child_no_parent(ctx_factory, tmp_path):
    """A ticket without a parent_id and no 'split from' history → no short-circuit."""
    ctx = ctx_factory()
    child = _ticket(ctx, title="Standalone")

    ws = ctx.service.workspace(child)
    result = _triage.split_child_fast_path(ctx, child, "draft", ws, None)

    assert result is None


# ===========================================================================
# triage_skip
# ===========================================================================


def _mock_triage_result(decision="REFINE", reason="needs refinement"):
    return TriageResult(decision=decision, reason=reason)


def test_triage_skip_feature_disabled(ctx_factory, tmp_path):
    """When refine_triage_enabled is False, return None immediately."""
    ctx = ctx_factory(refine_triage_enabled=False)
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    result = _triage.triage_skip(
        ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
    )

    assert result is None


def test_triage_skip_reviewer_comments_blocks(ctx_factory, tmp_path):
    """When reviewer comments are present, return None."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    result = _triage.triage_skip(
        ctx, t, "draft", None, None, t.title, ws, ctx.settings, "please fix scope"
    )

    assert result is None


def test_triage_skip_prior_skip_in_history_short_circuits(ctx_factory, tmp_path):
    """When a prior refine pass already issued a triage SKIP (recorded
    in the state-transition history), skip the LLM call and return the
    SKIP outcome immediately."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    # Simulate a prior triage SKIP: ticket went DRAFT → READY with the
    # SKIP note, then later (e.g. after implement sendback) returned to
    # DRAFT for re-refinement.
    ctx.service.transition(t.id, State.READY, note="triage SKIP: already precise")

    triage_refine_called = []

    def _fake_triage_refine(**kw):
        triage_refine_called.append(1)
        return _mock_triage_result("SKIP", "should not be called")

    with patch.object(_triage.refining, "triage_refine", _fake_triage_refine):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(_reconcile, "write_file_map"):
                result = _triage.triage_skip(
                    ctx,
                    t,
                    "draft body",
                    None,
                    None,
                    t.title,
                    ws,
                    ctx.settings,
                    None,
                )

    assert result is not None
    assert not triage_refine_called, "triage_refine should NOT be called"
    assert "triage SKIP" in result.note
    assert "prior verdict" in result.note


def test_triage_skip_no_prior_skip_in_history_still_calls_triage(
    ctx_factory,
    tmp_path,
):
    """When there is no prior triage SKIP in the history, the triage
    LLM call proceeds normally."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    # No "triage SKIP" in history — just a normal creation event.
    triage_refine_called = []

    def _fake_triage_refine(**kw):
        triage_refine_called.append(1)
        return _mock_triage_result("REFINE", "needs refinement")

    with patch.object(_triage.refining, "triage_refine", _fake_triage_refine):
        with patch.object(_reconcile, "persist_triage_complexity"):
            result = _triage.triage_skip(
                ctx, t, "draft body", None, None, t.title, ws, ctx.settings, None
            )

    assert result is None  # REFINE falls through
    assert triage_refine_called, "triage_refine SHOULD have been called"


def test_triage_skip_prescriptive_spec_threshold_met(ctx_factory, tmp_path):
    """When draft code block lines meet the threshold, skip refine."""
    ctx = ctx_factory(refine_prescriptive_spec_code_lines_threshold=3)
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "```\n" + "\n".join(f"line {i}" for i in range(5)) + "\n```"

    with patch.object(
        _result_paths, "resolved_outcome", return_value=State.READY
    ) as mock_resolved:
        with patch.object(_reconcile, "write_file_map"):
            result = _triage.triage_skip(
                ctx, t, draft, None, None, t.title, ws, ctx.settings, None
            )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "prescriptive spec" in base_note


def test_triage_skip_prescriptive_spec_threshold_zero_disabled(ctx_factory, tmp_path):
    """When threshold is 0, the prescriptive shortcut is disabled."""
    ctx = ctx_factory(refine_prescriptive_spec_code_lines_threshold=0)
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    draft = "```\n" + "\n".join(f"line {i}" for i in range(10)) + "\n```"

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("SKIP", "ok"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _result_paths, "resolved_outcome", return_value=State.READY
            ):
                with patch.object(_reconcile, "write_file_map"):
                    result = _triage.triage_skip(
                        ctx, t, draft, None, None, t.title, ws, ctx.settings, None
                    )

    # Should have gone through triage_refine (SKIP) — not the prescriptive path
    assert result is not None


def test_triage_skip_triage_refine_exception_returns_none(ctx_factory, tmp_path):
    """When triage_refine raises, fall through to full refine."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        side_effect=RuntimeError("LLM unavailable"),
    ):
        result = _triage.triage_skip(
            ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
        )

    assert result is None


def test_triage_skip_maintenance_decision(ctx_factory, tmp_path):
    """MAINTENANCE decision routes to MAINTENANCE state."""
    ctx = ctx_factory(maintenance_triage_enabled=True)
    t = _ticket(ctx, source=SourceKind.USER)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("MAINTENANCE", "restart the worker"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            result = _triage.triage_skip(
                ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
            )

    assert result is not None
    assert result.next_state == State.MAINTENANCE
    assert "maintenance triage (LLM)" in result.note
    assert (ws.artifacts_dir / "draft-original.md").exists()


def test_triage_skip_maintenance_ci_source_falls_through(ctx_factory, tmp_path):
    """CI source skips MAINTENANCE routing, falls through to full refine."""
    ctx = ctx_factory(maintenance_triage_enabled=True)
    t = _ticket(ctx, source=SourceKind.CI)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("MAINTENANCE", "restart"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            result = _triage.triage_skip(
                ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
            )

    # CI-source maintenance falls through past MAINTENANCE check;
    # with auto_approve_enabled=False, no mechanical fast-path triggers,
    # so it falls through to `return None`.
    assert result is None


def test_triage_skip_no_change_decision(ctx_factory, tmp_path):
    """NO_CHANGE decision routes to implement (TASK without branch)."""
    ctx = ctx_factory()
    t = _ticket(ctx, kind=TicketKind.TASK)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("NO_CHANGE", "already done"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _result_paths, "resolved_outcome", return_value=State.READY
            ) as mock_resolved:
                with patch.object(_reconcile, "write_file_map"):
                    result = _triage.triage_skip(
                        ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
                    )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "NO_CHANGE" in base_note
    assert "routing to implement" in base_note


def test_triage_skip_no_change_task_with_branch(ctx_factory, tmp_path):
    """NO_CHANGE for TASK with branch → DONE."""
    ctx = ctx_factory()
    t = _ticket(ctx, kind=TicketKind.TASK)
    ctx.service.set_branch(t.id, "feat/something")
    t = ctx.service.get(t.id)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("NO_CHANGE", "already merged"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(_reconcile, "write_file_map"):
                result = _triage.triage_skip(
                    ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
                )

    assert result is not None
    assert result.next_state == State.DONE
    assert "NO_CHANGE" in result.note


def test_triage_skip_skip_decision(ctx_factory, tmp_path):
    """SKIP decision routes to implement."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("SKIP", "already a precise spec"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _result_paths, "resolved_outcome", return_value=State.READY
            ) as mock_resolved:
                with patch.object(_reconcile, "write_file_map"):
                    result = _triage.triage_skip(
                        ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
                    )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "SKIP" in base_note


def test_triage_skip_mechanical_fast_path_deterministic_source(ctx_factory, tmp_path):
    """Deterministic source (e.g. audit) skips auto-approve LLM."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="audit")
    ws = ctx.service.workspace(t)
    draft = "Edit `src/foo.py` for this audit finding."

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=TriageResult(
            decision="REFINE", reason="needs refinement", complexity="simple"
        ),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _result_paths, "resolved_outcome", return_value=State.READY
            ) as mock_resolved:
                with patch.object(_reconcile, "write_file_map"):
                    result = _triage.triage_skip(
                        ctx, t, draft, None, None, t.title, ws, ctx.settings, None
                    )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "deterministic source" in base_note
    assert "skipped refine LLM" in base_note


def test_triage_skip_mechanical_fast_path_auto_approve_approve(ctx_factory, tmp_path):
    """Non-deterministic source with auto-approve APPROVE skips refine."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="retrospect")
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=TriageResult(
            decision="REFINE", reason="needs refinement", complexity="simple"
        ),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _triage.refining,
                "triage_auto_approve",
                return_value=AutoApproveResult(decision="APPROVE", reason="safe"),
            ):
                with patch.object(
                    _result_paths, "resolved_outcome", return_value=State.READY
                ) as mock_resolved:
                    with patch.object(_reconcile, "write_file_map"):
                        result = _triage.triage_skip(
                            ctx,
                            t,
                            "## Problem\n\nSome issue\n\n## Scope\n\nFix it",
                            None,
                            None,
                            t.title,
                            ws,
                            ctx.settings,
                            None,
                        )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "auto-approve APPROVE" in base_note


def test_triage_skip_mechanical_fast_path_auto_approve_needs_approval(
    ctx_factory, tmp_path
):
    """Non-deterministic source with auto-approve NEEDS_APPROVAL skips refine."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="retrospect")
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=TriageResult(
            decision="REFINE", reason="needs refinement", complexity="simple"
        ),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _triage.refining,
                "triage_auto_approve",
                return_value=AutoApproveResult(
                    decision="NEEDS_APPROVAL", reason="security change"
                ),
            ):
                with patch.object(
                    _result_paths,
                    "resolved_outcome",
                    return_value=State.HUMAN_ISSUE_APPROVAL,
                ) as mock_resolved:
                    with patch.object(_reconcile, "write_file_map"):
                        result = _triage.triage_skip(
                            ctx,
                            t,
                            "## Problem\n\nSecurity issue\n\n## Scope\n\nFix auth",
                            None,
                            None,
                            t.title,
                            ws,
                            ctx.settings,
                            None,
                        )

    assert result is not None
    base_note = mock_resolved.call_args.args[3]
    assert "NEEDS_APPROVAL" in base_note


def test_triage_skip_mechanical_fast_path_auto_approve_exception_falls_through(
    ctx_factory, tmp_path
):
    """When auto-approve raises, fall through to full refine."""
    ctx = ctx_factory(auto_approve_enabled=True)
    t = _ticket(ctx, source="retrospect")
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=TriageResult(
            decision="REFINE", reason="needs refinement", complexity="simple"
        ),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(
                _triage.refining,
                "triage_auto_approve",
                side_effect=RuntimeError("auto-approve failed"),
            ):
                result = _triage.triage_skip(
                    ctx,
                    t,
                    "draft",
                    None,
                    None,
                    t.title,
                    ws,
                    ctx.settings,
                    None,
                )

    assert result is None


def test_triage_skip_refine_decision_returns_none(ctx_factory, tmp_path):
    """REFINE decision (default) returns None to fall through to full agent."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    with patch.object(
        _triage.refining,
        "triage_refine",
        return_value=_mock_triage_result("REFINE", "needs refinement"),
    ):
        with patch.object(_reconcile, "persist_triage_complexity"):
            result = _triage.triage_skip(
                ctx, t, "draft", None, None, t.title, ws, ctx.settings, None
            )

    # With auto_approve_enabled=False and REFINE, falls through
    assert result is None

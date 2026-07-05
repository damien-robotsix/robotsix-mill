"""Unit tests for ``_result_paths`` — the result-mode handlers for the refine stage.

Covers every public function in ``src/robotsix_mill/stages/refine/_result_paths.py``:
* ``resolved_outcome``
* ``ack_threads``
* ``review_spec_conciseness``
* ``no_change_path``
* ``promote_to_epic_path``
* ``single_scope_path``
* ``multi_scope_path``

All external collaborators are mocked — no LLM or network.
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents.refining import (
    ChildSpec,
    RefineResult,
    SpecReviewResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import (
    _result_paths,
    orchestration as orch_module,
)
from robotsix_mill.stages.refine.helpers import (
    UNMERGED_BRANCH_PREFIX,
)

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
    """Create a DRAFT ticket with a substantive body."""
    if body is None:
        body = (
            "Add a feature. This is a substantive draft body padded "
            "past the 100-char trivial-draft threshold so refine's "
            "pipeline actually runs against this ticket."
        )
    return ctx.service.create(title, body, **kw)


# ===========================================================================
# resolved_outcome
# ===========================================================================


class TestResolvedOutcome:
    def test_base_note_only(self, ctx_factory, monkeypatch):
        """When _resolve_next_state returns no auto_note, only base_note is used."""
        ctx = ctx_factory()

        def _fake_resolve(ctx, spec, ticket_id, *, source=None, triage_note=None):
            return (State.READY, None)

        monkeypatch.setattr(_result_paths, "_resolve_next_state", _fake_resolve)

        outcome = _result_paths.resolved_outcome(
            ctx, _REAL_SPEC, "ticket-1", "refined", source="user"
        )

        assert outcome.next_state == State.READY
        assert outcome.note == "refined"

    def test_base_note_with_auto_note(self, ctx_factory, monkeypatch):
        """auto_note is appended with ' | ' separator."""
        ctx = ctx_factory()

        def _fake_resolve(ctx, spec, ticket_id, *, source=None, triage_note=None):
            return (State.HUMAN_ISSUE_APPROVAL, "gated: design decision")

        monkeypatch.setattr(_result_paths, "_resolve_next_state", _fake_resolve)

        outcome = _result_paths.resolved_outcome(
            ctx, _REAL_SPEC, "ticket-1", "refined", source="user"
        )

        assert outcome.next_state == State.HUMAN_ISSUE_APPROVAL
        assert outcome.note == "refined | gated: design decision"

    def test_passes_source_and_triage_note(self, ctx_factory, monkeypatch):
        """source and triage_note are forwarded to _resolve_next_state."""
        ctx = ctx_factory()
        captured: dict = {}

        def _fake_resolve(ctx, spec, ticket_id, *, source=None, triage_note=None):
            captured["source"] = source
            captured["triage_note"] = triage_note
            return (State.READY, None)

        monkeypatch.setattr(_result_paths, "_resolve_next_state", _fake_resolve)

        _result_paths.resolved_outcome(
            ctx,
            _REAL_SPEC,
            "ticket-1",
            "refined",
            source="ci",
            triage_note="auto-approved",
        )

        assert captured["source"] == "ci"
        assert captured["triage_note"] == "auto-approved"


# ===========================================================================
# ack_threads
# ===========================================================================


class TestAckThreads:
    def test_calls_acknowledge_when_both_truthy(self, ctx_factory, monkeypatch):
        """When reviewer_comments and open_thread_ids are truthy, delegate."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        called: list[tuple] = []

        def _fake_ack(ctx, ticket, open_thread_ids):
            called.append((ticket.id, open_thread_ids))

        monkeypatch.setattr(orch_module, "acknowledge_unanswered_threads", _fake_ack)

        _result_paths.ack_threads(ctx, t, "some reviewer comments", {1, 2, 3})

        assert len(called) == 1
        assert called[0] == (t.id, {1, 2, 3})

    def test_noop_when_reviewer_comments_none(self, ctx_factory, monkeypatch):
        """None reviewer_comments → no-op."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        called: list[tuple] = []

        def _fake_ack(ctx, ticket, open_thread_ids):
            called.append(("called",))

        monkeypatch.setattr(orch_module, "acknowledge_unanswered_threads", _fake_ack)

        _result_paths.ack_threads(ctx, t, None, {1, 2, 3})
        assert called == []

    def test_noop_when_reviewer_comments_empty(self, ctx_factory, monkeypatch):
        """Empty string reviewer_comments → no-op."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        called: list[tuple] = []

        def _fake_ack(ctx, ticket, open_thread_ids):
            called.append(("called",))

        monkeypatch.setattr(orch_module, "acknowledge_unanswered_threads", _fake_ack)

        _result_paths.ack_threads(ctx, t, "", {1, 2, 3})
        assert called == []

    def test_noop_when_open_thread_ids_empty(self, ctx_factory, monkeypatch):
        """Empty set open_thread_ids → no-op."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        called: list[tuple] = []

        def _fake_ack(ctx, ticket, open_thread_ids):
            called.append(("called",))

        monkeypatch.setattr(orch_module, "acknowledge_unanswered_threads", _fake_ack)

        _result_paths.ack_threads(ctx, t, "comments", set())
        assert called == []

    def test_noop_when_both_falsy(self, ctx_factory, monkeypatch):
        """Both falsy → no-op."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        called: list[tuple] = []

        def _fake_ack(ctx, ticket, open_thread_ids):
            called.append(("called",))

        monkeypatch.setattr(orch_module, "acknowledge_unanswered_threads", _fake_ack)

        _result_paths.ack_threads(ctx, t, None, set())
        assert called == []


# ===========================================================================
# review_spec_conciseness
# ===========================================================================


class TestReviewSpecConciseness:
    def test_success_returns_concise_and_writes_verbose(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _mock_review(*, settings, spec_markdown, **kw):
            return SpecReviewResult(
                concise_spec=_CONCISE_SPEC, stripped_summary="stripped 3 lines"
            )

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _mock_review
        )

        result = _result_paths.review_spec_conciseness(
            ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
        )

        assert result == _CONCISE_SPEC
        verbose = ws.artifacts_dir / "refine-verbose.md"
        assert verbose.exists()
        assert verbose.read_text(encoding="utf-8") == _REAL_SPEC

    def test_degenerate_concise_returns_original(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _mock_review(*, settings, spec_markdown, **kw):
            return SpecReviewResult(concise_spec="tbd", stripped_summary="degenerate")

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _mock_review
        )

        result = _result_paths.review_spec_conciseness(
            ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
        )

        assert result == _REAL_SPEC

    def test_exception_returns_original(self, ctx_factory, monkeypatch):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _boom(*, settings, spec_markdown, **kw):
            raise RuntimeError("backend down")

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _boom
        )

        result = _result_paths.review_spec_conciseness(
            ctx.settings, ws, t, _REAL_SPEC, "refine-verbose.md"
        )

        assert result == _REAL_SPEC

    def test_child_index_variant_success(self, ctx_factory, monkeypatch, caplog):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _mock_review(*, settings, spec_markdown, **kw):
            return SpecReviewResult(
                concise_spec=_CONCISE_SPEC, stripped_summary="stripped 3 lines"
            )

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _mock_review
        )

        with caplog.at_level("INFO", logger="robotsix_mill.stages.refine"):
            result = _result_paths.review_spec_conciseness(
                ctx.settings,
                ws,
                t,
                _REAL_SPEC,
                "refine-verbose-child-2.md",
                child_index=2,
            )

        assert result == _CONCISE_SPEC
        assert any("spec review child 2" in r.message for r in caplog.records)

    def test_child_index_variant_degenerate(self, ctx_factory, monkeypatch, caplog):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _mock_review(*, settings, spec_markdown, **kw):
            return SpecReviewResult(concise_spec="tbd", stripped_summary="degenerate")

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _mock_review
        )

        with caplog.at_level("WARNING", logger="robotsix_mill.stages.refine"):
            result = _result_paths.review_spec_conciseness(
                ctx.settings,
                ws,
                t,
                _REAL_SPEC,
                "refine-verbose-child-3.md",
                child_index=3,
            )

        assert result == _REAL_SPEC
        assert any(
            "spec review child 3 returned empty/placeholder" in r.message
            for r in caplog.records
        )

    def test_child_index_variant_exception(self, ctx_factory, monkeypatch, caplog):
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        def _boom(*, settings, spec_markdown, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            _result_paths.refining, "review_spec_for_conciseness", _boom
        )

        with caplog.at_level("WARNING", logger="robotsix_mill.stages.refine"):
            result = _result_paths.review_spec_conciseness(
                ctx.settings,
                ws,
                t,
                _REAL_SPEC,
                "refine-verbose-child-1.md",
                child_index=1,
            )

        assert result == _REAL_SPEC
        assert any(
            "spec review failed for child 1" in r.message for r in caplog.records
        )


# ===========================================================================
# no_change_path
# ===========================================================================


class TestNoChangePath:
    def test_flags_not_set_returns_none(self, ctx_factory, monkeypatch):
        """When no_change_needed is False, returns None."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=False,
            spec_markdown=_REAL_SPEC,
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is None

    def test_split_set_returns_none(self, ctx_factory, monkeypatch):
        """When split is True, returns None even if no_change_needed is True."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale="already fixed",
            split=True,
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is None

    def test_promote_to_epic_set_returns_none(self, ctx_factory, monkeypatch):
        """When promote_to_epic is True, returns None."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale="already fixed",
            promote_to_epic=True,
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is None

    def test_empty_rationale_degrades(self, ctx_factory, monkeypatch):
        """Empty no_change_rationale → warning logged, returns None."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale="   ",
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is None

    def test_unmerged_branch_blocks(self, ctx_factory, monkeypatch):
        """Ticket with unmerged branch → BLOCKED outcome."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        # Set a branch on the ticket
        ctx.service.set_branch(t.id, "feat/orphan")
        t = ctx.service.get(t.id)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale="The implementation is complete; nothing further.",
        )

        # Mock _verify_branch_merged (via the facade) to return False
        import robotsix_mill.stages.refine as refine_facade

        monkeypatch.setattr(
            refine_facade, "_verify_branch_merged", lambda repo_dir, ticket: False
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is not None
        assert outcome.next_state == State.BLOCKED
        assert outcome.note.startswith(UNMERGED_BRANCH_PREFIX)

    def test_external_fix_routes_to_implement(self, ctx_factory, monkeypatch):
        """Rationale claiming external fix routes to implement for verification."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale=(
                "The bug was already fixed in commit abc1234def. "
                "No further change is needed here."
            ),
        )

        # Mock _rationale_claims_external_fix → True
        monkeypatch.setattr(
            _result_paths, "_rationale_claims_external_fix", lambda r: True
        )
        # Mock _verify_cited_fix_at_head → True
        monkeypatch.setattr(
            _result_paths, "_verify_cited_fix_at_head", lambda rd, r: True
        )
        # Mock _resolve_next_state
        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft body",
            None,
            "Add feature",
            ws,
            result,
        )

        assert outcome is not None
        assert outcome.next_state == State.READY
        assert "routed to implement" in outcome.note

    def test_task_without_branch_routes_to_implement(self, ctx_factory, monkeypatch):
        """TASK ticket without branch → routes to implement for verification."""
        ctx = ctx_factory()
        t = _ticket(ctx, kind=TicketKind.TASK)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale=(
                "The condition is handled by existing logic. "
                "Investigation complete — no code change required."
            ),
        )

        # Mock _rationale_claims_external_fix → False (so we hit the TASK branch)
        monkeypatch.setattr(
            _result_paths, "_rationale_claims_external_fix", lambda r: False
        )
        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft body",
            None,
            "Add feature",
            ws,
            result,
        )

        assert outcome is not None
        assert outcome.next_state == State.READY
        assert "routing to implement" in outcome.note

    def test_normal_done(self, ctx_factory, monkeypatch):
        """Non-TASK ticket (e.g. FEATURE) with rationale → DONE."""
        ctx = ctx_factory()
        t = _ticket(ctx, kind=TicketKind.INQUIRY)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            no_change_needed=True,
            no_change_rationale="The body already documents the full investigation.",
        )

        # Mock _rationale_claims_external_fix → False
        monkeypatch.setattr(
            _result_paths, "_rationale_claims_external_fix", lambda r: False
        )

        outcome = _result_paths.no_change_path(
            ctx,
            t,
            "draft",
            None,
            "title",
            ws,
            result,
        )

        assert outcome is not None
        assert outcome.next_state == State.DONE
        assert "no change needed" in outcome.note


# ===========================================================================
# promote_to_epic_path
# ===========================================================================


class TestPromoteToEpicPath:
    def test_normal_promotion(self, ctx_factory, monkeypatch):
        """Normal promotion writes epic body, promotes, runs breakdown."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            promote_to_epic=True,
            epic_body=_REAL_SPEC,
        )

        # Mock epic_breakdown
        import robotsix_mill.agents.epic_breakdown as eb

        class _Breakdown:
            child_titles: list[str] = ["Child A", "Child B"]
            child_bodies: list[str] = [_REAL_SPEC, _REAL_SPEC]
            epic_body = ""

        monkeypatch.setattr(eb, "run_epic_breakdown_agent", lambda **kw: _Breakdown())

        # Mock find_child_overlaps + annotate_child_body (imported
        # inside the function from core.dedup)
        import robotsix_mill.core.dedup as dedup_mod

        monkeypatch.setattr(
            dedup_mod,
            "find_child_overlaps",
            lambda service, parent_epic_id, child_titles, child_bodies, settings, now: [
                None,
                None,
            ],
        )
        monkeypatch.setattr(
            dedup_mod,
            "annotate_child_body",
            lambda body, note: body,
        )

        # Mock plan_child_dependencies (imported inside the function)
        monkeypatch.setattr(
            eb,
            "plan_child_dependencies",
            lambda created_children, child_board_id, create_child: {},
        )

        outcome = _result_paths.promote_to_epic_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            result,
        )

        assert outcome.next_state == State.EPIC_OPEN
        assert "spawned 2 child" in outcome.note
        # Ticket was promoted to epic
        updated = ctx.service.get(t.id)
        assert updated.kind == TicketKind.EPIC

    def test_missing_epic_body_falls_back_to_draft(self, ctx_factory, monkeypatch):
        """When epic_body is empty, falls back to draft or title."""
        ctx = ctx_factory()
        t = _ticket(ctx, title="My Epic Title")
        ws = ctx.service.workspace(t)

        result = RefineResult(
            promote_to_epic=True,
            epic_body="",
            spec_markdown="",
        )

        import robotsix_mill.agents.epic_breakdown as eb

        class _Breakdown:
            child_titles: list[str] = []
            child_bodies: list[str] = []
            epic_body = ""

        monkeypatch.setattr(eb, "run_epic_breakdown_agent", lambda **kw: _Breakdown())
        import robotsix_mill.core.dedup as dedup_mod

        monkeypatch.setattr(
            dedup_mod,
            "find_child_overlaps",
            lambda service, parent_epic_id, child_titles, child_bodies, settings, now: [],
        )
        monkeypatch.setattr(
            eb,
            "plan_child_dependencies",
            lambda created_children, child_board_id, create_child: {},
        )

        outcome = _result_paths.promote_to_epic_path(
            ctx,
            t,
            "original draft body",
            ws,
            ctx.settings,
            result,
        )

        assert outcome.next_state == State.EPIC_OPEN
        # The description should contain the fallback (draft)
        desc = ws.read_description()
        assert "original draft body" in desc

    def test_breakdown_exception_handled(self, ctx_factory, monkeypatch):
        """When epic breakdown raises, the ticket is still promoted."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            promote_to_epic=True,
            epic_body=_REAL_SPEC,
        )

        import robotsix_mill.agents.epic_breakdown as eb

        def _boom(**kw):
            raise RuntimeError("breakdown failed")

        monkeypatch.setattr(eb, "run_epic_breakdown_agent", _boom)

        outcome = _result_paths.promote_to_epic_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            result,
        )

        assert outcome.next_state == State.EPIC_OPEN
        assert "breakdown failed" in outcome.note
        assert ctx.service.get(t.id).kind == TicketKind.EPIC


# ===========================================================================
# single_scope_path
# ===========================================================================


class TestSingleScopePath:
    def test_normal_path(self, ctx_factory, monkeypatch):
        """Normal single-scope: writes spec, acks threads, returns resolved outcome."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(spec_markdown=_REAL_SPEC)

        # Mock _resolve_next_state
        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        # Mock ack_threads
        ack_called: list = []
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: ack_called.append((rc, ot)),
        )

        outcome = _result_paths.single_scope_path(
            ctx,
            t,
            ws,
            ctx.settings,
            result,
            reviewer_comments="some feedback",
            open_thread_ids={1, 2},
        )

        assert outcome.next_state == State.READY
        assert outcome.note == "refined"
        # Verify spec was written
        assert ws.read_description() == _REAL_SPEC
        # ack_threads was called
        assert len(ack_called) == 1
        assert ack_called[0] == ("some feedback", {1, 2})

    def test_degenerate_spec_keeps_draft(self, ctx_factory, monkeypatch):
        """When spec is degenerate, returns outcome with fallback note."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(spec_markdown="tbd")

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )

        outcome = _result_paths.single_scope_path(
            ctx,
            t,
            ws,
            ctx.settings,
            result,
            reviewer_comments=None,
            open_thread_ids=set(),
        )

        assert outcome.next_state == State.READY
        assert "no usable spec" in outcome.note

    def test_spec_review_enabled_no_reviewer_comments(self, ctx_factory, monkeypatch):
        """When spec_review_enabled and no reviewer comments, runs conciseness review."""
        ctx = ctx_factory(spec_review_enabled=True)
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(spec_markdown=_REAL_SPEC)

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        # Mock review_spec_conciseness to return concise
        review_called: list = []
        monkeypatch.setattr(
            _result_paths,
            "review_spec_conciseness",
            lambda s, ws, ticket, spec, vfn, child_index=None: (
                review_called.append(spec) or _CONCISE_SPEC
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        _result_paths.single_scope_path(
            ctx,
            t,
            ws,
            ctx.settings,
            result,
            reviewer_comments=None,
            open_thread_ids=set(),
        )

        assert len(review_called) == 1
        assert ws.read_description() == _CONCISE_SPEC

    def test_spec_review_enabled_with_reviewer_comments_skips_review(
        self, ctx_factory, monkeypatch
    ):
        """When reviewer_comments are present, conciseness review is skipped."""
        ctx = ctx_factory(spec_review_enabled=True)
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(spec_markdown=_REAL_SPEC)

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        review_called: list = []
        monkeypatch.setattr(
            _result_paths,
            "review_spec_conciseness",
            lambda s, ws, ticket, spec, vfn, child_index=None: (
                review_called.append("called") or _CONCISE_SPEC
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        _result_paths.single_scope_path(
            ctx,
            t,
            ws,
            ctx.settings,
            result,
            reviewer_comments="some feedback",
            open_thread_ids={1},
        )

        # Conciseness review should NOT be called
        assert len(review_called) == 0
        assert ws.read_description() == _REAL_SPEC


# ===========================================================================
# multi_scope_path
# ===========================================================================


class TestMultiScopePath:
    def test_empty_children_degrades_to_single(self, ctx_factory, monkeypatch):
        """No children → degrades to single-scope with spec."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[],
            spec_markdown=_REAL_SPEC,
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert "split degraded" in outcome.note
        assert ws.read_description() == _REAL_SPEC

    def test_empty_children_with_degenerate_spec(self, ctx_factory, monkeypatch):
        """No children + degenerate spec → kept original draft."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[],
            spec_markdown="tbd",
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert "empty spec" in outcome.note
        assert "split degraded" in outcome.note

    def test_single_valid_child_no_split(self, ctx_factory, monkeypatch):
        """One valid child → single child, no split."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="Only child", spec_markdown=_REAL_SPEC),
            ],
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert "single child, no split" in outcome.note
        assert ws.read_description() == _REAL_SPEC

    def test_multiple_children_split(self, ctx_factory, monkeypatch):
        """Multiple valid children → split creates child tickets, closes parent."""
        ctx = ctx_factory()
        t = _ticket(ctx, title="Parent Ticket")
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
            ],
            title="Parent Title",
            spec_markdown=_REAL_SPEC,
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert outcome.next_state == State.CLOSED
        assert outcome.note.startswith("split into ")
        child_ids = outcome.note.removeprefix("split into ").split(", ")
        assert len(child_ids) == 2
        for cid in child_ids:
            child = ctx.service.get(cid)
            assert child is not None
            assert child.parent_id is not None  # reparented into an epic
            assert child.state == State.READY

    def test_children_with_depends_on(self, ctx_factory, monkeypatch):
        """Children with depends_on indices → dependencies set."""
        ctx = ctx_factory()
        t = _ticket(ctx, title="Parent")
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="Child A", spec_markdown=_REAL_SPEC, depends_on=[]),
                ChildSpec(title="Child B", spec_markdown=_REAL_SPEC, depends_on=[0]),
            ],
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        child_ids = outcome.note.removeprefix("split into ").split(", ")
        # Child B depends on Child A
        b = ctx.service.get(child_ids[1])
        assert child_ids[0] in (b.depends_on or [])

    def test_existing_epic_parent_reused(self, ctx_factory, monkeypatch):
        """When ticket has an EPIC parent, children are reparented to it."""
        ctx = ctx_factory()
        epic = _ticket(ctx, title="Existing Epic", kind=TicketKind.EPIC)
        t = _ticket(ctx, title="Child Ticket", parent_id=epic.id)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="Grandchild A", spec_markdown=_REAL_SPEC),
                ChildSpec(title="Grandchild B", spec_markdown=_REAL_SPEC),
            ],
            title="Parent Title",
            spec_markdown=_REAL_SPEC,
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        child_ids = outcome.note.removeprefix("split into ").split(", ")
        for cid in child_ids:
            child = ctx.service.get(cid)
            assert child.parent_id == epic.id

    def test_all_children_invalid_blocks(self, ctx_factory, monkeypatch):
        """When all children have empty titles/bodies → BLOCKED."""
        ctx = ctx_factory()
        t = _ticket(ctx)
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="", spec_markdown=""),
                ChildSpec(title="  ", spec_markdown="  "),
            ],
        )

        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )

        outcome = _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert outcome.next_state == State.BLOCKED
        assert "no valid split children" in outcome.note

    def test_spec_review_enabled_for_children(self, ctx_factory, monkeypatch):
        """When spec_review_enabled and no reviewer comments, each child gets reviewed."""
        ctx = ctx_factory(spec_review_enabled=True)
        t = _ticket(ctx, title="Parent")
        ws = ctx.service.workspace(t)

        result = RefineResult(
            split=True,
            children=[
                ChildSpec(title="Child A", spec_markdown=_REAL_SPEC),
                ChildSpec(title="Child B", spec_markdown=_REAL_SPEC),
            ],
        )

        monkeypatch.setattr(
            _result_paths,
            "_resolve_next_state",
            lambda ctx, spec, tid, *, source=None, triage_note=None: (
                State.READY,
                None,
            ),
        )
        monkeypatch.setattr(
            _result_paths,
            "ack_threads",
            lambda ctx, ticket, rc, ot: None,
        )
        review_calls: list = []
        monkeypatch.setattr(
            _result_paths,
            "review_spec_conciseness",
            lambda s, ws, ticket, spec, vfn, child_index=None: (
                review_calls.append(child_index) or _CONCISE_SPEC
            ),
        )

        _result_paths.multi_scope_path(
            ctx,
            t,
            "draft",
            ws,
            ctx.settings,
            "",
            result,
            None,
            set(),
        )

        assert len(review_calls) == 2
        assert review_calls == [1, 2]

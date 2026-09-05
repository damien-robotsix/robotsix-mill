"""Regression tests for the triage-skip handling of throwaway
test-fixture tickets, mirroring the noop-8835 / dummy-2218 incident.

``noop-8835`` and ``dummy-2218`` were junk tickets that leaked onto
production boards from implement sessions — bare-token titles with
empty/placeholder bodies — and one flowed through refine into a real
(wasted) implement run before a monitor closed it.  ``report_issue``
now refuses to file such tickets (``is_placeholder_ticket``); these
tests lock in the second half of the guard: a placeholder fixture
ticket created through any other path is deterministically SKIPped to
DONE at triage, without consuming a refine/implement run.

The shapes mirror ``test_placeholder_real_shapes`` /
``test_placeholder_token_prefix_with_degenerate_body`` in
``tests/core/test_text_noop.py`` and
``test_placeholder_fixture_ticket_not_filed`` in
``tests/agents/test_report_issue.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robotsix_mill.agents.refine_triage import TriageResult
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _reconcile, _triage

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
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title, body=""):
    """Create a DRAFT ticket with the exact title/body under test."""
    return ctx.service.create(title, body, source="agent")


# Real data shapes of the leaked junk tickets (noop-8835, dummy-2218).
_JUNK_SHAPES = [
    ("noop", "disregard placeholder"),
    ("dummy", ""),
    ("no-op", ""),
    ("  Placeholder  ", ""),
    ("TMP", "x"),
]


# ===========================================================================
# junk / placeholder fixture tickets -> deterministic triage SKIP to DONE
# ===========================================================================


@pytest.mark.parametrize(("title", "body"), _JUNK_SHAPES)
def test_triage_skip_placeholder_fixture_closes_done(ctx_factory, title, body):
    """A noop-8835-style fixture ticket is SKIPped to DONE at triage.

    The triage classifier must never be called — the ticket is junk by
    construction, so an LLM classify + refine/implement pass would be a
    pure waste (the exact noop-8835 failure mode).
    """
    ctx = ctx_factory()
    t = _ticket(ctx, title, body)
    ws = ctx.service.workspace(t)

    triage_refine_called = []

    def _fake_triage_refine(**kw):  # pragma: no cover - must not be reached
        triage_refine_called.append(1)

    with patch.object(_triage.refining, "triage_refine", _fake_triage_refine):
        with patch.object(_reconcile, "write_file_map"):
            result = _triage.triage_skip(
                ctx, t, body, None, None, t.title, ws, ctx.settings, None
            )

    assert result is not None
    assert result.next_state == State.DONE
    assert "triage SKIP" in (result.note or "")
    assert "placeholder" in (result.note or "")
    assert not triage_refine_called, "triage classifier should NOT be called"


def test_triage_skip_placeholder_prefix_with_degenerate_body_closes_done(
    ctx_factory,
):
    """A throwaway-leading title plus an empty/placeholder body is caught
    (mirrors ``test_placeholder_token_prefix_with_degenerate_body``)."""
    ctx = ctx_factory()
    t = _ticket(ctx, "dummy ticket for the flow", "tbd")
    ws = ctx.service.workspace(t)

    with patch.object(_reconcile, "write_file_map"):
        result = _triage.triage_skip(
            ctx, t, "tbd", None, None, t.title, ws, ctx.settings, None
        )

    assert result is not None
    assert result.next_state == State.DONE
    assert "triage SKIP" in (result.note or "")


# ===========================================================================
# genuine tickets are NOT closed as junk
# ===========================================================================


def test_triage_skip_genuine_ticket_not_flagged(ctx_factory):
    """A real ticket (specific title + substantive body) is not closed as
    junk — the triage classifier runs normally."""
    ctx = ctx_factory()
    body = "The ingest dedup path is flaky; make it deterministic."
    t = _ticket(ctx, "Fix flaky test in refine", body)
    ws = ctx.service.workspace(t)

    triage_refine_called = []

    def _fake_triage_refine(**kw):
        triage_refine_called.append(1)
        return TriageResult(decision="REFINE", reason="needs refinement")

    with patch.object(_triage.refining, "triage_refine", _fake_triage_refine):
        with patch.object(_reconcile, "persist_triage_complexity"):
            with patch.object(_reconcile, "write_file_map"):
                result = _triage.triage_skip(
                    ctx, t, body, None, None, t.title, ws, ctx.settings, None
                )

    assert result is None  # REFINE falls through to the full refine agent
    assert triage_refine_called, "triage classifier SHOULD have been called"


# ===========================================================================
# feature flag / reviewer-comment guards still win
# ===========================================================================


def test_triage_skip_placeholder_fixture_feature_disabled(ctx_factory):
    """With refine_triage_enabled=False, triage_skip returns None before
    the junk guard — the caller handles the ticket as before."""
    ctx = ctx_factory(refine_triage_enabled=False)
    t = _ticket(ctx, "noop", "disregard placeholder")
    ws = ctx.service.workspace(t)

    result = _triage.triage_skip(
        ctx, t, "disregard placeholder", None, None, t.title, ws, ctx.settings, None
    )

    assert result is None


def test_triage_skip_placeholder_fixture_reviewer_comments_blocks(ctx_factory):
    """Reviewer comments (human sendback) take precedence over the junk
    guard — human-flagged tickets always fall through to refine."""
    ctx = ctx_factory()
    t = _ticket(ctx, "noop", "disregard placeholder")
    ws = ctx.service.workspace(t)

    result = _triage.triage_skip(
        ctx,
        t,
        "disregard placeholder",
        None,
        None,
        t.title,
        ws,
        ctx.settings,
        "please clarify scope",
    )

    assert result is None

"""Unit tests for the CI/dev-tooling proposal routing policy.

Covers :mod:`robotsix_mill.stages.refine.ci_tooling_policy` (pure
classification + route resolution) and the ``_ci_tooling_auto_disposition``
/ ``deterministic_triage_pre_checks`` wiring in
``robotsix_mill.stages.refine._triage`` — each route (auto-ready /
auto-decline / escalate) plus the empty-policy no-op default.  No
LLM/network collaborators are involved.
"""

from __future__ import annotations

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _triage
from robotsix_mill.stages.refine.ci_tooling_policy import (
    CIToolingRoute,
    classify_ci_tooling_proposal,
    resolve_ci_tooling_route,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path):
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


def _proposal_ticket(ctx, source, title, body):
    return ctx.service.create(title, body, source=source)


# ===========================================================================
# classify_ci_tooling_proposal (pure)
# ===========================================================================


def test_classify_recognises_known_classes():
    assert (
        classify_ci_tooling_proposal("Add a code coverage measurement gate", "")
        == "coverage"
    )
    assert (
        classify_ci_tooling_proposal("Add OpenSSF Scorecard workflow", "")
        == "scorecard"
    )
    assert (
        classify_ci_tooling_proposal("Add gitleaks secret scanner", "")
        == "secret-scanner"
    )
    assert (
        classify_ci_tooling_proposal("Enforce cyclomatic complexity with xenon", "")
        == "complexity"
    )
    assert (
        classify_ci_tooling_proposal("Add commitizen Conventional Commits check", "")
        == "conventional-commits"
    )


def test_classify_matches_body_case_insensitively():
    assert (
        classify_ci_tooling_proposal(
            "New gate", "This adds CHROMATIC visual regression"
        )
        == "visual-regression"
    )


def test_classify_returns_none_for_non_tooling_draft():
    assert classify_ci_tooling_proposal("Fix a null pointer in foo()", "body") is None
    assert classify_ci_tooling_proposal("Refactor the parser", "") is None


# ===========================================================================
# resolve_ci_tooling_route (pure)
# ===========================================================================


def test_resolve_defaults_to_escalate_on_empty_policy():
    s = Settings(data_dir="/tmp/x")
    assert resolve_ci_tooling_route("coverage", s) == CIToolingRoute.ESCALATE


def test_resolve_allowlist_maps_to_auto_ready():
    s = Settings(
        data_dir="/tmp/x",
        ci_tooling_proposal_policy={"coverage": "auto-ready"},
    )
    assert resolve_ci_tooling_route("coverage", s) == CIToolingRoute.AUTO_READY


def test_resolve_denylist_maps_to_auto_decline():
    s = Settings(
        data_dir="/tmp/x",
        ci_tooling_proposal_policy={"visual-regression": "auto-decline"},
    )
    assert (
        resolve_ci_tooling_route("visual-regression", s) == CIToolingRoute.AUTO_DECLINE
    )


def test_resolve_unlisted_follows_configured_default():
    s = Settings(data_dir="/tmp/x", ci_tooling_proposal_default="auto-decline")
    assert resolve_ci_tooling_route("coverage", s) == CIToolingRoute.AUTO_DECLINE


def test_resolve_bogus_override_falls_back_to_default():
    s = Settings(
        data_dir="/tmp/x",
        ci_tooling_proposal_policy={"coverage": "nonsense"},
    )
    assert resolve_ci_tooling_route("coverage", s) == CIToolingRoute.ESCALATE


def test_resolve_bogus_default_falls_back_to_escalate():
    s = Settings(data_dir="/tmp/x", ci_tooling_proposal_default="nonsense")
    assert resolve_ci_tooling_route("coverage", s) == CIToolingRoute.ESCALATE


# ===========================================================================
# _ci_tooling_auto_disposition (triage wiring)
# ===========================================================================


def test_non_proposal_source_falls_through(ctx_factory):
    ctx = ctx_factory(ci_tooling_proposal_policy={"coverage": "auto-ready"})
    t = _proposal_ticket(ctx, SourceKind.USER, "Add coverage gate", "")
    ws = ctx.service.workspace(t)
    assert (
        _triage._ci_tooling_auto_disposition(
            ctx, t, "Add coverage gate", ws, ctx.settings
        )
        is None
    )


def test_non_tooling_draft_falls_through(ctx_factory):
    ctx = ctx_factory(ci_tooling_proposal_default="auto-decline")
    t = _proposal_ticket(ctx, SourceKind.SURVEY, "Fix a flaky test", "body")
    ws = ctx.service.workspace(t)
    assert (
        _triage._ci_tooling_auto_disposition(
            ctx, t, "Fix a flaky test", ws, ctx.settings
        )
        is None
    )


def test_allowlisted_class_routes_to_ready(ctx_factory):
    ctx = ctx_factory(ci_tooling_proposal_policy={"coverage": "auto-ready"})
    t = _proposal_ticket(
        ctx, SourceKind.SURVEY, "Add a code coverage measurement gate", ""
    )
    ws = ctx.service.workspace(t)
    outcome = _triage._ci_tooling_auto_disposition(
        ctx, t, "Add a code coverage measurement gate", ws, ctx.settings
    )
    assert outcome is not None
    assert outcome.next_state == State.READY
    assert "auto-ready" in (outcome.note or "")


def test_denylisted_class_auto_closes_with_note(ctx_factory):
    ctx = ctx_factory(ci_tooling_proposal_policy={"visual-regression": "auto-decline"})
    t = _proposal_ticket(
        ctx, SourceKind.SURVEY, "Add Chromatic visual regression CI", ""
    )
    ws = ctx.service.workspace(t)
    outcome = _triage._ci_tooling_auto_disposition(
        ctx, t, "Add Chromatic visual regression CI", ws, ctx.settings
    )
    assert outcome is not None
    assert outcome.next_state == State.CLOSED
    assert "auto-decline" in (outcome.note or "")


def test_unlisted_class_escalate_default_falls_through(ctx_factory):
    """Default (empty policy, escalate) preserves today's human-gate path."""
    ctx = ctx_factory()
    t = _proposal_ticket(ctx, SourceKind.AUDIT, "Add OpenSSF Scorecard workflow", "")
    ws = ctx.service.workspace(t)
    assert (
        _triage._ci_tooling_auto_disposition(
            ctx, t, "Add OpenSSF Scorecard workflow", ws, ctx.settings
        )
        is None
    )


# ===========================================================================
# deterministic_triage_pre_checks integration (live verification)
# ===========================================================================


def test_pre_checks_allowlisted_reaches_ready_not_human_gate(ctx_factory):
    """A survey proposal of an allowlisted class reaches READY without
    entering HUMAN_ISSUE_APPROVAL."""
    ctx = ctx_factory(ci_tooling_proposal_policy={"secret-scanner": "auto-ready"})
    t = _proposal_ticket(
        ctx,
        SourceKind.SURVEY,
        "Add gitleaks secret scanner gate",
        "Introduce a gitleaks secret scanner as a required check.",
    )
    ws = ctx.service.workspace(t)
    outcome = _triage.deterministic_triage_pre_checks(
        ctx,
        t,
        "Introduce a gitleaks secret scanner as a required check.",
        ws,
        ctx.settings,
    )
    assert outcome is not None
    assert outcome.next_state == State.READY
    assert outcome.next_state != State.HUMAN_ISSUE_APPROVAL


def test_pre_checks_denylisted_class_auto_closes(ctx_factory):
    ctx = ctx_factory(ci_tooling_proposal_policy={"visual-regression": "auto-decline"})
    t = _proposal_ticket(
        ctx,
        SourceKind.SURVEY,
        "Add Chromatic visual regression CI",
        "Wire Chromatic visual regression into CI for the UI repo.",
    )
    ws = ctx.service.workspace(t)
    outcome = _triage.deterministic_triage_pre_checks(
        ctx,
        t,
        "Wire Chromatic visual regression into CI for the UI repo.",
        ws,
        ctx.settings,
    )
    assert outcome is not None
    assert outcome.next_state == State.CLOSED

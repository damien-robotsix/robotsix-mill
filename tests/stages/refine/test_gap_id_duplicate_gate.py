"""Tests for the gap-id duplicate gate in ``deterministic_triage_pre_checks``.

Periodic findings are re-filed under a stable ``<!-- {label}-gap-id: ... -->``
marker appended to the draft body at creation time (pass_runner).  The
creation-time gap-id dedup is bypassed when a draft takes a fast-path that
never reaches a dedup engine, so a survey 'Standardize:' proposal re-filed
under the same marker sails straight to approval while the original is still
open.  The gate consults the marker index on this board BEFORE any fast-path
disposition and closes the new draft as a duplicate.

No LLM/network collaborators are involved — every path asserted here is
deterministic (``dedup_lookback_days`` / auto-approve defaults are not
reached because the gate fires first).
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

# The marker pass_runner appends to a survey draft that carries a gap id.
SURVEY_MARKER = "<!-- survey-gap-id: standards_pr_title_conventional_commit_check -->"


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


def _survey_draft(body_extra: str = "") -> str:
    return (
        "Standardize Conventional Commits PR-title validation as a required "
        "release-please check on the fleet. This is a real proposal body."
        + (f"\n\n{body_extra}" if body_extra else "")
    )


def _ticket(ctx, source=SourceKind.SURVEY, marker=SURVEY_MARKER, **kw):
    body = _survey_draft()
    if marker:
        body += f"\n\n{marker}"
    return ctx.service.create(
        "Standardize: Conventional Commits PR-title check", body, source=source, **kw
    )


def test_open_duplicate_marker_closes_as_duplicate(ctx_factory):
    """A re-filed survey draft whose gap-id marker matches an OPEN ticket on
    the same board is closed as a duplicate of that ticket."""
    ctx = ctx_factory()
    original = _ticket(ctx)
    duplicate = _ticket(ctx)
    ws = ctx.service.workspace(duplicate)

    outcome = _triage.deterministic_triage_pre_checks(
        ctx, duplicate, _survey_draft() + f"\n\n{SURVEY_MARKER}", ws, ctx.settings
    )

    assert outcome is not None
    assert outcome.next_state == State.DONE
    note = outcome.note or ""
    assert note.startswith("duplicate of ")
    assert str(original.id) in note
    assert "survey-gap-id" in note


def test_closed_prior_ticket_does_not_fire(ctx_factory):
    """A CLOSED prior ticket is not a duplicate target — the gate lets the
    draft fall through to the normal triage path (None)."""
    ctx = ctx_factory()
    original = _ticket(ctx)
    ctx.service.transition(original.id, State.CLOSED)
    duplicate = _ticket(ctx)
    ws = ctx.service.workspace(duplicate)

    assert (
        _triage.deterministic_triage_pre_checks(
            ctx, duplicate, _survey_draft() + f"\n\n{SURVEY_MARKER}", ws, ctx.settings
        )
        is None
    )


def test_no_marker_falls_through(ctx_factory):
    """A draft without any gap-id marker is not touched by the gate."""
    ctx = ctx_factory()
    _ticket(ctx)  # open ticket WITHOUT... actually with marker; the draft has none
    t = ctx.service.create(
        "Standardize: Conventional Commits PR-title check",
        _survey_draft(),
        source=SourceKind.SURVEY,
    )
    ws = ctx.service.workspace(t)

    assert (
        _triage.deterministic_triage_pre_checks(
            ctx, t, _survey_draft(), ws, ctx.settings
        )
        is None
    )


def test_different_marker_value_falls_through(ctx_factory):
    """A draft whose marker value differs from the open ticket's is not a
    duplicate — the gate does not fire on marker-value mismatch."""
    ctx = ctx_factory()
    _ticket(ctx)  # open ticket carrying SURVEY_MARKER
    other_marker = "<!-- survey-gap-id: codecov_fail_under_gate -->"
    t = ctx.service.create(
        "Standardize: Coverage fail-under gate",
        _survey_draft() + f"\n\n{other_marker}",
        source=SourceKind.SURVEY,
    )
    ws = ctx.service.workspace(t)

    assert (
        _triage.deterministic_triage_pre_checks(
            ctx, t, _survey_draft() + f"\n\n{other_marker}", ws, ctx.settings
        )
        is None
    )


def test_gap_id_gate_precedes_ci_tooling_auto_ready(ctx_factory):
    """THE incident regression: an allowlisted CI-tooling class
    (conventional-commits → auto-ready) must NOT auto-approve a re-filed
    proposal while the original carrying the same survey-gap-id marker is
    still open — the gate closes the new draft as a duplicate instead."""
    ctx = ctx_factory(ci_tooling_proposal_policy={"conventional-commits": "auto-ready"})
    original = _ticket(ctx)
    duplicate = _ticket(ctx)
    ws = ctx.service.workspace(duplicate)

    outcome = _triage.deterministic_triage_pre_checks(
        ctx, duplicate, _survey_draft() + f"\n\n{SURVEY_MARKER}", ws, ctx.settings
    )

    assert outcome is not None
    assert outcome.next_state == State.DONE
    note = outcome.note or ""
    assert note.startswith("duplicate of ")
    assert str(original.id) in note

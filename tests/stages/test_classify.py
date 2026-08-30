"""Tests for the classify stage — async ops/scope/dedup classification."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import Outcome, StageContext
from robotsix_mill.stages.classify import ClassifyStage


@pytest.fixture
def classify_stage() -> ClassifyStage:
    return ClassifyStage()


@pytest.fixture
def service(settings) -> TicketService:
    return TicketService(settings, board_id="test-board")


def _make_classifying_ticket(service: TicketService, title: str = "Test", body: str = "Body"):
    """Create a ticket in CLASSIFYING state."""
    ticket = service.create(
        title=title,
        description=body,
        source="test",
        kind=TicketKind.TASK,
        board_id="test-board",
        initial_state=State.CLASSIFYING,
    )
    return ticket


def _make_ctx(service, settings, worker=None) -> StageContext:
    """Build a minimal StageContext for testing."""
    return StageContext(
        service=service,
        settings=settings,
        repo_config=None,
    )


# ---------------------------------------------------------------------------
# ops_classify — OPERATIONAL → CLOSED
# ---------------------------------------------------------------------------
def test_classify_operational_closes_ticket(classify_stage, service, settings):
    """A ticket classified as OPERATIONAL is closed."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    ticket = _make_classifying_ticket(service, "Rotate PAT token", "Rotate the PAT.")

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Manual credential rotation.",
        ),
    ), patch(
        "robotsix_mill.stages.classify.emit_diagnostic_event",
        return_value=True,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.CLOSED
    assert "operational-maintenance" in outcome.note


def test_classify_code_proceeds(classify_stage, service, settings):
    """A ticket classified as CODE proceeds to DRAFT."""
    from robotsix_mill.agents.ops_classify import OpsClassifyVerdict

    ticket = _make_classifying_ticket(service, "Fix bug", "Fix the bug in code.")

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=OpsClassifyVerdict(
            classification="CODE",
            reason="Code defect.",
        ),
    ), patch(
        "robotsix_mill.stages.classify.emit_diagnostic_event",
        return_value=True,
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
        return_value={"duplicate_of": None},
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT


def test_classify_ops_fail_open(classify_stage, service, settings):
    """When ops_classify fails, the ticket proceeds (fail-open)."""
    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        side_effect=RuntimeError("LLM timeout"),
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
        return_value={"duplicate_of": None},
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT


# ---------------------------------------------------------------------------
# LLM dedup — duplicate → CLOSED
# ---------------------------------------------------------------------------
def test_classify_dedup_closes_ticket(classify_stage, service, settings):
    """A ticket that is a semantic duplicate is closed."""
    existing = service.create(
        "Existing ticket",
        "Something went wrong.",
        source="test",
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    ticket = _make_classifying_ticket(service, "Same issue", "Same thing went wrong.")

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=True,
    ), patch(
        "robotsix_mill.stages.classify.rank_candidates_by_similarity",
        return_value=[existing],
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
        return_value={"duplicate_of": existing.id},
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.CLOSED
    assert existing.id in outcome.note


def test_classify_dedup_miss_proceeds(classify_stage, service, settings):
    """A ticket that is not a duplicate proceeds to DRAFT."""
    service.create(
        "Other ticket",
        "Different issue.",
        source="test",
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    ticket = _make_classifying_ticket(service, "New issue", "New thing went wrong.")

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=True,
    ), patch(
        "robotsix_mill.stages.classify.rank_candidates_by_similarity",
        return_value=[],
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
        return_value={"duplicate_of": None},
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT


def test_classify_dedup_fail_open(classify_stage, service, settings):
    """When LLM dedup fails, the ticket proceeds (fail-open)."""
    service.create(
        "Other ticket",
        "Different issue.",
        source="test",
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=True,
    ), patch(
        "robotsix_mill.stages.classify.rank_candidates_by_similarity",
        return_value=[],
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
        side_effect=RuntimeError("LLM timeout"),
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT


# ---------------------------------------------------------------------------
# scope_classify — EPIC → EPIC_OPEN
# ---------------------------------------------------------------------------
def test_classify_epic_promotes(classify_stage, service, settings):
    """A ticket classified as EPIC (above threshold) is promoted."""
    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.agents.scope_classify import ScopeVerdict

    ticket = _make_classifying_ticket(
        service,
        "Build the whole notifications subsystem",
        "Add email, SMS, and webhook delivery channels.",
    )

    breakdown = EpicBreakdownResult(
        child_titles=["Email", "SMS"],
        child_bodies=["Email body.", "SMS body."],
    )

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ), patch(
        "robotsix_mill.stages.classify.run_scope_classify_agent",
        return_value=ScopeVerdict(classification="EPIC", confidence=0.9, reason="Broad."),
    ), patch(
        "robotsix_mill.stages.classify.emit_diagnostic_event",
        return_value=True,
    ), patch(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        return_value=breakdown,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.EPIC_OPEN

    # Ticket promoted to epic.
    updated = service.get(ticket.id)
    assert updated.kind == TicketKind.EPIC


def test_classify_epic_below_threshold_stays_task(classify_stage, service, settings):
    """An EPIC verdict below the confidence threshold stays a task."""
    from robotsix_mill.agents.scope_classify import ScopeVerdict

    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ), patch(
        "robotsix_mill.stages.classify.run_scope_classify_agent",
        return_value=ScopeVerdict(classification="EPIC", confidence=0.5, reason="Borderline."),
    ), patch(
        "robotsix_mill.stages.classify.emit_diagnostic_event",
        return_value=True,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT

    # Ticket stays a task.
    updated = service.get(ticket.id)
    assert updated.kind == TicketKind.TASK


def test_classify_scope_disabled(classify_stage, service, settings):
    """When auto_epic_enabled is False, scope_classify is skipped."""
    settings.auto_epic_enabled = False
    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ), patch(
        "robotsix_mill.stages.classify.run_scope_classify_agent",
    ) as mock_scope:
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT
    mock_scope.assert_not_called()


def test_classify_scope_fail_open(classify_stage, service, settings):
    """When scope_classify fails, the ticket proceeds as a task."""
    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ), patch(
        "robotsix_mill.stages.classify.run_scope_classify_agent",
        side_effect=RuntimeError("LLM timeout"),
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT


# ---------------------------------------------------------------------------
# No candidates → skip LLM dedup
# ---------------------------------------------------------------------------
def test_classify_no_candidates_skips_dedup(classify_stage, service, settings):
    """When there are no candidates, LLM dedup is skipped."""
    ticket = _make_classifying_ticket(service)

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
    ) as mock_dedup, patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT
    mock_dedup.assert_not_called()


# ---------------------------------------------------------------------------
# No token overlap → skip LLM dedup
# ---------------------------------------------------------------------------
def test_classify_no_overlap_skips_dedup(classify_stage, service, settings):
    """When there's no token overlap, LLM dedup is skipped."""
    service.create(
        "12345 67890",
        "99999 00000",
        source="test",
        kind=TicketKind.TASK,
        board_id="test-board",
    )
    ticket = _make_classifying_ticket(service, "abcdef", "ghijkl")

    with patch(
        "robotsix_mill.stages.classify.run_ops_classify_agent",
        return_value=None,
    ), patch(
        "robotsix_mill.stages.classify.run_dedup_check",
    ) as mock_dedup, patch(
        "robotsix_mill.stages.classify.any_candidate_overlap",
        return_value=False,
    ):
        ctx = _make_ctx(service, settings)
        outcome = classify_stage.run(ticket, ctx)

    assert outcome.next_state == State.DRAFT
    mock_dedup.assert_not_called()

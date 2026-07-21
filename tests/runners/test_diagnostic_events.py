"""Tests for the diagnostic event store (emit, list, dedup) and the
recurring CI failure check."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.runners.diagnostic_check_recurring_ci import (
    RecurringCIFailureCheck,
)
from robotsix_mill.runners.diagnostic_checks import DiagnosticCheckContext
from robotsix_mill.runners.diagnostic_events import (
    DiagnosticEvent,
    emit_diagnostic_event,
    list_diagnostic_events,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a per-test data directory."""
    s = Settings()
    # Override data_dir to isolate test data.
    s.data_dir = tmp_path / "data"
    return s


@pytest.fixture
def board_id() -> str:
    return "test-board"


# ---------------------------------------------------------------------------
# emit / list / dedup
# ---------------------------------------------------------------------------


class TestEmitListDedup:
    def test_emit_and_list_single_event(self, settings, board_id):
        emitted = emit_diagnostic_event(
            settings,
            board_id,
            category="CI_FAILURE",
            ticket_id="ticket-1",
            reason="ruff check failed",
            normalized_key="abc123",
        )
        assert emitted is True

        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        ev = events[0]
        assert ev.category == "CI_FAILURE"
        assert ev.ticket_id == "ticket-1"
        assert ev.repo_id == board_id
        assert ev.reason == "ruff check failed"
        assert ev.normalized_key == "abc123"
        assert ev.timestamp  # non-empty ISO timestamp

    def test_emit_dedup_same_ticket_and_key(self, settings, board_id):
        first = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "reason", "key-1"
        )
        assert first is True

        second = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "reason", "key-1"
        )
        assert second is False  # deduped

        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1

    def test_emit_different_key_same_ticket(self, settings, board_id):
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "reason", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "reason", "key-2"
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 2

    def test_emit_different_ticket_same_key(self, settings, board_id):
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "reason", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-2", "reason", "key-1"
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 2

    def test_list_filtered_by_category(self, settings, board_id):
        emit_diagnostic_event(settings, board_id, "CI_FAILURE", "ticket-1", "r1", "k1")
        emit_diagnostic_event(settings, board_id, "OTHER", "ticket-2", "r2", "k2")

        ci_events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        assert len(ci_events) == 1
        assert ci_events[0].category == "CI_FAILURE"

        other_events = list_diagnostic_events(settings, board_id, category="OTHER")
        assert len(other_events) == 1

        all_events = list_diagnostic_events(settings, board_id)
        assert len(all_events) == 2

    def test_list_empty_when_no_file(self, settings, board_id):
        events = list_diagnostic_events(settings, board_id)
        assert events == []

    def test_list_skips_malformed_lines(self, settings, board_id):
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE","ticket_id":"ok","repo_id":"x",'
            '"reason":"r","normalized_key":"k","timestamp":"t"}\n'
            "not valid json\n"
            '{"category":"CI_FAILURE","ticket_id":"ok2","repo_id":"x",'
            '"reason":"r2","normalized_key":"k2","timestamp":"t2"}\n',
            encoding="utf-8",
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 2
        assert {e.ticket_id for e in events} == {"ok", "ok2"}

    def test_list_skips_missing_keys(self, settings, board_id):
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE"}\n',  # missing required keys
            encoding="utf-8",
        )
        events = list_diagnostic_events(settings, board_id)
        assert events == []

    def test_emit_creates_parent_dirs(self, settings, board_id):
        data_dir = settings.data_dir
        # Remove the data dir to confirm mkdir works.
        import shutil

        if data_dir.exists():
            shutil.rmtree(data_dir)
        emitted = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "t-1", "r", "k"
        )
        assert emitted is True
        assert settings.diagnostic_events_file_for(board_id).is_file()


# ---------------------------------------------------------------------------
# RecurringCIFailureCheck tests
# ---------------------------------------------------------------------------


class TestRecurringCIFailureCheck:
    def test_no_events_returns_ok(self, settings, board_id):
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()
        result = check.run(ctx)
        assert result.ok is True
        assert result.drafts_created == []

    def test_threshold_zero_disabled(self, settings, board_id):
        settings.diagnostic_ci_failure_threshold = 0
        # Emit some events anyway.
        for i in range(5):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"ticket-{i}", "r", "key-1"
            )
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()
        result = check.run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "disabled" in result.summary

    def test_below_threshold_no_drafts(self, settings, board_id):
        settings.diagnostic_ci_failure_threshold = 3
        # 2 distinct tickets — below threshold of 3.
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-1", "r", "key-1"
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-2", "r", "key-1"
        )
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()
        result = check.run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "none reached threshold" in result.summary

    def test_at_threshold_files_draft(self, settings, board_id, monkeypatch):
        settings.diagnostic_ci_failure_threshold = 3
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"ticket-{i}", "r", "key-1"
            )

        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()

        # Mock TicketService to avoid real DB interaction.
        from unittest.mock import MagicMock

        mock_service = MagicMock()
        mock_service.list.return_value = []  # no duplicates
        mock_ticket = MagicMock()
        mock_ticket.id = "draft-1"
        mock_ticket.title = "[diagnostic] recurring CI failure: key=abc123 (3 tickets)"
        mock_service.create.return_value = mock_ticket

        import robotsix_mill.runners.diagnostic_check_recurring_ci as check_mod

        monkeypatch.setattr(check_mod, "TicketService", lambda *a, **kw: mock_service)

        result = check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 1
        assert mock_service.create.called

    def test_multiple_keys_above_threshold(self, settings, board_id, monkeypatch):
        settings.diagnostic_ci_failure_threshold = 2
        # key-1: 3 tickets, key-2: 2 tickets — both at/above threshold.
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"ta-{i}", "r", "key-1"
            )
        for i in range(2):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"tb-{i}", "r", "key-2"
            )

        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()

        from unittest.mock import MagicMock

        mock_service = MagicMock()
        mock_service.list.return_value = []
        mock_ticket = MagicMock()
        mock_ticket.id = "draft-1"
        mock_service.create.return_value = mock_ticket

        import robotsix_mill.runners.diagnostic_check_recurring_ci as check_mod

        monkeypatch.setattr(check_mod, "TicketService", lambda *a, **kw: mock_service)

        result = check.run(ctx)
        assert result.ok is True
        assert len(result.drafts_created) == 2
        assert mock_service.create.call_count == 2

    def test_duplicate_title_skipped(self, settings, board_id, monkeypatch):
        settings.diagnostic_ci_failure_threshold = 2
        for i in range(3):
            emit_diagnostic_event(
                settings, board_id, "CI_FAILURE", f"ticket-{i}", "r", "key-1"
            )

        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        check = RecurringCIFailureCheck()

        from unittest.mock import MagicMock

        # Simulate an existing open ticket with the same title.
        existing_ticket = MagicMock()
        existing_ticket.title = (
            "[diagnostic] recurring CI failure: key=key-1 (3 tickets)"
        )
        existing_ticket.state = "draft"

        mock_service = MagicMock()
        mock_service.list.return_value = [existing_ticket]

        import robotsix_mill.runners.diagnostic_check_recurring_ci as check_mod

        monkeypatch.setattr(check_mod, "TicketService", lambda *a, **kw: mock_service)

        result = check.run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert not mock_service.create.called


# ---------------------------------------------------------------------------
# DiagnosticEvent dataclass
# ---------------------------------------------------------------------------


class TestDiagnosticEventDataclass:
    def test_construction_and_attributes(self):
        ev = DiagnosticEvent(
            category="CI_FAILURE",
            ticket_id="t-1",
            repo_id="r-1",
            reason="test failure",
            normalized_key="abc123",
            timestamp="2025-01-01T00:00:00Z",
        )
        assert ev.category == "CI_FAILURE"
        assert ev.ticket_id == "t-1"
        assert ev.repo_id == "r-1"
        assert ev.reason == "test failure"
        assert ev.normalized_key == "abc123"

    def test_frozen(self):
        ev = DiagnosticEvent(
            category="X",
            ticket_id="t",
            repo_id="r",
            reason="r",
            normalized_key="k",
            timestamp="t",
        )
        with pytest.raises(AttributeError):
            ev.category = "Y"  # type: ignore[misc]

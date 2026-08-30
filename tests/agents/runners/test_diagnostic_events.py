"""Tests for the diagnostic event store (emit, list, dedup) and the
recurring CI failure check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robotsix_mill.agents.runners.diagnostic_check_recurring_ci import (
    RecurringCIFailureCheck,
)
from robotsix_mill.agents.runners.diagnostic_checks import DiagnosticCheckContext
from robotsix_mill.agents.runners.diagnostic_events import (
    DiagnosticEvent,
    emit_diagnostic_event,
    list_diagnostic_events,
)
from robotsix_mill.config import Settings

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
    """The check is summary-only: it must never touch the ticket service."""

    @pytest.fixture(autouse=True)
    def _no_ticket_service(self, monkeypatch):
        # The module no longer imports TicketService at all; guard the
        # service class itself so any future re-introduction of ticket
        # filing (via any import path) fails loudly here.
        from robotsix_mill.core import service as service_mod

        def _boom(*_a, **_kw):
            raise AssertionError("recurring_ci_failure must not create tickets")

        monkeypatch.setattr(service_mod.TicketService, "create", _boom)

    def test_no_events_returns_ok(self, settings, board_id):
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        result = RecurringCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "no CI_FAILURE events" in result.summary

    def test_many_tickets_same_key_files_nothing(self, settings, board_id):
        # This is exactly the shape that used to trigger a report ticket:
        # well past the (now inert) threshold on a single normalized key.
        settings.diagnostic_ci_failure_threshold = 3
        for i in range(8):
            emit_diagnostic_event(
                settings,
                board_id,
                "CI_FAILURE",
                f"ticket-{i}",
                "failing checks: ci / tests",
                "key-1",
                bucket="ruff-format" if i % 2 else "mypy",
            )
        emit_diagnostic_event(
            settings, board_id, "CI_FIX_RESOLVED", "ticket-1", "fixed", "key-1"
        )
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        result = RecurringCIFailureCheck().run(ctx)
        assert result.ok is True
        assert result.drafts_created == []
        assert "8 CI_FAILURE event(s) across 8 ticket(s)" in result.summary
        assert "ruff-format=4" in result.summary
        assert "mypy=4" in result.summary
        assert "1 resolved by ci_fix" in result.summary
        assert "no tickets filed" in result.summary

    def test_threshold_zero_still_summarises(self, settings, board_id):
        settings.diagnostic_ci_failure_threshold = 0
        emit_diagnostic_event(settings, board_id, "CI_FAILURE", "t", "r", "k")
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        result = RecurringCIFailureCheck().run(ctx)
        assert result.ok is True
        assert "unknown=1" in result.summary  # legacy events have no bucket


# ---------------------------------------------------------------------------
# Semantic fields (bucket / root_cause / prevention_rule)
# ---------------------------------------------------------------------------


class TestSemanticFields:
    def test_roundtrip(self, settings, board_id):
        emit_diagnostic_event(
            settings,
            board_id,
            "CI_FAILURE",
            "t-1",
            "failing checks: ci / tests",
            "k-1",
            bucket="mypy",
            root_cause="error: Incompatible return value type",
            prevention_rule="Run mypy before stopping.",
        )
        (ev,) = list_diagnostic_events(settings, board_id)
        assert ev.bucket == "mypy"
        assert ev.root_cause == "error: Incompatible return value type"
        assert ev.prevention_rule == "Run mypy before stopping."

    def test_legacy_lines_without_fields_still_load(self, settings, board_id):
        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"category":"CI_FAILURE","ticket_id":"old","repo_id":"x",'
            '"reason":"r","normalized_key":"k","timestamp":"t"}\n',
            encoding="utf-8",
        )
        (ev,) = list_diagnostic_events(settings, board_id)
        assert ev.ticket_id == "old"
        assert ev.bucket == ""
        assert ev.root_cause == ""
        assert ev.prevention_rule == ""

    def test_plain_event_keeps_historical_line_shape(self, settings, board_id):
        import json

        emit_diagnostic_event(settings, board_id, "CI_FAILURE", "t", "r", "k")
        line = settings.diagnostic_events_file_for(board_id).read_text().strip()
        assert set(json.loads(line)) == {
            "category",
            "ticket_id",
            "repo_id",
            "reason",
            "normalized_key",
            "timestamp",
        }

    def test_dedup_is_per_category(self, settings, board_id):
        # A CI_FIX_RESOLVED event pairs with its CI_FAILURE twin on the same
        # (ticket, key) — it must not be swallowed by the dedup.
        assert emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "t-1", "r", "k-1"
        )
        assert emit_diagnostic_event(
            settings, board_id, "CI_FIX_RESOLVED", "t-1", "fixed", "k-1"
        )
        assert not emit_diagnostic_event(
            settings, board_id, "CI_FIX_RESOLVED", "t-1", "fixed again", "k-1"
        )
        assert len(list_diagnostic_events(settings, board_id)) == 2


# ---------------------------------------------------------------------------
# DiagnosticEvent dataclass
# ---------------------------------------------------------------------------


class TestListAging:
    """Tests for event aging (diagnostic_events_max_age_days)."""

    @pytest.fixture
    def recent_event(self, settings, board_id):
        """Emit one event with current timestamp."""
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-recent", "r", "k-recent"
        )

    def _write_event(
        self,
        settings: Settings,
        board_id: str,
        ticket_id: str,
        key: str,
        timestamp: str,
    ) -> None:
        """Directly write an event with an explicit timestamp to bypass
        emit_diagnostic_event's dedup."""
        import json

        path = settings.diagnostic_events_file_for(board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        event = DiagnosticEvent(
            category="CI_FAILURE",
            ticket_id=ticket_id,
            repo_id=board_id,
            reason="r",
            normalized_key=key,
            timestamp=timestamp,
        )
        line = json.dumps(
            {
                "category": event.category,
                "ticket_id": event.ticket_id,
                "repo_id": event.repo_id,
                "reason": event.reason,
                "normalized_key": event.normalized_key,
                "timestamp": event.timestamp,
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def test_aging_zero_keeps_all(self, settings, board_id):
        """Aging disabled (0) returns all events regardless of age."""
        settings.diagnostic_events_max_age_days = 0
        self._write_event(
            settings,
            board_id,
            "ticket-old",
            "k-old",
            "2000-01-01T00:00:00+00:00",
        )
        emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-new", "r", "k-new"
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 2

    def test_old_events_filtered_with_default_90_days(self, settings, board_id):
        """Events older than 90 days are filtered by default."""
        # Default is 90 days.
        assert settings.diagnostic_events_max_age_days == 90
        # Event from 120 days ago.
        self._write_event(
            settings,
            board_id,
            "ticket-old",
            "k-old",
            (datetime.now(UTC) - timedelta(days=120)).isoformat(),
        )
        # Event from 30 days ago — within window.
        self._write_event(
            settings,
            board_id,
            "ticket-recent",
            "k-recent",
            (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        assert events[0].ticket_id == "ticket-recent"

    def test_malformed_timestamp_keeps_event(self, settings, board_id):
        """An event with a non-parseable timestamp is kept (not silently
        dropped) — age filtering should degrade gracefully."""
        settings.diagnostic_events_max_age_days = 90
        self._write_event(
            settings,
            board_id,
            "ticket-bad-ts",
            "k-bad",
            "not-a-valid-iso-timestamp",
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        assert events[0].ticket_id == "ticket-bad-ts"

    def test_custom_max_age(self, settings, board_id):
        """Custom max-age filters differently from the default."""
        settings.diagnostic_events_max_age_days = 10
        # 5 days ago — within window.
        self._write_event(
            settings,
            board_id,
            "ticket-5d",
            "k-5d",
            (datetime.now(UTC) - timedelta(days=5)).isoformat(),
        )
        # 15 days ago — outside window.
        self._write_event(
            settings,
            board_id,
            "ticket-15d",
            "k-15d",
            (datetime.now(UTC) - timedelta(days=15)).isoformat(),
        )
        events = list_diagnostic_events(settings, board_id)
        assert len(events) == 1
        assert events[0].ticket_id == "ticket-5d"

    def test_aging_preserves_dedup_semantics(self, settings, board_id):
        """Aging doesn't affect the dedup guard in emit — the (ticket, key)
        check still writes a fresh event when the old one was aged out."""
        settings.diagnostic_events_max_age_days = 30
        # Write an event from 60 days ago.
        self._write_event(
            settings,
            board_id,
            "ticket-a",
            "key-a",
            (datetime.now(UTC) - timedelta(days=60)).isoformat(),
        )
        # emit with same ticket+key — dedup guard reads the raw file and
        # finds the old entry, so it should skip (dedup is on raw rows,
        # not age-filtered rows).
        emitted = emit_diagnostic_event(
            settings, board_id, "CI_FAILURE", "ticket-a", "r", "key-a"
        )
        # Even though the old event is aged out of list, the raw file
        # still has the duplicate ticket+key pair, so emit should skip.
        assert emitted is False


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

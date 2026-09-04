"""Tests for the error-detection diagnostic check
(``runners.diagnostic_check_errors.ErroredRunsCheck``).

Uses a real :class:`TicketService` backed by a ``tmp_path`` SQLite DB
(like the rest of the suite); only the ``query_run_errors`` data seam is
monkeypatched (in the check's own namespace, the name as imported). The
check reads ``ctx.board_id`` / ``ctx.settings`` from the
:class:`DiagnosticCheckContext` the runner passes, so the tests build a
context backed by a sandboxed settings whose board DB we initialize.

The check files **no tickets**: findings are surfaced as diagnostic
events in the JSONL event store, so every test asserts both that no
ticket is created and that (where expected) events were emitted.
"""

from __future__ import annotations

from robotsix_mill.agents.runners import diagnostic_check_errors as dce
from robotsix_mill.agents.runners import diagnostic_checks as dc
from robotsix_mill.agents.runners.diagnostic_checks import DiagnosticCheckContext
from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService

_BOARD = "robotsix-mill"


def _prepare(tmp_path, monkeypatch):
    """Init a sandboxed DB for the diagnostic board and build a context."""
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(settings, board_id=_BOARD)
    return settings


def _ctx(settings):
    return DiagnosticCheckContext(board_id=_BOARD, settings=settings)


def _error_run(id, kind, started_at, error, summary=""):
    return {
        "id": id,
        "kind": kind,
        "started_at": started_at,
        "finished_at": started_at,
        "status": "error",
        "summary": summary,
        "error": error,
        "repo_id": "r",
    }


# --- detection: event emitted, no ticket created ---------------------------


def test_detection_emits_event_and_no_ticket(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run(
                "run-1",
                "bc_check",
                "2026-06-14T00:00:00+00:00",
                "YAML parse error\n  could not find expected ':'",
            )
        ],
    )

    result = dce.ErroredRunsCheck().run(_ctx(settings))

    assert result.ok is True
    assert result.drafts_created == []
    # No ticket was created on the board.
    assert TicketService(settings, board_id=_BOARD).list() == []

    events = list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN")
    assert len(events) == 1
    ev = events[0]
    assert ev.ticket_id == "run-1"
    assert "bc_check" in ev.reason
    assert "YAML parse error" in ev.reason
    assert "no tickets filed" in result.summary


# --- dedup -----------------------------------------------------------------


def test_dedup_no_duplicate_on_second_pass(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "boom")
        ],
    )

    first = dce.ErroredRunsCheck().run(_ctx(settings))
    assert first.ok is True

    second = dce.ErroredRunsCheck().run(_ctx(settings))
    assert second.ok is True

    # Same (category, ticket_id, normalized_key) is deduplicated by the
    # event store, so the second pass emits nothing new and no ticket.
    events = list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN")
    assert len(events) == 1
    assert TicketService(settings, board_id=_BOARD).list() == []


# --- per-unique-error separation -------------------------------------------


def test_distinct_fingerprints_yield_two_events(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "alpha"),
            _error_run("run-2", "audit", "2026-06-14T01:00:00+00:00", "beta"),
        ],
    )
    result = dce.ErroredRunsCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN")) == 2
    assert TicketService(settings, board_id=_BOARD).list() == []


def test_identical_fingerprints_collapse_to_one_event(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "same boom"),
            _error_run("run-2", "bc_check", "2026-06-14T01:00:00+00:00", "same boom"),
        ],
    )
    result = dce.ErroredRunsCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN")) == 1
    assert TicketService(settings, board_id=_BOARD).list() == []


# --- no errors -------------------------------------------------------------


def test_restart_interrupted_runs_are_not_filed(tmp_path, monkeypatch):
    """Runs killed by a process restart are a deploy artifact, not a defect
    (mill c05c, 2026-08-26): no event, and genuine errors alongside them
    are still emitted."""
    from robotsix_mill.runtime.run_registry import RESTART_INTERRUPTED_ERROR

    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run(
                "r1",
                "completeness_check",
                "2026-08-26T06:03:22+00:00",
                RESTART_INTERRUPTED_ERROR,
            ),
            _error_run(
                "r2",
                "completeness_check",
                "2026-08-26T06:35:12+00:00",
                RESTART_INTERRUPTED_ERROR,
            ),
            _error_run(
                "r3",
                "pin_bump",
                "2026-08-26T06:43:48+00:00",
                "invalid group reference 11 at position 1",
            ),
        ],
    )
    result = dce.ErroredRunsCheck().run(_ctx(settings))

    assert result.ok is True
    events = list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN")
    assert len(events) == 1
    assert "pin_bump" in events[0].reason
    assert not any(
        "interrupted by process restart" in ev.reason for ev in events
    )
    assert TicketService(settings, board_id=_BOARD).list() == []


def test_only_restart_interrupted_runs_is_ok_with_no_events(tmp_path, monkeypatch):
    from robotsix_mill.runtime.run_registry import RESTART_INTERRUPTED_ERROR

    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run(
                "r1", "audit", "2026-08-26T06:03:22+00:00", RESTART_INTERRUPTED_ERROR
            ),
        ],
    )
    result = dce.ErroredRunsCheck().run(_ctx(settings))
    assert result.ok is True
    assert list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN") == []
    assert TicketService(settings, board_id=_BOARD).list() == []


def test_no_errors_returns_ok_no_events(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(dce, "query_run_errors", lambda board_id, **k: [])
    result = dce.ErroredRunsCheck().run(_ctx(settings))
    assert result.ok is True
    assert result.drafts_created == []
    assert list_diagnostic_events(settings, _BOARD, category="ERRORED_RUN") == []


# --- fail-safe -------------------------------------------------------------


def test_outage_empty_errors_is_safe(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    # The data layer log-and-swallows outages by returning [].
    monkeypatch.setattr(dce, "query_run_errors", lambda board_id, **k: [])
    result = dce.ErroredRunsCheck().run(_ctx(settings))
    assert result.ok is True
    assert result.drafts_created == []


# --- registration ----------------------------------------------------------


def test_check_is_registered():
    names = [c.name for c in dc.get_registered_checks()]
    assert "errored_runs" in names

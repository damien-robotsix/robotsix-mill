"""Tests for the error-detection diagnostic check
(``runners.diagnostic_check_errors.ErroredRunsCheck``).

Uses a real :class:`TicketService` backed by a ``tmp_path`` SQLite DB
(like the rest of the suite); only the ``query_run_errors`` data seam is
monkeypatched (in the check's own namespace, the name as imported). The
check pulls its own ``Settings()``, so we monkeypatch
``diagnostic_check_errors.Settings`` to return a sandboxed settings whose
``diagnostic_target_repo_id`` board DB we initialize.
"""

from __future__ import annotations

import logging

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.runners import diagnostic_check_errors as dce
from robotsix_mill.runners import diagnostic_checks as dc

_BOARD = "robotsix-mill"


def _prepare(tmp_path, monkeypatch):
    """Init a sandboxed DB for the diagnostic board and pin Settings()."""
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(settings, board_id=_BOARD)
    monkeypatch.setattr(dce, "Settings", lambda: settings)
    return settings


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


# --- detection + filing ----------------------------------------------------


def test_detection_files_draft_with_full_context(tmp_path, monkeypatch, caplog):
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

    with caplog.at_level(logging.INFO, logger=dce.log.name):
        result = dce.ErroredRunsCheck().run()

    assert result.ok is True
    assert len(result.drafts_created) == 1
    ticket_id = result.drafts_created[0]["id"]

    service = TicketService(settings, board_id=_BOARD)
    ticket = service.get(ticket_id)
    assert ticket is not None
    assert ticket.source == SourceKind.AGENT
    assert ticket.state.value == "draft"
    body = service.workspace(ticket).read_description()
    assert _BOARD in body
    assert "bc_check" in body
    assert "run-1" in body
    assert "2026-06-14T00:00:00+00:00" in body
    assert "YAML parse error" in body

    messages = [r.getMessage() for r in caplog.records]
    assert any("detected" in m for m in messages)
    assert any("created ticket" in m for m in messages)


# --- dedup -----------------------------------------------------------------


def test_dedup_no_duplicate_on_second_pass(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "boom")
        ],
    )

    first = dce.ErroredRunsCheck().run()
    assert len(first.drafts_created) == 1

    second = dce.ErroredRunsCheck().run()
    assert second.drafts_created == []


def test_terminal_ticket_does_not_block_creation(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "boom")
        ],
    )

    first = dce.ErroredRunsCheck().run()
    assert len(first.drafts_created) == 1

    # Drive the existing ticket to a terminal state.
    service = TicketService(settings, board_id=_BOARD)
    service.mark_done(first.drafts_created[0]["id"])

    second = dce.ErroredRunsCheck().run()
    assert len(second.drafts_created) == 1  # terminal ticket does not suppress


# --- per-unique-error separation -------------------------------------------


def test_distinct_fingerprints_yield_two_tickets(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "alpha"),
            _error_run("run-2", "audit", "2026-06-14T01:00:00+00:00", "beta"),
        ],
    )
    result = dce.ErroredRunsCheck().run()
    assert len(result.drafts_created) == 2


def test_identical_fingerprints_collapse_to_one_ticket(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "same boom"),
            _error_run("run-2", "bc_check", "2026-06-14T01:00:00+00:00", "same boom"),
        ],
    )
    result = dce.ErroredRunsCheck().run()
    assert len(result.drafts_created) == 1


# --- no errors -------------------------------------------------------------


def test_no_errors_returns_ok_no_drafts(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(dce, "query_run_errors", lambda board_id, **k: [])
    result = dce.ErroredRunsCheck().run()
    assert result.ok is True
    assert result.drafts_created == []


# --- fail-safe -------------------------------------------------------------


def test_create_failure_does_not_propagate(tmp_path, monkeypatch, caplog):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dce,
        "query_run_errors",
        lambda board_id, **k: [
            _error_run("run-1", "bc_check", "2026-06-14T00:00:00+00:00", "alpha"),
            _error_run("run-2", "audit", "2026-06-14T01:00:00+00:00", "beta"),
        ],
    )

    calls = {"n": 0}
    orig_create = TicketService.create

    def flaky_create(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db exploded")
        return orig_create(self, *a, **k)

    monkeypatch.setattr(TicketService, "create", flaky_create)

    with caplog.at_level(logging.ERROR, logger=dce.log.name):
        result = dce.ErroredRunsCheck().run()

    # First group failed, but the second still produced a ticket.
    assert result.ok is True
    assert len(result.drafts_created) == 1
    assert any("failed to file ticket" in r.getMessage() for r in caplog.records)


def test_outage_empty_errors_is_safe(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    # The data layer log-and-swallows outages by returning [].
    monkeypatch.setattr(dce, "query_run_errors", lambda board_id, **k: [])
    result = dce.ErroredRunsCheck().run()
    assert result.ok is True
    assert result.drafts_created == []


# --- registration ----------------------------------------------------------


def test_check_is_registered():
    names = [c.name for c in dc.get_registered_checks()]
    assert "errored_runs" in names

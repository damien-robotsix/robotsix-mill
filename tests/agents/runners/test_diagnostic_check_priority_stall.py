"""Tests for the priority-stall diagnostic check
(``runners.diagnostic_check_priority_stall.PriorityStallCheck``).

Uses a real :class:`TicketService` backed by a ``tmp_path`` SQLite DB.
The forge seam (``get_forge``) is monkeypatched so no network calls
are made.  The check reads ``ctx.board_id`` / ``ctx.settings`` from
the :class:`DiagnosticCheckContext` the runner passes, so the tests
build a context backed by a sandboxed settings whose board DB we
initialize.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from robotsix_mill.agents.runners import diagnostic_check_priority_stall as dcp
from robotsix_mill.agents.runners.diagnostic_checks import DiagnosticCheckContext
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.models import Ticket as TicketModel
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

_BOARD = "robotsix-mill"


def _prepare(tmp_path, monkeypatch):
    """Init a sandboxed DB for the diagnostic board and build a context."""
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path), require_approval="false")
    db.init_db(settings, board_id=_BOARD)
    return settings


def _ctx(settings):
    return DiagnosticCheckContext(board_id=_BOARD, settings=settings)


def _make_priority_ticket(
    service: TicketService, settings: Settings, *, stuck: bool = True
) -> str:
    """Create a priority ticket at IMPLEMENT_COMPLETE.

    When *stuck* is True the ticket's ``updated_at`` is back-dated
    beyond the 20-minute threshold so the check treats it as stuck.

    State and priority are set via direct DB write to bypass
    transition validation (the pipeline's normal path is many
    stages, which tests don't need).
    """
    ticket = service.create(
        "fix: critical bug",
        "description",
        source=SourceKind.AGENT,
    )

    with db.session(settings, service.board_id) as s:
        row = s.get(TicketModel, ticket.id)
        row.state = State.IMPLEMENT_COMPLETE
        row.priority = True
        if stuck:
            row.updated_at = datetime.now(UTC) - timedelta(minutes=30)
        s.add(row)
        s.commit()

    return ticket.id


class _FakeForge:
    """Minimal forge stub for priority-stall tests."""

    def __init__(
        self,
        pr: dict | None = None,
        ci: dict | None = None,
        pr_error: Exception | None = None,
        ci_error: Exception | None = None,
    ):
        self._pr = pr
        self._ci = ci
        self._pr_error = pr_error
        self._ci_error = ci_error

    def pr_status(self, *, source_branch: str) -> dict | None:
        if self._pr_error:
            raise self._pr_error
        return self._pr

    def check_status(self, *, source_branch: str) -> dict | None:
        if self._ci_error:
            raise self._ci_error
        return self._ci


# --- no priority tickets ---------------------------------------------------


def test_no_priority_tickets_returns_ok(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert result.drafts_created == []
    assert "no priority" in result.summary


# --- priority ticket not yet stuck -----------------------------------------


def test_priority_ticket_not_stuck_yet(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings, stuck=False)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert result.drafts_created == []
    assert "none stuck" in result.summary


# --- stuck ticket: CI green and mergeable ----------------------------------


def test_stuck_ticket_ci_green_mergeable(tmp_path, monkeypatch, caplog):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    tid = _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    with caplog.at_level(logging.INFO, logger=dcp.log.name):
        result = dcp.PriorityStallCheck().run(_ctx(settings))

    assert result.ok is True
    assert len(result.drafts_created) == 1
    diag = service.get(result.drafts_created[0]["id"])
    assert diag is not None
    assert diag.source == SourceKind.AGENT
    body = service.workspace(diag).read_description()
    assert tid in body
    assert "green" in body.lower() or "mergeable" in body.lower()


# --- stuck ticket: CI failing ----------------------------------------------


def test_stuck_ticket_ci_failing(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={
            "conclusion": "failure",
            "pending": [],
            "failing": [
                {"name": "lint"},
                {"name": "test-py314"},
            ],
        },
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "lint" in body
    assert "test-py314" in body


# --- stuck ticket: merge conflicts -----------------------------------------


def test_stuck_ticket_merge_conflicts(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": False,
            "mergeable_state": "dirty",
            "merged": False,
            "state": "open",
        },
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "conflict" in body.lower()


# --- stuck ticket: branch behind target ------------------------------------


def test_stuck_ticket_branch_behind(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "behind",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "behind" in body.lower()


# --- stuck ticket: no PR found ---------------------------------------------


def test_stuck_ticket_no_pr(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(pr=None)
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "no PR" in body


# --- stuck ticket: PR already merged ---------------------------------------


def test_stuck_ticket_pr_merged(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "merged": True,
            "state": "closed",
        },
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "merged" in body.lower()


# --- stuck ticket: PR closed -----------------------------------------------


def test_stuck_ticket_pr_closed(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "merged": False,
            "state": "closed",
        },
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "closed" in body.lower()


# --- stuck ticket: PR status error -----------------------------------------


def test_stuck_ticket_pr_status_error(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(pr_error=RuntimeError("rate limited"))
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "failed" in body.lower()


# --- stuck ticket: CI status error -----------------------------------------


def test_stuck_ticket_ci_status_error(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci_error=RuntimeError("API down"),
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "failed" in body.lower()


# --- stuck ticket: CI pending ----------------------------------------------


def test_stuck_ticket_ci_pending(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={
            "conclusion": "pending",
            "pending": ["build", "lint"],
            "failing": [],
        },
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    # CI still running is not a stall — the ticket is waiting on exactly the
    # thing it should be waiting on, so no diagnostic is filed.
    assert result.drafts_created == []
    assert "waiting on in-flight CI" in result.summary


# --- dedup: second pass does not file duplicate -----------------------------


def test_dedup_no_duplicate_on_second_pass(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    first = dcp.PriorityStallCheck().run(_ctx(settings))
    assert len(first.drafts_created) == 1

    second = dcp.PriorityStallCheck().run(_ctx(settings))
    assert second.drafts_created == []


# --- dedup: survives a change in the investigation summary -----------------


def test_dedup_survives_summary_drift(tmp_path, monkeypatch):
    """A second pass that reaches a *different* verdict still dedups.

    The title embeds a slice of the investigation summary, so matching on
    the whole title would file a fresh ticket every time the PR moved
    (behind → blocked → green).  Dedup is scoped to the stalled ticket id.
    """
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    pr = {
        "url": "https://github.com/org/repo/pull/42",
        "mergeable": True,
        "mergeable_state": "behind",
        "merged": False,
        "state": "open",
    }
    monkeypatch.setattr(
        dcp,
        "get_forge",
        lambda *a, **k: _FakeForge(
            pr=pr, ci={"conclusion": "success", "pending": [], "failing": []}
        ),
    )
    first = dcp.PriorityStallCheck().run(_ctx(settings))
    assert len(first.drafts_created) == 1
    assert "behind" in first.drafts_created[0]["title"]

    # Same stalled ticket, different verdict → same stall, no new ticket.
    pr["mergeable_state"] = "blocked"
    second = dcp.PriorityStallCheck().run(_ctx(settings))
    assert second.drafts_created == []


# --- dedup: terminal ticket does not block new creation ---------------------


def test_terminal_ticket_does_not_block_creation(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    first = dcp.PriorityStallCheck().run(_ctx(settings))
    assert len(first.drafts_created) == 1

    # Drive the diagnostic ticket to a terminal state.
    service.mark_done(first.drafts_created[0]["id"])

    second = dcp.PriorityStallCheck().run(_ctx(settings))
    assert len(second.drafts_created) == 1


# --- forge resolution failure ----------------------------------------------


def test_forge_resolution_failure(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    monkeypatch.setattr(
        dcp,
        "get_forge",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no forge")),
    )

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "forge" in body.lower()


# --- multiple stuck tickets ------------------------------------------------


def test_multiple_stuck_tickets(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 2


# --- check raises exception ------------------------------------------------


def test_exception_returns_not_ok(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dcp.PriorityStallCheck,
        "_run",
        lambda self, ctx: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is False
    assert "exception" in result.summary


# --- CI green but mergeable_state blocked ----------------------------------


def test_stuck_ticket_ci_green_but_blocked(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "blocked",
            "merged": False,
            "state": "open",
        },
        ci={"conclusion": "success", "pending": [], "failing": []},
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "blocked" in body.lower() or "protection" in body.lower()


# --- no CI data available --------------------------------------------------


def test_stuck_ticket_no_ci_data(tmp_path, monkeypatch):
    settings = _prepare(tmp_path, monkeypatch)
    service = TicketService(settings, board_id=_BOARD)
    _make_priority_ticket(service, settings)

    forge = _FakeForge(
        pr={
            "url": "https://github.com/org/repo/pull/42",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "state": "open",
        },
        ci=None,
    )
    monkeypatch.setattr(dcp, "get_forge", lambda *a, **k: forge)

    result = dcp.PriorityStallCheck().run(_ctx(settings))
    assert result.ok is True
    assert len(result.drafts_created) == 1
    body = service.workspace(
        service.get(result.drafts_created[0]["id"])
    ).read_description()
    assert "no CI" in body or "unknown" in body.lower()

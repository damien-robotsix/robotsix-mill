"""Tests for orphan-workspace detection in data_dir_audit_runner.

Covers ticket 5 of the data-dir audit epic: ``find_orphan_workspaces``
plus its integration into ``run_data_dir_audit_pass``.
"""

from __future__ import annotations

import logging

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.states import State
from robotsix_mill.data_dir_audit_runner import (
    OrphanWorkspace,
    find_orphan_workspaces,
    run_data_dir_audit_pass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path) -> Settings:
    """Build a fresh Settings rooted at *tmp_path*.

    Engines are also reset so each test gets a clean per-board DB
    cache (the engine cache survives across tests otherwise).
    """
    db.reset_engine()
    return Settings(data_dir=str(tmp_path), require_approval="false")


def _make_workspace_dir(settings: Settings, board_id: str, ticket_id: str):
    """Create ``<data_dir>/<board>/workspaces/<ticket_id>/`` with a
    tiny payload file so dir-size accounting has something to sum."""
    ws_dir = settings.workspaces_dir_for(board_id) / ticket_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "description.md").write_text("hello\n")
    return ws_dir


def _insert_ticket(settings: Settings, board_id: str, ticket_id: str) -> None:
    """Insert a minimal Ticket row so the orphan scan sees it."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=State.DRAFT,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# find_orphan_workspaces
# ---------------------------------------------------------------------------


def test_no_orphans_when_no_workspaces_dir(tmp_path):
    """Missing ``workspaces/`` dir returns an empty list, no error."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")  # creates the mill.db but no workspaces/
    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_no_orphans_when_workspaces_dir_empty(tmp_path):
    """An existing-but-empty workspaces dir returns []."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    s.workspaces_dir_for("board-x").mkdir(parents=True)
    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_single_orphan_detected(tmp_path):
    """A workspace dir with no matching Ticket row is reported as orphan."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    ticket_id = "20260101T000000Z-old-ticket-ab12"
    ws_dir = _make_workspace_dir(s, "board-x", ticket_id)

    orphans = find_orphan_workspaces(s, "board-x")

    assert len(orphans) == 1
    o = orphans[0]
    assert isinstance(o, OrphanWorkspace)
    assert o.board_id == "board-x"
    assert o.ticket_id == ticket_id
    assert o.path == ws_dir
    assert o.dir_size_bytes > 0
    db.reset_engine()


def test_active_ticket_not_flagged(tmp_path):
    """A workspace dir whose ticket exists in the DB is NOT reported."""
    s = _make_settings(tmp_path)
    ticket_id = "20260101T000000Z-active-ab12"
    _make_workspace_dir(s, "board-x", ticket_id)
    _insert_ticket(s, "board-x", ticket_id)

    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_non_ticket_id_dir_skipped_with_warning(tmp_path, caplog):
    """A non-ticket-shaped subdir is skipped + WARNING logged, never an orphan."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    ws_root = s.workspaces_dir_for("board-x")
    ws_root.mkdir(parents=True)
    (ws_root / "artifacts").mkdir()  # non-ticket-ID dir name
    (ws_root / ".gitkeep").write_text("")  # not a dir — also ignored

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_audit"):
        orphans = find_orphan_workspaces(s, "board-x")

    assert orphans == []
    assert any(
        "artifacts" in rec.message and "non-ticket-ID" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_board_isolation(tmp_path):
    """A workspace dir in board A is matched against A's DB only."""
    s = _make_settings(tmp_path)
    ticket_id = "20260101T000000Z-shared-ab12"

    # Put the workspace dir under board-A; insert the matching ticket
    # ONLY under board-B. From board-A's perspective the ticket is
    # missing — so it must still be reported as orphan.
    _make_workspace_dir(s, "board-a", ticket_id)
    _insert_ticket(s, "board-b", ticket_id)
    # Make sure board-a's DB exists but does NOT contain the ticket.
    db.init_db(s, "board-a")

    orphans_a = find_orphan_workspaces(s, "board-a")
    assert len(orphans_a) == 1
    assert orphans_a[0].board_id == "board-a"
    assert orphans_a[0].ticket_id == ticket_id

    # And board-B has no workspace dir, so it reports no orphans.
    orphans_b = find_orphan_workspaces(s, "board-b")
    assert orphans_b == []
    db.reset_engine()


def test_batch_query_used_for_large_set(tmp_path, monkeypatch):
    """All candidate IDs are queried in batched ``IN`` selects, not
    one query per directory.

    We monkeypatch ``Session.exec`` to count invocations and assert
    the total is well below the candidate count (with default
    batch size 500, 600 candidates yields exactly 2 SELECT calls).
    """
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")

    # 600 orphan dirs — exceeds the 500 batch ceiling.
    for i in range(600):
        _make_workspace_dir(s, "board-x", f"20260101T000000Z-bulk-{i:04d}")

    from sqlmodel import Session

    calls: list[int] = []
    real_exec = Session.exec

    def counting_exec(self, *a, **k):
        calls.append(1)
        return real_exec(self, *a, **k)

    monkeypatch.setattr(Session, "exec", counting_exec)

    orphans = find_orphan_workspaces(s, "board-x")

    assert len(orphans) == 600
    # 600 candidates / 500 batch size = 2 SELECT calls. Anything
    # close to one-query-per-directory would be 600+.
    assert len(calls) == 2
    db.reset_engine()


# ---------------------------------------------------------------------------
# run_data_dir_audit_pass integration
# ---------------------------------------------------------------------------


def test_pass_reports_orphans_per_board_in_summary(tmp_path, monkeypatch):
    """``run_data_dir_audit_pass`` discovers boards from disk, scans
    each, and includes orphan counts (with per-board detail) in the
    summary string."""
    s = _make_settings(tmp_path)

    # Two boards, each with one orphan workspace.
    _make_workspace_dir(s, "board-a", "20260101T000000Z-orph-aa11")
    _make_workspace_dir(s, "board-b", "20260101T000000Z-orph-bb22")
    db.init_db(s, "board-a")
    db.init_db(s, "board-b")

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-1")

    assert result.session_id == "sess-1"
    assert result.drafts_created == []
    assert "orphan workspaces" in result.summary
    assert "2" in result.summary  # total
    assert "board-a=1" in result.summary
    assert "board-b=1" in result.summary
    db.reset_engine()


def test_pass_no_findings_when_clean(tmp_path, monkeypatch):
    """With no orphans the pass returns the legacy ``no findings`` summary."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-clean")

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-clean")
    assert result.summary == "no findings"
    assert result.drafts_created == []
    db.reset_engine()


@pytest.fixture(autouse=True)
def _engine_cleanup():
    """Belt-and-braces: reset the engine cache before AND after each
    test, in case one of the asserts above raises before the inline
    ``reset_engine()`` runs."""
    db.reset_engine()
    yield
    db.reset_engine()

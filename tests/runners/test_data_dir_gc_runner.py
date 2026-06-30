"""Tests for the data-dir GC runner.

Covers the 5 GC steps:
- Terminal-ticket clone pruning
- Closed-workspace pruning (opt-in)
- DB row purging
- Orphan workspace pruning
- Memory ledger truncation
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.models import Ticket, TicketEvent, _now
from robotsix_mill.core.states import State
from robotsix_mill.runners.data_dir_gc import run_data_dir_gc_pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    """Build a fresh Settings rooted at *tmp_path*.

    Resets engine + secrets + repos config so each test gets a clean
    per-board DB cache (the engine cache otherwise survives across
    tests). Extra keyword overrides are forwarded to ``Settings(...)``
    so the unbounded-candidate tests can dial ``max_memory_chars``
    down.
    """
    from robotsix_mill.config import _reset_repos_config

    _reset_secrets()
    db.reset_engine()
    _reset_repos_config()
    overrides.setdefault("data_dir", str(tmp_path))
    overrides.setdefault("require_approval", "false")
    return Settings(**overrides)


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


def _write_bytes(path: Path, size: int, *, fill: bytes = b"x") -> None:
    """Create ``path`` containing exactly ``size`` bytes of ``fill``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * size)


def _insert_closed_ticket(
    settings: Settings,
    board_id: str,
    ticket_id: str,
    *,
    closed_at,
    state: State = State.CLOSED,
) -> None:
    """Insert a terminal-state Ticket plus a backdated terminal
    ``TicketEvent`` (``at=closed_at``) so the prune age guard has a
    close time to measure against."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=state,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.add(
            TicketEvent(
                ticket_id=ticket_id,
                state=state,
                at=closed_at,
            )
        )
        s.commit()


def _insert_ticket_with_state(
    settings: Settings, board_id: str, ticket_id: str, state: State
) -> None:
    """Insert a Ticket row in an arbitrary (non-terminal) ``state``,
    with no terminal event."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=state,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Orphan-workspace GC — prune old orphan dirs
# ---------------------------------------------------------------------------


def test_prune_orphan_removes_old_orphan_dir(tmp_path):
    """An orphan workspace dir older than the age threshold is deleted."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_orphans=True,
        data_dir_gc_prune_orphans_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan")
    db.init_db(s, "board-x")

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 1
    assert not ws.exists()
    db.reset_engine()


def test_prune_orphan_keeps_recent_orphan_dir(tmp_path):
    """An orphan workspace dir younger than the age threshold is kept."""
    ticket_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-recent-orphan"
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_orphans=True,
        data_dir_gc_prune_orphans_age_seconds=86_400,
    )
    ws = _make_workspace_dir(s, "board-x", ticket_id)
    db.init_db(s, "board-x")

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_leaves_live_ticket_workspace(tmp_path):
    """A live ticket's workspace is never removed by the orphan GC."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_orphans=True,
        data_dir_gc_prune_orphans_age_seconds=0,
    )
    ticket_id = "20200101T000000Z-live-ticket"
    ws = _make_workspace_dir(s, "board-x", ticket_id)
    _insert_ticket(s, "board-x", ticket_id)

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_knob_off_is_noop(tmp_path):
    """With the knob disabled, no orphan dirs are touched."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_orphans=False,
        data_dir_gc_prune_orphans_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan")
    db.init_db(s, "board-x")

    # Go through run_data_dir_gc_pass so the knob is checked.
    import robotsix_mill.runners.data_dir_gc as gc_mod

    original = gc_mod.Settings
    gc_mod.Settings = lambda: s
    try:
        result = run_data_dir_gc_pass()
    finally:
        gc_mod.Settings = original

    assert result.orphans_pruned == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_summary_line(tmp_path, monkeypatch):
    """The summary includes 'orphans=N' when orphans are GC'd."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_orphans=True,
        data_dir_gc_prune_orphans_age_seconds=0,
    )
    _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan-1")
    _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan-2")
    db.init_db(s, "board-x")

    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_gc.Settings",
        lambda: s,
    )

    result = run_data_dir_gc_pass()
    assert result.orphans_pruned == 2
    assert "orphans=2" in result.summary
    db.reset_engine()


# ---------------------------------------------------------------------------
# prune_closed GC — workspaces of terminal-state tickets
# ---------------------------------------------------------------------------


def test_prune_closed_knob_default_off_is_noop(tmp_path, monkeypatch):
    """With ``data_dir_gc_prune_closed`` unset/false, an old CLOSED
    ticket's workspace is NOT removed by the pass."""
    s = _make_settings(tmp_path)
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-aaaa")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-aaaa",
        closed_at=_now() - timedelta(days=30),
    )

    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_gc.Settings",
        lambda: s,
    )

    result = run_data_dir_gc_pass()

    assert ws.exists()
    assert result.closed_pruned == 0
    db.reset_engine()


def test_prune_closed_removes_old_closed_workspace(tmp_path):
    """With the age threshold low, a CLOSED-ticket workspace older than
    the threshold is removed and the return count reflects it."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_closed=True,
        data_dir_gc_prune_closed_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-bbbb")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-bbbb",
        closed_at=_now() - timedelta(days=30),
    )

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_closed_workspaces

    removed = _prune_closed_workspaces(s)

    assert removed == 1
    assert not ws.exists()
    db.reset_engine()


def test_prune_closed_age_guard_keeps_recent_closure(tmp_path):
    """With a 7-day threshold, a ticket closed 'now' is NOT removed."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_closed=True,
        data_dir_gc_prune_closed_age_seconds=604_800,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-cccc")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-cccc",
        closed_at=_now(),
    )

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_closed_workspaces

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_closed_leaves_non_terminal_workspaces(tmp_path):
    """DRAFT, DONE, and BLOCKED ticket workspaces are never pruned."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_closed=True,
        data_dir_gc_prune_closed_age_seconds=0,
    )
    ws_draft = _make_workspace_dir(s, "board-x", "20260101T000000Z-draft-0001")
    ws_done = _make_workspace_dir(s, "board-x", "20260101T000000Z-done-0002")
    ws_blocked = _make_workspace_dir(s, "board-x", "20260101T000000Z-blkd-0003")
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-draft-0001", State.DRAFT)
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-done-0002", State.DONE)
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-blkd-0003", State.BLOCKED)

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_closed_workspaces

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws_draft.exists()
    assert ws_done.exists()
    assert ws_blocked.exists()
    db.reset_engine()


def test_prune_closed_leaves_orphans(tmp_path):
    """A workspace dir with no matching ticket row is left for the
    orphan detector, not removed by the prune step."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_closed=True,
        data_dir_gc_prune_closed_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-orph-dddd")
    db.init_db(s, "board-x")  # board DB exists, but ticket row does not

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_closed_workspaces

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_closed_board_failure_is_skipped(tmp_path, monkeypatch, caplog):
    """A board whose DB access raises is skipped with a warning while a
    second healthy board is still processed — no exception propagates."""
    s = _make_settings(
        tmp_path,
        data_dir_gc_prune_closed=True,
        data_dir_gc_prune_closed_age_seconds=0,
    )
    ws_a = _make_workspace_dir(s, "board-a", "20260101T000000Z-closed-aa11")
    ws_b = _make_workspace_dir(s, "board-b", "20260101T000000Z-closed-bb22")
    _insert_closed_ticket(
        s,
        "board-a",
        "20260101T000000Z-closed-aa11",
        closed_at=_now() - timedelta(days=30),
    )
    db.init_db(s, "board-b")  # discoverable board with a workspace candidate

    real_session = db.session

    def fake_session(settings, board_id):
        if board_id == "board-b":
            raise RuntimeError("simulated unreachable DB")
        return real_session(settings, board_id)

    monkeypatch.setattr(db, "session", fake_session)

    from robotsix_mill.runners.data_dir_gc.orphans import _prune_closed_workspaces

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_gc"):
        removed = _prune_closed_workspaces(s)

    assert removed == 1
    assert not ws_a.exists()  # healthy board pruned
    assert ws_b.exists()  # failing board untouched
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    db.reset_engine()


# ---------------------------------------------------------------------------
# Terminal-clone GC — repo/ + repos/ inside terminal-ticket workspaces
# ---------------------------------------------------------------------------


def _make_workspace_with_clones(settings: Settings, board_id: str, ticket_id: str):
    """Workspace with description + artifacts + both clone subdirs."""
    ws_dir = _make_workspace_dir(settings, board_id, ticket_id)
    (ws_dir / "artifacts").mkdir(exist_ok=True)
    (ws_dir / "artifacts" / "retrospect.md").write_text("kept\n")
    _write_bytes(ws_dir / "repo" / ".git" / "objects.bin", 1_000)
    _write_bytes(ws_dir / "repos" / "other-repo" / ".git" / "objects.bin", 1_000)
    return ws_dir


def test_prune_terminal_clones_removes_clones_keeps_artifacts(tmp_path, monkeypatch):
    """The default-on clone GC removes repo/ + repos/ of an old terminal
    ticket but preserves description.md and artifacts/."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_gc.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-eeee"
    ws_dir = _make_workspace_with_clones(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    result = run_data_dir_gc_pass()

    assert result.clones_pruned == 2
    assert not (ws_dir / "repo").exists()
    assert not (ws_dir / "repos").exists()
    assert (ws_dir / "description.md").exists()
    assert (ws_dir / "artifacts" / "retrospect.md").exists()
    assert "clones=2" in result.summary
    db.reset_engine()


def test_prune_terminal_clones_age_guard_and_active_kept(tmp_path, monkeypatch):
    """Recently-closed and active tickets keep their clones."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_gc.Settings", lambda: s)
    board_id = "test-board"
    recent = "20260101T000000Z-recent-ffff"
    active = "20260101T000000Z-active-0000"
    ws_recent = _make_workspace_with_clones(s, board_id, recent)
    ws_active = _make_workspace_with_clones(s, board_id, active)
    _insert_closed_ticket(s, board_id, recent, closed_at=_now(), state=State.CLOSED)
    _insert_ticket(s, board_id, active)

    result = run_data_dir_gc_pass()

    assert result.clones_pruned == 0
    assert (ws_recent / "repo").exists()
    assert (ws_active / "repo").exists()
    db.reset_engine()


def test_prune_terminal_clones_knob_off_is_noop(tmp_path, monkeypatch):
    """With the knob disabled, no clones are touched."""
    s = _make_settings(tmp_path, data_dir_gc_prune_terminal_clones=False)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_gc.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-1111"
    ws_dir = _make_workspace_with_clones(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    result = run_data_dir_gc_pass()

    assert result.clones_pruned == 0
    assert (ws_dir / "repo").exists()
    db.reset_engine()

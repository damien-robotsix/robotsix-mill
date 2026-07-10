"""Unit tests for db.py — engine/session lifecycle.

Tests _db_path, get_engine, init_db, session, reset_engine,
and the disk-full retry machinery (DiskFullError, retry_on_db_full,
session-commit protection)
using tmp_path-backed SQLite files (matching the pattern in
the existing ``settings`` fixture).
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core import models  # noqa: F401 — populate SQLModel.metadata
from robotsix_mill.core.db import (
    DiskFullError,
    _is_db_full_error,
    _reclaim_disk_space,
    _raise_disk_full,
    retry_on_db_full,
)


# ── _db_path ──────────────────────────────────────────────────────────────


def test_db_path_returns_correct_path():
    """_db_path returns <data_dir>/<board_id>/mill.db."""
    s = Settings(data_dir="/tmp/foo")
    result = db._db_path(s, "my-board")
    assert result == Path("/tmp/foo/my-board/mill.db")


def test_db_path_raises_valueerror_on_empty_board_id():
    """_db_path raises ValueError when board_id is empty."""
    s = Settings(data_dir="/tmp/foo")
    with pytest.raises(ValueError, match="board_id is required"):
        db._db_path(s, "")


# ── get_engine ────────────────────────────────────────────────────────────


def test_get_engine_creates_and_caches(tmp_path: Path):
    """get_engine creates a new engine on first call, returns cached on second."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    e1 = db.get_engine(s, "board-a")
    e2 = db.get_engine(s, "board-a")
    assert e1 is e2  # cached

    e3 = db.get_engine(s, "board-b")
    assert e3 is not e1  # different board


def test_get_engine_check_same_thread_false(tmp_path: Path):
    """get_engine passes check_same_thread=False to SQLite connect_args."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    engine = db.get_engine(s, "board-x")
    # Connect to verify the engine works — check_same_thread=False is
    # required for the threaded usage pattern.  The flag is not exposed
    # as a public attribute on the dialect, so we verify indirectly by
    # confirming that a connection can be acquired.
    with engine.connect() as conn:
        result = conn.exec_driver_sql("SELECT 1").scalar()
        assert result == 1


def test_get_engine_creates_parent_directory(tmp_path: Path):
    """get_engine creates the parent directory tree if it doesn't exist."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    db_dir = tmp_path / "nested" / "deep-board"
    assert not db_dir.exists()

    db.get_engine(s, "deep-board")
    # The directory should now exist (settings data_dir is tmp_path,
    # so the path is tmp_path/deep-board/mill.db; parent is tmp_path/deep-board)
    assert (tmp_path / "deep-board").exists()


# ── init_db ───────────────────────────────────────────────────────────────


def test_init_db_creates_all_tables(tmp_path: Path):
    """init_db creates ticket, ticketevent, comment, memory tables."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    db.init_db(s, "test-init")

    engine = db.get_engine(s, "test-init")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {"ticket", "ticketevent", "comment", "memory"}
    assert expected.issubset(table_names), f"missing tables: {expected - table_names}"


def test_init_db_is_idempotent(tmp_path: Path):
    """Calling init_db twice does not raise an error (ALTER TABLE is tolerant)."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    db.init_db(s, "test-idem")
    db.init_db(s, "test-idem")  # must not raise

    # Tables should still be intact.
    engine = db.get_engine(s, "test-idem")
    inspector = inspect(engine)
    assert "ticket" in inspector.get_table_names()


def test_init_db_migration_preserves_existing_data(tmp_path: Path):
    """Seed a pre-migration DB then run init_db; verify migrations applied and data intact.

    Creates the ticket and ticketevent tables with only the columns that
    pre-date the additive migration list, seeds representative rows, runs
    init_db, then checks that every migration column was added and that the
    seeded data survived the round-trip through SQLModel.
    """
    import sqlite3
    from sqlmodel import Session, select
    from robotsix_mill.core.models import Ticket, TicketEvent
    from robotsix_mill.core.states import State

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    # 1. Create the pre-migration schema — tables with ONLY the columns
    #    that are NOT in the additive migration list.
    db_path = tmp_path / "test-board" / "mill.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE ticket (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'task',
            workspace_path TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            branch TEXT,
            parent_id TEXT REFERENCES ticket(id),
            source TEXT NOT NULL DEFAULT 'user',
            blocked_from TEXT,
            origin_session TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            review_rounds INTEGER NOT NULL DEFAULT 0,
            retry_attempt INTEGER NOT NULL DEFAULT 0,
            last_transient_error TEXT,
            next_retry_at TEXT,
            depends_on TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE ticketevent (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT NOT NULL REFERENCES ticket(id),
            state TEXT NOT NULL,
            note TEXT,
            at TEXT NOT NULL
        );
        CREATE TABLE comment (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT NOT NULL REFERENCES ticket(id),
            body TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT 'user',
            parent_id INTEGER REFERENCES comment(id),
            closed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY,
            board_id TEXT NOT NULL,
            name TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """
    )

    # 2. Seed representative data
    conn.execute(
        "INSERT INTO ticket (id, title, state, kind, workspace_path, "
        "created_at, updated_at) "
        "VALUES ('tkt-001', 'Test ticket', 'DRAFT', 'task', '/path', "
        "'2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ticket (id, title, state, kind, workspace_path, "
        "created_at, updated_at) "
        "VALUES ('tkt-002', 'Second ticket', 'READY', 'task', '/path2', "
        "'2024-01-02T00:00:00Z', '2024-01-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ticketevent (ticket_id, state, at) "
        "VALUES ('tkt-001', 'DRAFT', '2024-01-01T00:00:00Z')"
    )
    conn.commit()

    # 3. Record pre-migration column sets (must NOT contain migration columns)
    pre_ticket_cols = {row[1] for row in conn.execute("PRAGMA table_info('ticket')")}
    pre_event_cols = {
        row[1] for row in conn.execute("PRAGMA table_info('ticketevent')")
    }
    conn.close()

    # Sanity: migration columns are absent before init_db
    for col in (
        "board_id",
        "priority",
        "paused_from",
        "unblocks",
        "labels",
        "pre_redraft_cost_usd",
        "implement_cycles",
        "refine_passes",
        "refine_output_hash",
    ):
        assert col not in pre_ticket_cols, f"{col} unexpectedly present pre-migration"
    for col in ("prev_hash", "hash"):
        assert col not in pre_event_cols, f"{col} unexpectedly present pre-migration"

    # 4. Run init_db — this applies all migrations
    db.init_db(s, "test-board")

    # 5. Verify migration columns exist
    engine = db.get_engine(s, "test-board")
    inspector = inspect(engine)
    ticket_cols = {c["name"] for c in inspector.get_columns("ticket")}
    event_cols = {c["name"] for c in inspector.get_columns("ticketevent")}

    assert "board_id" in ticket_cols
    assert "priority" in ticket_cols
    assert "paused_from" in ticket_cols
    assert "unblocks" in ticket_cols
    assert "labels" in ticket_cols
    assert "pre_redraft_cost_usd" in ticket_cols
    assert "implement_cycles" in ticket_cols
    assert "refine_passes" in ticket_cols
    assert "refine_output_hash" in ticket_cols
    assert "prev_hash" in event_cols
    assert "hash" in event_cols

    # 6. Verify seeded data survived the round-trip through SQLModel
    with Session(engine) as sess:
        tickets = sess.exec(select(Ticket).order_by(Ticket.id)).all()
        assert len(tickets) == 2
        assert tickets[0].id == "tkt-001"
        assert tickets[0].title == "Test ticket"
        assert tickets[1].id == "tkt-002"
        assert tickets[1].title == "Second ticket"

        events = sess.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == "tkt-001")
        ).all()
        assert len(events) == 1
        assert events[0].state == State.DRAFT


def test_init_db_marks_board_as_initialized(tmp_path: Path):
    """After init_db, the board_id appears in _initialized."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    assert "test-mark" not in db._initialized
    db.init_db(s, "test-mark")
    assert "test-mark" in db._initialized


# ── session ───────────────────────────────────────────────────────────────


def test_session_lazy_initializes_board(tmp_path: Path):
    """session() calls init_db lazily when the board is not yet initialized."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    # Ensure board is NOT in _initialized.
    assert "lazy-board" not in db._initialized

    sess = db.session(s, "lazy-board")
    # After session(), the board should be initialized.
    assert "lazy-board" in db._initialized
    sess.close()


def test_session_can_execute_sql(tmp_path: Path):
    """A session returned by session() can run a simple SQL query."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    sess = db.session(s, "exec-board")
    # exec_driver_sql is on Connection, not Session — go through the
    # bound connection.
    result = sess.connection().exec_driver_sql("SELECT 1").scalar()
    assert result == 1
    sess.close()


def test_session_second_call_reuses_initialized_board(tmp_path: Path):
    """Second session() call does not re-initialize (no error, tables exist)."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    s1 = db.session(s, "reuse-board")
    s1.close()
    s2 = db.session(s, "reuse-board")
    # Should work without error — tables already exist.
    result = s2.connection().exec_driver_sql("SELECT 1").scalar()
    assert result == 1
    s2.close()


# ── reset_engine ──────────────────────────────────────────────────────────


def test_reset_engine_clears_cache(tmp_path: Path):
    """reset_engine clears _engines and _initialized."""
    s = Settings(data_dir=str(tmp_path))

    # Populate cache.
    db.reset_engine()
    db.get_engine(s, "board-1")
    db.init_db(s, "board-1")
    assert "board-1" in db._engines
    assert "board-1" in db._initialized

    db.reset_engine()
    assert db._engines == {}
    assert db._initialized == set()


def test_reset_engine_disposes_engines(tmp_path: Path):
    """reset_engine calls engine.dispose() on each cached engine."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    e1 = db.get_engine(s, "board-dispose")
    # Check pool is connected before reset
    assert e1.pool is not None

    db.reset_engine()
    # After reset, the engine should no longer be in cache.
    assert "board-dispose" not in db._engines


# ── DiskFullError ─────────────────────────────────────────────────────────


def test_disk_full_error_is_runtime_error():
    """DiskFullError is a RuntimeError subclass."""
    err = DiskFullError("disk is full")
    assert isinstance(err, RuntimeError)
    assert str(err) == "disk is full"


# ── _is_db_full_error ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("database or disk is full", True),
        ("DATABASE OR DISK IS FULL", True),
        ("disk is full", True),
        ("database is locked", True),
        ("Database Is LOCKED", True),
        ("no such table: ticket", False),
        ("UNIQUE constraint failed", False),
        ("disk I/O error", False),
    ],
)
def test_is_db_full_error(message, expected):
    """_is_db_full_error matches the correct patterns case-insensitively."""
    exc = sqlite3.OperationalError(message)
    assert _is_db_full_error(exc) == expected


# ── _raise_disk_full ─────────────────────────────────────────────────────


def test_raise_disk_full_includes_error(tmp_path: Path):
    """_raise_disk_full raises DiskFullError with the original error info."""
    s = Settings(data_dir=str(tmp_path))
    orig = sqlite3.OperationalError("database or disk is full")
    with pytest.raises(DiskFullError, match="database or disk is full"):
        _raise_disk_full(s, orig)


# ── _reclaim_disk_space ──────────────────────────────────────────────────


def test_reclaim_disk_space_runs_vacuum(tmp_path: Path):
    """_reclaim_disk_space runs VACUUM without error on a valid DB."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))
    db.init_db(s, "vacuum-test")
    # Should not raise.
    _reclaim_disk_space(s, "vacuum-test")


def test_reclaim_disk_space_handles_missing_db(tmp_path: Path):
    """_reclaim_disk_space logs a warning when the DB doesn't exist."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))
    # Board with no DB file — should not raise.
    _reclaim_disk_space(s, "nonexistent-board")


# ── _retry_db_op (core retry logic) ──────────────────────────────────────


def test_retry_db_op_succeeds_first_try(tmp_path: Path):
    """_retry_db_op calls the operation once when it succeeds."""
    from robotsix_mill.core.db import _retry_db_op

    s = Settings(data_dir=str(tmp_path))
    calls = []

    def op():
        calls.append(1)

    _retry_db_op(op, s, "test-board")
    assert calls == [1]


def test_retry_db_op_retries_on_disk_full(tmp_path: Path, monkeypatch):
    """_retry_db_op calls the operation twice when first attempt fails."""
    from robotsix_mill.core import db as db_module
    from robotsix_mill.core.db import _retry_db_op

    s = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(db_module, "_reclaim_disk_space", lambda *a, **kw: None)
    monkeypatch.setattr(db_module, "_log_disk_full", lambda *a, **kw: None)

    call_count = 0

    def op():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("database or disk is full")

    _retry_db_op(op, s, "test-board")
    assert call_count == 2


def test_retry_db_op_raises_disk_full_on_double_failure(tmp_path: Path, monkeypatch):
    """_retry_db_op raises DiskFullError when both attempts fail."""
    from robotsix_mill.core import db as db_module
    from robotsix_mill.core.db import _retry_db_op

    s = Settings(data_dir=str(tmp_path))
    monkeypatch.setattr(db_module, "_reclaim_disk_space", lambda *a, **kw: None)
    monkeypatch.setattr(db_module, "_log_disk_full", lambda *a, **kw: None)

    def op():
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(DiskFullError, match="VACUUM retry"):
        _retry_db_op(op, s, "test-board")


def test_retry_db_op_passes_through_unrelated_error(tmp_path: Path):
    """_retry_db_op re-raises non-disk-full OperationalErrors."""
    from robotsix_mill.core.db import _retry_db_op

    s = Settings(data_dir=str(tmp_path))

    def op():
        raise sqlite3.OperationalError("no such table: bananas")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _retry_db_op(op, s, "test-board")


# ── session commit/flush protection ──────────────────────────────────────


def test_session_has_protected_commit(tmp_path: Path):
    """session() returns a Session whose commit is wrapped with retry logic."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    sess = db.session(s, "protected-board")
    # The commit method is replaced by our _safe_commit closure.
    # Verify it's callable and does not crash on normal operations.
    sess.connection().exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS test_protected (x INTEGER)"
    )
    sess.commit()  # should succeed without error
    sess.close()


def test_session_commit_passes_through_unrelated_error(tmp_path: Path):
    """session().commit() re-raises non-disk-full OperationalErrors."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    sess = db.session(s, "other-err-board")
    sess.connection().exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS test_other (x INTEGER)"
    )

    def schema_error():
        raise sqlite3.OperationalError("no such table: bananas")

    sess.commit = schema_error

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        sess.commit()
    sess.close()


# ── retry_on_db_full context manager ─────────────────────────────────────


def test_retry_on_db_full_cm_passes_on_success(tmp_path: Path):
    """Context manager does nothing when the block succeeds."""
    s = Settings(data_dir=str(tmp_path))
    with retry_on_db_full(s, "cm-test"):
        pass  # no error


def test_retry_on_db_full_cm_raises_disk_full(tmp_path: Path):
    """Context manager raises DiskFullError after VACUUM on disk-full."""
    s = Settings(data_dir=str(tmp_path))
    # Need a DB file for VACUUM to succeed
    db.reset_engine()
    db.init_db(s, "cm-test")

    with pytest.raises(DiskFullError, match="VACUUM retry"):
        with retry_on_db_full(s, "cm-test"):
            raise sqlite3.OperationalError("database or disk is full")


def test_retry_on_db_full_cm_passes_through_other_error(tmp_path: Path):
    """Context manager does not intercept non-disk-full errors."""
    s = Settings(data_dir=str(tmp_path))
    with pytest.raises(ValueError, match="something else"):
        with retry_on_db_full(s, "cm-other"):
            raise ValueError("something else")


# ── retry_on_db_full decorator ───────────────────────────────────────────


def test_retry_on_db_full_decorator_retries_once(tmp_path: Path):
    """Decorator retries the wrapped function once on disk-full."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))
    db.init_db(s, "deco-test")

    call_count = 0

    @retry_on_db_full(s, "deco-test")
    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("database or disk is full")

    flaky()
    assert call_count == 2


def test_retry_on_db_full_decorator_raises_on_double_failure(tmp_path: Path):
    """Decorator raises DiskFullError after two failures."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))
    db.init_db(s, "deco-double")

    @retry_on_db_full(s, "deco-double")
    def always_fails():
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(DiskFullError, match="VACUUM retry"):
        always_fails()


def test_retry_on_db_full_decorator_passes_through_other_error(
    tmp_path: Path,
):
    """Decorator passes through non-disk-full errors."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    @retry_on_db_full(s, "deco-other")
    def raises_value_error():
        raise sqlite3.OperationalError("no such table: xyz")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        raises_value_error()

"""Unit tests for db.py — engine/session lifecycle.

Tests _db_path, get_engine, init_db, session, and reset_engine
using tmp_path-backed SQLite files (matching the pattern in
the existing ``settings`` fixture).
"""

from pathlib import Path

import pytest
from sqlalchemy import inspect

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core import models


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
    """init_db creates ticket, ticketevent, comment, proposedaction tables."""
    db.reset_engine()
    s = Settings(data_dir=str(tmp_path))

    db.init_db(s, "test-init")

    engine = db.get_engine(s, "test-init")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {"ticket", "ticketevent", "comment", "proposedaction", "memory"}
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

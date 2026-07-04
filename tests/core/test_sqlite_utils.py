"""Tests for ``robotsix_mill.core.sqlite_utils`` — additive SQLite
column migration helpers."""

import sqlite3

import pytest
import sqlalchemy as sa

from robotsix_mill.core.sqlite_utils import (
    add_column_if_missing,
    run_additive_migrations,
    _exec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite3_conn():
    """In-memory sqlite3 connection with a pre-created ``t`` table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    return conn


@pytest.fixture
def sa_conn():
    """In-memory SQLAlchemy connection with a pre-created ``t`` table."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        conn.commit()
        yield conn


# ---------------------------------------------------------------------------
# _exec
# ---------------------------------------------------------------------------


def test_exec_raw_sqlite3(sqlite3_conn):
    """_exec creates a table via raw sqlite3.Connection."""
    _exec(sqlite3_conn, "CREATE TABLE u (x INTEGER)")
    rows = list(
        sqlite3_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='u'"
        )
    )
    assert len(rows) == 1


def test_exec_sa_connection(sa_conn):
    """_exec creates a table via SQLAlchemy Connection."""
    _exec(sa_conn, "CREATE TABLE u (x INTEGER)")
    sa_conn.commit()
    result = sa_conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='u'")
    )
    rows = result.fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# add_column_if_missing
# ---------------------------------------------------------------------------


def test_add_column_returns_true(sqlite3_conn):
    """First ADD COLUMN returns True."""
    result = add_column_if_missing(sqlite3_conn, "t", "name TEXT")
    assert result is True

    cols = [row[1] for row in sqlite3_conn.execute("PRAGMA table_info('t')")]
    assert "name" in cols


def test_add_existing_column_returns_false(sqlite3_conn):
    """Re-adding the same column returns False."""
    add_column_if_missing(sqlite3_conn, "t", "name TEXT")
    result = add_column_if_missing(sqlite3_conn, "t", "name TEXT")
    assert result is False


def test_add_column_sa_connection(sa_conn):
    """add_column_if_missing works with a SQLAlchemy connection."""
    result = add_column_if_missing(sa_conn, "t", "label TEXT NOT NULL DEFAULT ''")
    assert result is True

    result2 = add_column_if_missing(sa_conn, "t", "label TEXT NOT NULL DEFAULT ''")
    assert result2 is False


# ---------------------------------------------------------------------------
# run_additive_migrations
# ---------------------------------------------------------------------------


def test_run_additive_migrations_batch(sqlite3_conn):
    """Batch of 3 migrations: first two new, third already exists."""
    add_column_if_missing(sqlite3_conn, "t", "c3 TEXT")

    results = run_additive_migrations(
        sqlite3_conn, "t", ["c1 TEXT", "c2 INTEGER", "c3 TEXT"]
    )
    assert results == [True, True, False]

    cols = [row[1] for row in sqlite3_conn.execute("PRAGMA table_info('t')")]
    assert "c1" in cols
    assert "c2" in cols
    assert "c3" in cols


def test_run_additive_migrations_all_new(sqlite3_conn):
    """All migrations are new → all True."""
    results = run_additive_migrations(sqlite3_conn, "t", ["a TEXT", "b TEXT"])
    assert results == [True, True]


def test_run_additive_migrations_empty(sqlite3_conn):
    """Empty migration list returns empty list."""
    results = run_additive_migrations(sqlite3_conn, "t", [])
    assert results == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_nonexistent_table_raises(sqlite3_conn):
    """PRAGMA on a nonexistent table returns empty → ALTER TABLE raises."""
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        add_column_if_missing(sqlite3_conn, "nonexistent", "x TEXT")


def test_nonexistent_table_raises_sa(sa_conn):
    """PRAGMA on a nonexistent table via SA returns empty → ALTER raises."""
    with pytest.raises(sa.exc.OperationalError):
        add_column_if_missing(sa_conn, "nonexistent", "x TEXT")


def test_invalid_column_def_raises(sqlite3_conn):
    """Malformed column definition propagates OperationalError."""
    with pytest.raises(sqlite3.OperationalError):
        add_column_if_missing(sqlite3_conn, "t", "123 INVALID")

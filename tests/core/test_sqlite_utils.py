"""Tests for ``robotsix_mill.core.sqlite_utils`` — additive SQLite
column migration helpers."""

import sqlite3

import pytest
import sqlalchemy as sa

from robotsix_mill.core.sqlite_utils import (
    _execute_sql,
    add_column_if_missing,
    run_additive_migrations,
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
# _execute_sql
# ---------------------------------------------------------------------------


def test_execute_sql_raw_sqlite3(sqlite3_conn):
    """_execute_sql creates a table via raw sqlite3.Connection."""
    _execute_sql(sqlite3_conn, "CREATE TABLE u (x INTEGER)")
    rows = list(
        sqlite3_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='u'"
        )
    )
    assert len(rows) == 1


def test_execute_sql_sa_connection(sa_conn):
    """_execute_sql creates a table via SQLAlchemy Connection."""
    _execute_sql(sa_conn, "CREATE TABLE u (x INTEGER)")
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

    # Verify column actually exists
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
    # Pre-add the third column so it already exists
    add_column_if_missing(sqlite3_conn, "t", "c3 TEXT")

    migrations: list[tuple[str, str]] = [
        ("t", "c1 TEXT"),
        ("t", "c2 INTEGER"),
        ("t", "c3 TEXT"),
    ]
    results = run_additive_migrations(sqlite3_conn, migrations)
    assert results == [True, True, False]

    # Verify columns exist
    cols = [row[1] for row in sqlite3_conn.execute("PRAGMA table_info('t')")]
    assert "c1" in cols
    assert "c2" in cols
    assert "c3" in cols


def test_run_additive_migrations_all_new(sqlite3_conn):
    """All migrations are new → all True."""
    migrations: list[tuple[str, str]] = [
        ("t", "a TEXT"),
        ("t", "b TEXT"),
    ]
    results = run_additive_migrations(sqlite3_conn, migrations)
    assert results == [True, True]


def test_run_additive_migrations_empty(sqlite3_conn):
    """Empty migration list returns empty list."""
    results = run_additive_migrations(sqlite3_conn, [])
    assert results == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_nonexistent_table_raises(sqlite3_conn):
    """ALTER TABLE on a nonexistent table propagates OperationalError."""
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        add_column_if_missing(sqlite3_conn, "nonexistent", "x TEXT")


def test_nonexistent_table_raises_sa(sa_conn):
    """ALTER TABLE on a nonexistent table via SA propagates OperationalError."""
    with pytest.raises(sa.exc.OperationalError):
        add_column_if_missing(sa_conn, "nonexistent", "x TEXT")


def test_invalid_column_def_raises(sqlite3_conn):
    """Malformed column definition propagates OperationalError (not silent False)."""
    with pytest.raises(sqlite3.OperationalError):
        add_column_if_missing(sqlite3_conn, "t", "123 INVALID")

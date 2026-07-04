"""Additive SQLite column migrations — shared helpers.

.. deprecated::
    This module is **deprecated** as of the Alembic migration
    adoption (``alembic/``).  ``db.py`` no longer calls
    ``run_additive_migrations`` — Alembic handles all schema
    changes via versioned migration files.  This file is kept
    temporarily for reference; it will be removed once all
    deployments have transitioned to Alembic-tracked databases.

Historical context (kept for archaeology)
-----------------------------------------
These functions mirrored the intent of
``robotsix_llmio.core.sqlite_utils`` but the llmio API was NOT a
drop-in replacement as of llmio pin ``3da3c4317f4a``:

* ``run_additive_migrations`` takes ``(table, column_ddls)`` in llmio
  vs ``(list[tuple[str, str]])`` here — single-table only, different
  signature.
* llmio uses ``conn.execute(str)`` which works with raw
  ``sqlite3.Connection`` but raises ``ObjectNotExecutableError`` on
  SQLAlchemy ≥2.0 ``Connection``.  Mill passed a SA connection from
  ``engine.begin()``, so llmio's version could not be used as-is.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

from sqlalchemy import exc as sa_exc


class _ExecutesSQL(Protocol):
    """Structural protocol matching both ``sqlite3.Connection`` and
    SQLAlchemy ``Connection`` — anything that can execute raw SQL."""

    def execute(self, sql: str) -> object: ...

    def exec_driver_sql(self, sql: str) -> object: ...


def _execute_sql(conn: _ExecutesSQL, sql: str) -> None:
    """Execute raw SQL on *conn*, using ``exec_driver_sql`` (SQLAlchemy)
    or ``execute`` (raw sqlite3) as appropriate."""
    runner = getattr(conn, "exec_driver_sql", None)
    if runner is None:
        runner = conn.execute
    runner(sql)


def add_column_if_missing(conn: _ExecutesSQL, table: str, column_def: str) -> bool:
    """Add a column to a SQLite table if it does not already exist.

    Args:
        conn: A ``sqlite3.Connection`` or SQLAlchemy ``Connection``.
        table: The table name.
        column_def: The full column definition clause, e.g.
            ``"board_id TEXT NOT NULL DEFAULT ''"``.

    Returns:
        ``True`` if the column was newly added, ``False`` if it already
        existed (the ``ALTER TABLE`` raised ``sqlite3.OperationalError``,
        which typically means "duplicate column name").
    """
    sql = f"ALTER TABLE {table} ADD COLUMN {column_def}"
    try:
        _execute_sql(conn, sql)
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column" in str(exc):
            return False
        raise
    except sa_exc.OperationalError as exc:
        if "duplicate column" in str(exc.orig if exc.orig else exc):
            return False
        raise


def run_additive_migrations(
    conn: _ExecutesSQL,
    migrations: list[tuple[str, str]],
) -> list[bool]:
    """Run a batch of additive column migrations on *conn*.

    Args:
        conn: A ``sqlite3.Connection`` or SQLAlchemy ``Connection``.
        migrations: A list of ``(table, column_def)`` tuples.

    Returns:
        A list of ``bool`` values — one per migration — where ``True``
        means the column was newly added and ``False`` means it already
        existed.
    """
    return [
        add_column_if_missing(conn, table, col_def) for table, col_def in migrations
    ]

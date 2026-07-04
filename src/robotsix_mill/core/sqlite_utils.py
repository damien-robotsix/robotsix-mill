"""Additive SQLite column migration utilities.

.. deprecated::
    This module is **deprecated** as of the Alembic migration
    adoption (``alembic/``).  ``db.py`` no longer calls
    ``run_additive_migrations`` — Alembic handles all schema
    changes via versioned migration files.  This file is kept
    temporarily for reference; it will be removed once all
    deployments have transitioned to Alembic-tracked databases.

Works with both :class:`sqlite3.Connection` and SQLAlchemy
:class:`~sqlalchemy.engine.Connection` — routes raw SQL through
``exec_driver_sql`` for SA connections (which reject bare strings in
``.execute()``) and through ``.execute()`` for raw sqlite3 connections.

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

from collections.abc import Sequence
from typing import Any


def _exec(conn: Any, sql: str) -> Any:
    """Execute raw *sql* against *conn*.

    Routes through ``exec_driver_sql`` when available (SQLAlchemy
    Connection), otherwise falls back to ``.execute()`` (raw sqlite3).
    """
    runner = getattr(conn, "exec_driver_sql", None)
    if runner is None:
        runner = conn.execute
    return runner(sql)


def add_column_if_missing(conn: Any, table: str, column_ddl: str) -> bool:
    """Add *column_ddl* to *table* when the column is not already present.

    Returns ``True`` when the column was newly created, ``False`` when it
    already existed.
    """
    column_name = column_ddl.strip().split(maxsplit=1)[0].strip('"').strip()
    rows = _exec(conn, f"PRAGMA table_info({table})").fetchall()
    existing = {row[1] for row in rows}
    if column_name in existing:
        return False
    _exec(conn, f"ALTER TABLE {table} ADD COLUMN {column_ddl}")
    conn.commit()
    return True


def run_additive_migrations(
    conn: Any,
    table: str,
    column_ddls: Sequence[str],
) -> list[bool]:
    """Apply every DDL in *column_ddls* to *table*.

    Returns a ``list[bool]`` parallel to *column_ddls*: ``True`` when the
    column was newly added, ``False`` when it already existed.
    """
    return [add_column_if_missing(conn, table, ddl) for ddl in column_ddls]

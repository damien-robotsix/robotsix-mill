"""SQLite engine + schema lifecycle.

One engine per process. ``check_same_thread=False`` because stage work
runs in a threadpool while the worker coroutine owns DB access; all
writes are still serialized through the single worker, so SQLite's
single-writer model is respected.
"""

from __future__ import annotations

import sqlite3
import logging

from sqlmodel import Session, SQLModel, create_engine

from ..config import Settings

log = logging.getLogger("robotsix_mill.db")

_engine = None


def get_engine(settings: Settings):
    """Return the process-wide SQLite engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.db_url,
            connect_args={"check_same_thread": False},
        )
    return _engine


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    type_sql: str,
    default: str,
) -> None:
    """Add *col* to *table* if it is not already present."""
    columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info('{table}')")
    }
    if col in columns:
        return
    conn.execute(
        f"ALTER TABLE {table} ADD COLUMN {col} {type_sql} DEFAULT {default}"
    )
    conn.commit()
    log.info("migration: added %s column to %s table", col, table)


def _rename_state_value(
    conn: sqlite3.Connection, old: str, new: str,
) -> None:
    """Rename a stored ``state`` enum value in every table that
    persists it (``ticket`` and ``ticketevent``).

    Needed when a :class:`~..states.State` member is renamed in code:
    existing rows still store the *old* enum name, and an ORM load of
    such a row raises ``LookupError`` and crashes startup. Raw SQL only
    — the ORM enum no longer knows the old name. Idempotent: a second
    run matches no rows.
    """
    renamed = 0
    for table in ("ticket", "ticketevent"):
        cur = conn.execute(
            f"UPDATE {table} SET state = ? WHERE state = ?", (new, old)
        )
        renamed += cur.rowcount
    if renamed:
        conn.commit()
        log.info(
            "migration: renamed state %s -> %s in %d row(s)",
            old, new, renamed,
        )


def _run_migrations(settings: Settings) -> None:
    """Run idempotent schema migrations on the existing database.

    SQLModel.metadata.create_all only creates missing tables; it does not
    alter existing ones. Use raw SQL (via the sqlite3 module, not the
    SQLAlchemy engine) for ALTER TABLE so we bypass ORM machinery.
    """
    db_path = str(settings.db_path)
    if not settings.db_path.exists():
        return  # nothing to migrate — create_all will build from scratch

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='ticket'"
        )
        if cur.fetchone() is None:
            return  # ticket table doesn't exist yet

        # cost_usd is no longer persisted — per-ticket cost is read
        # on-demand from the Langfuse session at API render time (see
        # langfuse_client.session_cost). The column may still exist on
        # older DBs; it's harmless and simply ignored (the API overwrites
        # the value in-memory on the way out). Keep the additive column
        # migration only so older code paths / direct DB reads don't trip
        # on a missing column.
        _add_column_if_missing(
            conn, "ticket", "source", "TEXT NOT NULL", "'user'"
        )
        _add_column_if_missing(
            conn, "ticket", "blocked_from", "TEXT", "NULL"
        )
        _add_column_if_missing(
            conn, "ticket", "origin_session", "TEXT", "NULL"
        )
        _add_column_if_missing(
            conn, "ticket", "cost_usd", "REAL NOT NULL", "0"
        )
        _add_column_if_missing(
            conn, "ticket", "depends_on", "TEXT", "NULL"
        )
        _add_column_if_missing(
            conn, "ticket", "kind", "TEXT NOT NULL", "'task'"
        )

        # State renames (PR #143): existing rows still store the old
        # enum NAMES; an ORM load of such a row raises LookupError and
        # crashes startup. Rename the stored values in place.
        _rename_state_value(conn, "IN_REVIEW", "HUMAN_MR_APPROVAL")
        _rename_state_value(
            conn, "AWAITING_APPROVAL", "HUMAN_ISSUE_APPROVAL"
        )
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    """Create tables (if missing) and run idempotent column migrations."""
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine(settings))
    _run_migrations(settings)


def session(settings: Settings) -> Session:
    """Return a new SQLModel Session bound to the process-wide engine."""
    return Session(get_engine(settings))


def reset_engine() -> None:
    """Test hook: drop the cached engine so a fresh DB path is picked up."""
    global _engine
    _engine = None

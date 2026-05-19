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
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.db_url,
            connect_args={"check_same_thread": False},
        )
    return _engine


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

        # Check which columns already exist.
        cur = conn.execute("PRAGMA table_info('ticket')")
        columns = {row[1] for row in cur.fetchall()}

        if "source" not in columns:
            conn.execute(
                "ALTER TABLE ticket ADD COLUMN source TEXT NOT NULL DEFAULT 'user'"
            )
            conn.commit()
            log.info("migration: added source column to ticket table")
        if "blocked_from" not in columns:
            conn.execute(
                "ALTER TABLE ticket ADD COLUMN blocked_from TEXT DEFAULT NULL"
            )
            conn.commit()
            log.info("migration: added blocked_from column to ticket table")

        if "origin_session" not in columns:
            conn.execute(
                "ALTER TABLE ticket ADD COLUMN origin_session TEXT DEFAULT NULL"
            )
            conn.commit()
            log.info("migration: added origin_session column to ticket table")

        # cost_usd is no longer persisted — per-ticket cost is read
        # on-demand from the Langfuse session at API render time (see
        # langfuse_client.session_cost). The column may still exist on
        # older DBs; it's harmless and simply ignored (the API overwrites
        # the value in-memory on the way out). Keep the additive column
        # migration only so older code paths / direct DB reads don't trip
        # on a missing column.
        if "cost_usd" not in columns:
            conn.execute(
                "ALTER TABLE ticket ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0"
            )
            conn.commit()
            log.info("migration: added cost_usd column to ticket table")
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine(settings))
    _run_migrations(settings)


def session(settings: Settings) -> Session:
    return Session(get_engine(settings))


def reset_engine() -> None:
    """Test hook: drop the cached engine so a fresh DB path is picked up."""
    global _engine
    _engine = None

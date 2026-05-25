"""SQLite engine + schema lifecycle.

One engine per process. ``check_same_thread=False`` because stage work
runs in a threadpool while the worker coroutine owns DB access; all
writes are still serialized through the single worker, so SQLite's
single-writer model is respected.
"""

from __future__ import annotations

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


def init_db(settings: Settings) -> None:
    """Create tables (if missing)."""
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    engine = get_engine(settings)
    SQLModel.metadata.create_all(engine)

    # SQLite / SQLModel do not auto-add columns to existing tables.
    # Ensure the board_id column from RepoConfig exists so the model
    # and schema stay in sync.  Ignore "duplicate column" errors.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ticket ADD COLUMN board_id TEXT NOT NULL DEFAULT ''"
            )
    except Exception:
        pass


def session(settings: Settings) -> Session:
    """Return a new SQLModel Session bound to the process-wide engine."""
    return Session(get_engine(settings))


def reset_engine() -> None:
    """Test hook: drop the cached engine so a fresh DB path is picked up."""
    global _engine
    _engine = None

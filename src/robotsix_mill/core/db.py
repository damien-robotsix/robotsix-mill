"""SQLite engine + schema lifecycle.

Per-repo databases: each registered repo gets its own SQLite file at
``<data_dir>/<board_id>/mill.db``; the default ``<data_dir>/mill.db``
holds anything without a board_id (legacy / unmapped). Engines are
cached process-wide, one per board.

``check_same_thread=False`` because stage work runs in a threadpool
while the worker coroutine owns DB access; all writes for a given
board are still serialized through that board's single worker, so
SQLite's single-writer model is respected.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from ..config import Settings

log = logging.getLogger("robotsix_mill.db")

# Per-board engine cache. "" key is the default DB at <data_dir>/mill.db.
_engines: dict[str, object] = {}

# Tracks which boards have had init_db() called so we can lazily
# materialize schema on first access for a fresh repo.
_initialized: set[str] = set()


def _db_path(settings: Settings, board_id: str) -> Path:
    """Return the on-disk path for *board_id*'s SQLite file."""
    if board_id:
        return settings.data_dir / board_id / "mill.db"
    return settings.data_dir / "mill.db"


def get_engine(settings: Settings, board_id: str = ""):
    """Return the per-board SQLite engine, creating it on first call.

    *board_id* selects the repo whose DB to open. Empty string uses
    the default DB at ``<data_dir>/mill.db``.

    Emits a warning with a stack trace when board_id is empty AND
    multi-repo is configured AND the file would be NEWLY created —
    every ticket should live in a per-repo DB in that mode, so any
    fresh ``<data_dir>/mill.db`` materialisation indicates a missing
    board_id thread-through. The engine is still returned so the
    caller doesn't blow up; the offending site can be fixed on its
    own schedule.
    """
    engine = _engines.get(board_id)
    if engine is None:
        path = _db_path(settings, board_id)
        if not board_id and not path.exists():
            try:
                from ..config import get_repos_config

                if get_repos_config().repos:
                    import logging
                    import traceback

                    logging.getLogger("robotsix_mill.core.db").warning(
                        "db.get_engine: creating board-less mill.db at %s "
                        "in multi-repo mode — board_id should be threaded "
                        "through. Stack:\n%s",
                        path,
                        "".join(traceback.format_stack(limit=8)),
                    )
            except Exception:  # noqa: BLE001
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
        )
        _engines[board_id] = engine
    return engine


def init_db(settings: Settings, board_id: str = "") -> None:
    """Create tables (if missing) on the per-board DB."""
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    engine = get_engine(settings, board_id)
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
    # Same pattern for the operator-set ``priority`` flag.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ticket ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
            )
    except Exception:
        pass
    # paused_from column: records the originating state when a ticket is
    # paused mid-stage to await a user reply (AWAITING_USER_REPLY).
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ticket ADD COLUMN paused_from TEXT")
    except Exception:
        pass
    # Hash-chain integrity columns for TicketEvent.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ticketevent ADD COLUMN prev_hash TEXT")
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ticketevent ADD COLUMN hash TEXT NOT NULL DEFAULT ''"
            )
    except Exception:
        pass
    _initialized.add(board_id)


def session(settings: Settings, board_id: str = "") -> Session:
    """Return a new SQLModel Session bound to the per-board engine.

    Lazily initializes the per-board schema on first access — so a
    fresh repo's DB is created on its first session() call without
    requiring an explicit init_db() at startup.
    """
    if board_id not in _initialized:
        init_db(settings, board_id)
    return Session(get_engine(settings, board_id))


def reset_engine() -> None:
    """Test hook: drop the cached engines so fresh DB paths are picked up."""
    global _engines, _initialized
    _engines = {}
    _initialized = set()

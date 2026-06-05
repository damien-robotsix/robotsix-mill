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

# Per-board engine cache.
_engines: dict[str, object] = {}

# Tracks which boards have had init_db() called so we can lazily
# materialize schema on first access for a fresh repo.
_initialized: set[str] = set()


def _db_path(settings: Settings, board_id: str) -> Path:
    """Return the on-disk path for *board_id*'s SQLite file.

    Single-repo deployments configure exactly one repo in
    ``config/repos.yaml`` and get ``<data_dir>/<repo_id>/mill.db`` —
    the legacy board-less ``<data_dir>/mill.db`` is gone. Raises
    ``ValueError`` when *board_id* is empty so callers that forgot to
    thread it through fail loudly at the site of the bug.
    """
    if not board_id:
        raise ValueError(
            "db._db_path: board_id is required. The board-less "
            "<data_dir>/mill.db is gone; configure your repo(s) in "
            "config/repos.yaml and pass the board_id through."
        )
    return settings.data_dir / board_id / "mill.db"


def get_engine(settings: Settings, board_id: str):
    """Return the per-board SQLite engine, creating it on first call.

    *board_id* is required — raises ``ValueError`` (via
    :func:`_db_path`) when empty.
    """
    engine = _engines.get(board_id)
    if engine is None:
        path = _db_path(settings, board_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{path}"
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
        )
        _engines[board_id] = engine
    return engine


def init_db(settings: Settings, board_id: str) -> None:
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
    # unblocks column: JSON list of ticket IDs to auto-unblock when this
    # ticket completes (the inverse of depends_on, declared on the solver).
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ticket ADD COLUMN unblocks TEXT")
    except Exception:
        pass
    # labels column: JSON list of free-form label strings on the ticket.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE ticket ADD COLUMN labels TEXT")
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
    # failure_reason column for ProposedAction: records why execution
    # failed (exception message) when status = FAILED.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE proposedaction ADD COLUMN failure_reason TEXT"
            )
    except Exception:
        pass
    _initialized.add(board_id)


def session(settings: Settings, board_id: str) -> Session:
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

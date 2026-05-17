"""SQLite engine + schema lifecycle.

One engine per process. ``check_same_thread=False`` because stage work
runs in a threadpool while the worker coroutine owns DB access; all
writes are still serialized through the single worker, so SQLite's
single-writer model is respected.
"""

from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from ..config import Settings

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


def init_db(settings: Settings) -> None:
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(get_engine(settings))


def session(settings: Settings) -> Session:
    return Session(get_engine(settings))


def reset_engine() -> None:
    """Test hook: drop the cached engine so a fresh DB path is picked up."""
    global _engine
    _engine = None

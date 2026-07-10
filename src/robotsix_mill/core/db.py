"""SQLite engine + schema lifecycle.

Per-repo databases: each registered repo gets its own SQLite file at
``<data_dir>/<board_id>/mill.db``; the default ``<data_dir>/mill.db``
holds anything without a board_id (legacy / unmapped). Engines are
cached process-wide, one per board.

``check_same_thread=False`` because stage work runs in a threadpool
while the worker coroutine owns DB access; all writes for a given
board are still serialized through that board's single worker, so
SQLite's single-writer model is respected.

Schema migration
----------------
``SQLModel.metadata.create_all()`` creates tables on fresh databases.
Alembic (``alembic/``) handles all subsequent schema changes — column
additions, renames, type changes, and table restructures.  The previous
hand-rolled additive-migration system (``sqlite_utils.py``) is retained
temporarily for reference but is no longer called; it will be removed
once all deployments have transitioned.
"""

from __future__ import annotations

import functools
import logging
import shutil
from pathlib import Path
from typing import Any, Literal

import sqlite3

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from ..config import Settings

log = logging.getLogger("robotsix_mill.db")

# Patterns in sqlite3.OperationalError messages that indicate disk
# exhaustion or a transient lock — distinct from schema errors.
_DB_FULL_MESSAGES: tuple[str, ...] = (
    "disk is full",
    "database or disk is full",
    "database is locked",
)


class DiskFullError(RuntimeError):
    """Raised when a DB operation fails due to disk exhaustion.

    The message includes the current disk usage stats when available.
    """


def _is_db_full_error(exc: sqlite3.OperationalError) -> bool:
    """Return ``True`` when *exc* matches a disk-full or lock pattern."""
    msg = str(exc).lower()
    return any(pat in msg for pat in _DB_FULL_MESSAGES)


def _log_disk_full(
    settings: Settings, board_id: str, exc: sqlite3.OperationalError
) -> None:
    """Log a warning with the error and current disk usage stats."""
    try:
        usage = shutil.disk_usage(settings.data_dir)
        log.warning(
            "Disk-full error on board %r: %s. Disk usage: total=%d, used=%d, free=%d",
            board_id,
            exc,
            usage.total,
            usage.used,
            usage.free,
        )
    except Exception:
        log.warning(
            "Disk-full error on board %r: %s",
            board_id,
            exc,
        )


def _reclaim_disk_space(settings: Settings, board_id: str) -> None:
    """Emergency VACUUM to reclaim free pages from the per-board DB.

    VACUUM cannot run inside a transaction, so we use a raw connection
    outside of any SQLModel session.
    """
    try:
        engine = get_engine(settings, board_id)
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
        log.info("Emergency VACUUM on board %r completed", board_id)
    except Exception:
        log.warning("Emergency VACUUM on board %r failed", board_id, exc_info=True)


# Per-board engine cache.
_engines: dict[str, Engine] = {}

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


def get_engine(settings: Settings, board_id: str) -> Engine:
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
            connect_args={
                "check_same_thread": False,
                "timeout": 5,
            },
        )

        @event.listens_for(engine, "connect")
        def _set_wal(dbapi_connection: Any, _: Any) -> None:
            """Enable WAL mode and cap the WAL file at 2 MiB.

            Without a size limit SQLite's default auto-checkpoint
            threshold (1000 pages ≈ 4 MiB) lets the WAL grow to ~4 MiB,
            which the data-dir audit flags as unbounded growth.
            """
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute("PRAGMA journal_size_limit = 2097152")

        _engines[board_id] = engine
    return engine


def _run_alembic_migrations(settings: Settings, board_id: str, engine: Engine) -> None:
    """Run Alembic migrations against the per-board SQLite database.

    When Alembic is installed (dev/CI environments), runs
    ``alembic upgrade head`` on tracked databases, and stamps
    fresh or pre-Alembic databases as ``head`` after running the
    legacy additive migrations one last time to catch any
    straggling columns.

    When Alembic is NOT installed (production deployments where it
    is only a dev dependency), falls back to the legacy
    ``run_additive_migrations`` behaviour so that ``init_db``
    remains functional.  Production deployments should run Alembic
    migrations via ``make migrate`` or ``scripts/migrate.sh``
    before deploying.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        # Alembic not installed (production) — fall back to legacy
        # additive migrations so init_db still works.
        from .sqlite_utils import run_additive_migrations as _legacy_migrate

        # engine.connect() (not begin()) because add_column_if_missing
        # calls conn.commit() internally after each ALTER TABLE, which
        # would close an engine.begin() transaction and break subsequent
        # operations.
        with engine.connect() as conn:
            _legacy_migrate(
                conn,
                "ticket",
                [
                    "board_id TEXT NOT NULL DEFAULT ''",
                    "priority INTEGER NOT NULL DEFAULT 0",
                    "paused_from TEXT",
                    "unblocks TEXT",
                    "labels TEXT",
                    "pre_redraft_cost_usd REAL DEFAULT 0.0",
                    "implement_cycles INTEGER NOT NULL DEFAULT 0",
                    "refine_passes INTEGER NOT NULL DEFAULT 0",
                    "refine_output_hash TEXT NOT NULL DEFAULT ''",
                ],
            )
            _legacy_migrate(
                conn,
                "ticketevent",
                [
                    "prev_hash TEXT",
                    "hash TEXT NOT NULL DEFAULT ''",
                ],
            )
        return

    from sqlalchemy import inspect as sa_inspect

    path = _db_path(settings, board_id)
    db_url = f"sqlite:///{path}"

    # Resolve alembic.ini relative to the repo root.  We walk up from
    # this file's location (src/robotsix_mill/core/) until we find it.
    _here = Path(__file__).resolve().parent  # .../core/
    _root = _here.parent.parent.parent  # repo root
    alembic_ini = _root / "alembic.ini"
    if not alembic_ini.is_file():
        # Fallback: try CWD (works when run from repo root, e.g. tests).
        alembic_ini = Path("alembic.ini")

    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    with engine.connect() as conn:
        inspector = sa_inspect(conn)
        has_alembic = "alembic_version" in inspector.get_table_names()

    if has_alembic:
        command.upgrade(alembic_cfg, "head")
    else:
        # Pre-Alembic database: run legacy additive migrations one last
        # time to catch any columns that may be missing from hand-rolled
        # pre-Alembic schemas (``create_all`` only creates tables, not
        # columns on existing tables).  Then stamp as ``head`` so future
        # runs use ``upgrade head``.
        from .sqlite_utils import run_additive_migrations as _legacy_migrate

        # engine.connect() (not begin()) — see note above.
        with engine.connect() as conn2:
            _legacy_migrate(
                conn2,
                "ticket",
                [
                    "board_id TEXT NOT NULL DEFAULT ''",
                    "priority INTEGER NOT NULL DEFAULT 0",
                    "paused_from TEXT",
                    "unblocks TEXT",
                    "labels TEXT",
                    "pre_redraft_cost_usd REAL DEFAULT 0.0",
                    "implement_cycles INTEGER NOT NULL DEFAULT 0",
                    "refine_passes INTEGER NOT NULL DEFAULT 0",
                    "refine_output_hash TEXT NOT NULL DEFAULT ''",
                ],
            )
            _legacy_migrate(
                conn2,
                "ticketevent",
                [
                    "prev_hash TEXT",
                    "hash TEXT NOT NULL DEFAULT ''",
                ],
            )
        command.stamp(alembic_cfg, "head")


def init_db(settings: Settings, board_id: str) -> None:
    """Create tables (if missing) on the per-board DB and apply any
    pending Alembic migrations."""
    # import models so SQLModel.metadata is populated before create_all
    from . import models  # noqa: F401

    engine = get_engine(settings, board_id)
    SQLModel.metadata.create_all(engine)

    # Run Alembic migrations — stamps fresh databases as ``head``,
    # applies pending migrations on existing ones.
    _run_alembic_migrations(settings, board_id, engine)

    # Self-heal any legacy rows whose ``kind`` was persisted as the
    # lowercase StrEnum *value* instead of the canonical uppercase
    # member NAME.  Idempotent: upper(upper(x)) == upper(x).  The
    # CaseTolerantEnum column on Ticket.kind already tolerates
    # lowercase on read, so this is defense-in-depth that also
    # normalizes the stored bytes.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE ticket SET kind = upper(kind)")
    except sqlite3.OperationalError:
        log.warning(
            "init_db: kind-normalization UPDATE failed — "
            "this is harmless when the ticket table does not exist yet",
            exc_info=True,
        )
    _initialized.add(board_id)


def session(settings: Settings, board_id: str) -> Session:
    """Return a new SQLModel Session bound to the per-board engine.

    Lazily initializes the per-board schema on first access — so a
    fresh repo's DB is created on its first session() call without
    requiring an explicit init_db() at startup.

    Every session returned by this function has its ``commit()`` and
    ``flush()`` methods wrapped with disk-full retry logic: on the
    first ``OperationalError`` matching a disk-full or lock pattern an
    emergency VACUUM is attempted and the operation is retried once.
    On a second failure a :class:`DiskFullError` is raised with the
    current disk usage in the message.
    """
    if board_id not in _initialized:
        init_db(settings, board_id)
    s = Session(get_engine(settings, board_id))
    _install_disk_full_retry(s, settings, board_id)
    return s


def _install_disk_full_retry(s: Session, settings: Settings, board_id: str) -> None:
    """Monkey-patch *s* so ``commit()`` and ``flush()`` retry once on
    disk-full errors after an emergency VACUUM."""
    s.commit = _make_safe_commit(s.commit, settings, board_id)  # type: ignore[method-assign]
    s.flush = _make_safe_flush(s.flush, settings, board_id)  # type: ignore[method-assign]


def _make_safe_commit(orig_commit: Any, settings: Settings, board_id: str) -> Any:
    def _safe_commit() -> None:
        _retry_db_op(orig_commit, settings, board_id)

    return _safe_commit


def _make_safe_flush(orig_flush: Any, settings: Settings, board_id: str) -> Any:
    def _safe_flush(*args: Any, **kwargs: Any) -> None:
        _retry_db_op(lambda: orig_flush(*args, **kwargs), settings, board_id)

    return _safe_flush


def _retry_db_op(op: Any, settings: Settings, board_id: str) -> None:
    """Call *op*; on disk-full OperationalError, VACUUM and retry once."""
    try:
        op()
    except sqlite3.OperationalError as e:
        if not _is_db_full_error(e):
            raise
        _log_disk_full(settings, board_id, e)
        _reclaim_disk_space(settings, board_id)
        try:
            op()
        except sqlite3.OperationalError as e2:
            if not _is_db_full_error(e2):
                raise
            _raise_disk_full(settings, e2)


def _raise_disk_full(settings: Settings, exc: sqlite3.OperationalError) -> None:
    """Raise :class:`DiskFullError` with current disk usage in the message."""
    try:
        usage = shutil.disk_usage(settings.data_dir)
        raise DiskFullError(
            f"DB operation failed after VACUUM retry: {exc}. "
            f"Disk total={usage.total}, used={usage.used}, free={usage.free}"
        ) from exc
    except Exception:
        raise DiskFullError(f"DB operation failed after VACUUM retry: {exc}") from exc


class retry_on_db_full:
    """Context manager and decorator that catches disk-full
    ``sqlite3.OperationalError``, runs an emergency VACUUM, and
    retries the wrapped operation once.

    .. note::

       Every :class:`sqlmodel.Session` returned by :func:`session`
       already has this protection installed on its ``commit()`` and
       ``flush()`` methods — this utility is only needed for code that
       bypasses :func:`session` (raw-engine or bare-connection calls).

    **Context manager**::

        with retry_on_db_full(settings, board_id):
            conn.exec_driver_sql("VACUUM")

    **Decorator**::

        @retry_on_db_full(settings, board_id)
        def my_db_operation():
            ...
    """

    def __init__(self, settings: Settings, board_id: str) -> None:
        self._settings = settings
        self._board_id = board_id

    def __enter__(self) -> retry_on_db_full:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> Literal[False]:
        if exc_type is None:
            return False
        if exc_type is not sqlite3.OperationalError:
            return False
        if exc_val is None:
            return False
        if not isinstance(exc_val, sqlite3.OperationalError):
            return False
        if not _is_db_full_error(exc_val):
            return False
        _log_disk_full(self._settings, self._board_id, exc_val)
        _reclaim_disk_space(self._settings, self._board_id)
        # Re-raise so the caller can retry the with-block body.
        _raise_disk_full(self._settings, exc_val)
        return False

    def __call__(self, func):  # type: ignore[no-untyped-def]
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if not _is_db_full_error(e):
                    raise
                _log_disk_full(self._settings, self._board_id, e)
                _reclaim_disk_space(self._settings, self._board_id)
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e2:
                    if not _is_db_full_error(e2):
                        raise
                    _raise_disk_full(self._settings, e2)

        return wrapper


def reset_engine() -> None:
    """Test hook: drop the cached engines so fresh DB paths are picked up.

    Dispose each engine before dropping it so its pooled SQLite
    connections (and their file descriptors) are released. Without this
    every test that swaps the cache leaks an undisposed engine; across a
    full suite run those leaked file descriptors accumulate and
    eventually trip an "unable to open database file" / too-many-open-
    files error on whichever later test happens to cross the limit.
    """
    global _engines, _initialized
    for engine in _engines.values():
        try:
            engine.dispose()
        except Exception:
            pass
    _engines = {}
    _initialized = set()


# ---------------------------------------------------------------------------
# Agent memory ledger — DB-backed read/write with retention
# ---------------------------------------------------------------------------


def load_memory_db(
    settings: Settings, board_id: str, name: str, max_chars: int | None = None
) -> str:
    """Read *name*'s memory ledger for *board_id* from the DB.

    When *max_chars* is set and the content exceeds that limit, the
    oldest entries are dropped — only the most recent content (by newline
    alignment) is kept, with a truncation note prepended.  Returns ``""``
    when no row exists for this (board_id, name) pair.
    """
    from sqlmodel import select

    from .models import Memory

    with session(settings, board_id) as s:
        row = s.exec(
            select(Memory).where(Memory.board_id == board_id, Memory.name == name)
        ).first()
    if row is None:
        return ""
    content = row.content
    if max_chars is not None and len(content) > max_chars:
        # Import tail_keep lazily from its canonical home.
        from .text_utils import tail_keep

        original_size = len(content)
        content = tail_keep(content, max_chars, label=f"memory ({name})")
        log.warning(
            "memory DB %s/%s truncated: %d → %d chars",
            board_id,
            name,
            original_size,
            len(content),
        )
    return content


def persist_memory_db(
    settings: Settings,
    board_id: str,
    name: str,
    text: str,
    max_chars: int | None = None,
) -> None:
    """Write *text* as *name*'s memory ledger for *board_id* to the DB.

    Upserts the (board_id, name) row.  On first write for a given row
    (no existing row), attempts a one-time migration from the legacy
    Markdown file (at ``settings.memory_file_for(name, board_id)``)
    and renames the migrated file to ``<name>_memory.md.migrated``.

    Strips ephemeral sections, applies *max_chars* truncation (same
    as the file-based ``persist_memory``), and updates ``updated_at``.
    """
    from datetime import datetime, timezone

    from sqlmodel import select

    from .text_utils import tail_keep
    from ..runners.pass_runner import strip_ephemeral_sections
    from .models import Memory

    text = strip_ephemeral_sections(text)
    if max_chars is not None and len(text) > max_chars:
        text = tail_keep(text, max_chars, label=f"memory ({name})")

    with session(settings, board_id) as s:
        row = s.exec(
            select(Memory).where(Memory.board_id == board_id, Memory.name == name)
        ).first()
        now = datetime.now(timezone.utc)
        if row is None:
            # First write — attempt one-time migration from legacy file.
            legacy_path = settings.memory_file_for(name, board_id)
            migrated_content = text  # default: use what we were given
            if legacy_path.exists():
                try:
                    file_content = legacy_path.read_text(encoding="utf-8")
                    if file_content.strip():
                        if not text.strip():
                            # No new text — carry over legacy content verbatim.
                            migrated_content = file_content
                        # else: keep migrated_content = text (new text provided)
                    legacy_path.rename(str(legacy_path) + ".migrated")
                    log.info(
                        "memory DB %s/%s: migrated %d chars from legacy file %s",
                        board_id,
                        name,
                        len(file_content),
                        legacy_path,
                    )
                except OSError:
                    log.warning(
                        "memory DB %s/%s: could not migrate legacy file %s",
                        board_id,
                        name,
                        legacy_path,
                        exc_info=True,
                    )
            row = Memory(
                board_id=board_id,
                name=name,
                content=migrated_content,
                created_at=now,
                updated_at=now,
            )
            s.add(row)
        else:
            row.content = text
            row.updated_at = now
            s.add(row)
        s.commit()

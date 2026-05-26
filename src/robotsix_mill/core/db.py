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


def migrate_legacy_global_db(settings: Settings) -> dict[str, int]:
    """One-shot migration: split the pre-per-repo global ``mill.db`` into
    per-repo DBs at ``<data_dir>/<board_id>/mill.db``.

    Idempotent. Runs at startup. The legacy DB is renamed to
    ``mill.db.legacy-pre-split`` after a successful copy so the
    operator can rollback by renaming it back if needed.

    Returns a dict ``{board_id: rows_copied}`` summarizing what moved.
    Skips entirely when:
    - The legacy global DB doesn't exist (fresh install, nothing to do).
    - The legacy DB has zero tickets (nothing to copy).
    - At least one per-repo DB already exists and has rows (treat as
      "already migrated" — don't risk clobbering).
    """
    legacy_path = settings.data_dir / "mill.db"
    if not legacy_path.exists():
        return {}

    from . import models  # noqa: F401
    from sqlalchemy import inspect

    # Open the legacy DB directly without entering the per-board cache —
    # if we re-enter via get_engine(""), every subsequent session("")
    # would re-target it after we rename below.
    legacy_engine = create_engine(
        f"sqlite:///{legacy_path}",
        connect_args={"check_same_thread": False},
    )

    # Schema sanity check — old DBs may predate the board_id / priority
    # columns. If the legacy file is empty / has no ticket table, skip.
    try:
        insp = inspect(legacy_engine)
        if "ticket" not in insp.get_table_names():
            legacy_engine.dispose()
            return {}
    except Exception:
        legacy_engine.dispose()
        return {}

    # Check whether any per-repo DB already holds data — handles two
    # scenarios:
    #   (a) Fully-migrated: per-repo DBs populated AND legacy file
    #       already renamed. Then this function isn't called at all.
    #   (b) Partial migration: per-repo DBs populated but legacy file
    #       still in place because a previous run crashed before the
    #       rename. Rename it now to complete the migration and exit
    #       cleanly without re-inserting (which would collide on
    #       UNIQUE).
    from ..config import get_repos_config

    repos = get_repos_config()
    any_populated = False
    for repo_id, rc in repos.repos.items():
        per_repo_path = _db_path(settings, rc.board_id)
        if per_repo_path.exists():
            try:
                eng = create_engine(
                    f"sqlite:///{per_repo_path}",
                    connect_args={"check_same_thread": False},
                )
                with eng.connect() as conn:
                    res = conn.exec_driver_sql("SELECT COUNT(*) FROM ticket").scalar()
                eng.dispose()
                if res and res > 0:
                    any_populated = True
                    log.info(
                        "migrate_legacy_global_db: per-repo DB at %s "
                        "already has %d rows", per_repo_path, res,
                    )
            except Exception:
                pass
    if any_populated:
        legacy_engine.dispose()
        legacy_backup = settings.data_dir / "mill.db.legacy-pre-split"
        try:
            legacy_path.rename(legacy_backup)
            log.info(
                "migrate_legacy_global_db: per-repo DBs already "
                "populated — completed migration by renaming legacy "
                "DB to %s", legacy_backup.name,
            )
        except OSError as e:
            log.warning(
                "migrate_legacy_global_db: could not rename legacy DB: %s", e,
            )
        return {}

    # OK to migrate. Use raw SQL for the copy — SQLAlchemy schemas
    # might mismatch between legacy + new and we just want row-faithful.
    from .models import Ticket, TicketEvent, Comment

    moved: dict[str, int] = {}

    # Ensure each repo's destination DB has the schema in place.
    for rc in repos.repos.values():
        init_db(settings, rc.board_id)
    init_db(settings, "")  # default — for legacy/no-board rows

    # Pull each ticket and route it. Use dict(row._mapping) to get
    # column-name access regardless of how SQLAlchemy materializes rows.
    with Session(legacy_engine) as legacy_s:
        tickets = [dict(r._mapping) for r in legacy_s.exec(Ticket.__table__.select()).all()]
        events = [dict(r._mapping) for r in legacy_s.exec(TicketEvent.__table__.select()).all()]
        comments = [dict(r._mapping) for r in legacy_s.exec(Comment.__table__.select()).all()]

    # Group ticket_ids by destination board.
    ticket_to_board: dict[str, str] = {}
    for row in tickets:
        tid = row["id"]
        # When the legacy DB pre-dates board_id the column may be
        # missing or empty — leave those in the default DB.
        ticket_to_board[tid] = row.get("board_id") or ""

    # Pre-build per-board lists for batched insert. Only tickets with
    # a non-empty board_id move — empty-board (legacy) rows stay in
    # the legacy DB; the default DB at <data_dir>/mill.db is recreated
    # empty after the rename. Inserting into "" would collide with the
    # legacy DB itself since _db_path(settings, "") == legacy_path.
    per_board_rows: dict[str, dict[str, list]] = {}
    for row in tickets:
        board = ticket_to_board[row["id"]]
        if not board:
            continue
        per_board_rows.setdefault(board, {"ticket": [], "event": [], "comment": []})
        per_board_rows[board]["ticket"].append(row)
    for row in events:
        tid = row.get("ticket_id", "")
        board = ticket_to_board.get(tid, "")
        if not board:
            continue
        per_board_rows.setdefault(board, {"ticket": [], "event": [], "comment": []})
        per_board_rows[board]["event"].append(row)
    for row in comments:
        tid = row.get("ticket_id", "")
        board = ticket_to_board.get(tid, "")
        if not board:
            continue
        per_board_rows.setdefault(board, {"ticket": [], "event": [], "comment": []})
        per_board_rows[board]["comment"].append(row)

    # Insert into each destination DB.
    for board, rows in per_board_rows.items():
        if not rows["ticket"] and not rows["event"] and not rows["comment"]:
            continue
        dest = get_engine(settings, board)
        with Session(dest) as dest_s:
            for r in rows["ticket"]:
                dest_s.execute(Ticket.__table__.insert().values(**r))
            for r in rows["event"]:
                # event id is auto-increment in the new DB; drop the old PK.
                r = {k: v for k, v in r.items() if k != "id"}
                dest_s.execute(TicketEvent.__table__.insert().values(**r))
            for r in rows["comment"]:
                r = {k: v for k, v in r.items() if k != "id"}
                dest_s.execute(Comment.__table__.insert().values(**r))
            dest_s.commit()
        moved[board] = len(rows["ticket"])
        log.info(
            "migrate_legacy_global_db: moved %d tickets to board %r",
            len(rows["ticket"]), board or "<default>",
        )

    legacy_engine.dispose()

    # Rename the legacy DB so subsequent runs don't repeat the
    # migration (and the operator can rollback by renaming back).
    legacy_backup = settings.data_dir / "mill.db.legacy-pre-split"
    try:
        legacy_path.rename(legacy_backup)
        log.info(
            "migrate_legacy_global_db: renamed legacy DB to %s",
            legacy_backup.name,
        )
    except OSError as e:
        log.warning(
            "migrate_legacy_global_db: could not rename legacy DB: %s", e,
        )

    return moved


def migrate_legacy_workspaces(settings: Settings) -> dict[str, int]:
    """One-shot migration: move per-ticket workspace dirs from the
    flat ``<data_dir>/workspaces/<ticket_id>/`` layout to the per-repo
    ``<data_dir>/<board_id>/workspaces/<ticket_id>/`` layout.

    Runs after :func:`migrate_legacy_global_db` so each ticket's
    board_id is available in its per-repo DB.

    Idempotent: only moves workspaces whose ticket lives in a
    per-repo DB (not in the default DB) AND whose dir is still under
    the legacy root. Skips silently when the legacy workspaces dir
    is missing or empty.

    Returns ``{board_id: dirs_moved}``.
    """
    legacy_root = settings.data_dir / "workspaces"
    if not legacy_root.exists() or not legacy_root.is_dir():
        return {}

    from ..config import get_repos_config
    from .models import Ticket

    repos = get_repos_config()
    if not repos.repos:
        return {}

    # Build ticket_id → board_id mapping by sweeping each per-repo DB.
    ticket_to_board: dict[str, str] = {}
    for rc in repos.repos.values():
        try:
            engine = get_engine(settings, rc.board_id)
            with engine.connect() as conn:
                for tid, in conn.exec_driver_sql("SELECT id FROM ticket").fetchall():
                    ticket_to_board[tid] = rc.board_id
        except Exception as e:
            log.warning(
                "migrate_legacy_workspaces: could not read tickets for "
                "board %r: %s", rc.board_id, e,
            )

    moved: dict[str, int] = {}
    for entry in legacy_root.iterdir():
        if not entry.is_dir():
            continue
        ticket_id = entry.name
        board_id = ticket_to_board.get(ticket_id)
        if not board_id:
            # Ticket not in any per-repo DB → leave at legacy root.
            continue
        dest_root = settings.data_dir / board_id / "workspaces"
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / ticket_id
        if dest.exists():
            log.warning(
                "migrate_legacy_workspaces: destination %s already exists, "
                "leaving legacy %s in place",
                dest, entry,
            )
            continue
        try:
            entry.rename(dest)
            moved[board_id] = moved.get(board_id, 0) + 1
        except OSError as e:
            log.warning(
                "migrate_legacy_workspaces: could not move %s → %s: %s",
                entry, dest, e,
            )

    for board_id, n in moved.items():
        log.info(
            "migrate_legacy_workspaces: moved %d workspace(s) to "
            "<data_dir>/%s/workspaces/", n, board_id,
        )

    # Clean the data root: remove the legacy workspaces dir if empty,
    # otherwise rename to a clearly-marked backup so the operator can
    # inspect (and discard once happy). Mirrors mill.db.legacy-pre-split.
    try:
        if not any(legacy_root.iterdir()):
            legacy_root.rmdir()
            log.info(
                "migrate_legacy_workspaces: removed empty legacy "
                "<data_dir>/workspaces/ root",
            )
        else:
            backup = settings.data_dir / "workspaces.legacy-pre-split"
            if not backup.exists():
                legacy_root.rename(backup)
                log.info(
                    "migrate_legacy_workspaces: renamed leftover legacy "
                    "<data_dir>/workspaces/ → %s (orphan/conflict "
                    "workspaces preserved for inspection)", backup.name,
                )
    except OSError:
        pass

    return moved


# All known memory ledger file basenames — kept in sync with the names
# passed to :meth:`Settings.memory_file_for`.
_LEGACY_MEMORY_NAMES = (
    "audit", "agent_check", "bc_check", "ci_fix",
    "completeness_check", "cost_reconciliation", "doc",
    "env_sync", "expert_python-backend", "health",
    "implement", "rebase", "refine", "retrospect",
    "review_revision", "survey", "test_gap",
    "trace_inspector",
)


def migrate_legacy_memories(settings: Settings) -> dict[str, int]:
    """One-shot migration: copy ``<data_dir>/<name>_memory.md`` ledger
    files into every registered repo's subtree as
    ``<data_dir>/<board_id>/<name>_memory.md``.

    Why copy rather than move: ledgers accumulate generic patterns
    (refine triage rules, retrospect observations) that are equally
    relevant to every repo. Seeding each per-repo ledger with the
    pre-split ledger gives each repo a useful warm start.

    Idempotent. Skips a destination if it already exists (so a repo
    that has begun accumulating its own ledger isn't overwritten).
    After seeding, the legacy file at ``<data_dir>/<name>_memory.md``
    is renamed to ``<name>_memory.md.legacy-pre-split`` so the data
    root stays clean.

    Returns ``{board_id: files_copied}``.
    """
    from ..config import get_repos_config
    import shutil

    try:
        repos = get_repos_config()
    except Exception:
        return {}
    if not repos.repos:
        return {}

    copied: dict[str, int] = {}
    for name in _LEGACY_MEMORY_NAMES:
        src = settings.data_dir / f"{name}_memory.md"
        if not src.exists() or not src.is_file():
            continue
        for rc in repos.repos.values():
            board_dir = settings.data_dir / rc.board_id
            board_dir.mkdir(parents=True, exist_ok=True)
            dest = board_dir / f"{name}_memory.md"
            if dest.exists():
                continue
            try:
                shutil.copy2(src, dest)
                copied[rc.board_id] = copied.get(rc.board_id, 0) + 1
            except OSError as e:
                log.warning(
                    "migrate_legacy_memories: could not copy %s → %s: %s",
                    src, dest, e,
                )
        backup = settings.data_dir / f"{name}_memory.md.legacy-pre-split"
        if not backup.exists():
            try:
                src.rename(backup)
            except OSError as e:
                log.warning(
                    "migrate_legacy_memories: could not rename %s → %s: %s",
                    src, backup, e,
                )

    for board_id, n in copied.items():
        log.info(
            "migrate_legacy_memories: seeded %d ledger(s) into "
            "<data_dir>/%s/", n, board_id,
        )

    return copied

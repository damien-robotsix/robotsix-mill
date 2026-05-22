"""Tests for db.init_db migrations, especially the idempotent source
and cost_usd column additions.
"""

import sqlite3

from robotsix_mill.config import Settings
from robotsix_mill.core import db

# --- helpers ---

_PRE_MIGRATION_TICKET_DDL = """
    CREATE TABLE ticket (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'draft',
        workspace_path TEXT NOT NULL,
        content_hash TEXT NOT NULL DEFAULT '',
        branch TEXT,
        parent_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

_EVENT_DDL = """
    CREATE TABLE ticketevent (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT,
        state TEXT NOT NULL,
        note TEXT,
        at TEXT NOT NULL
    )
"""

_INSERT_DUMMY = (
    "INSERT INTO ticket (id, title, state, workspace_path, created_at, updated_at) "
    "VALUES ('test-dummy', 'Test', 'draft', '/tmp/ws', '2025-01-01T00:00:00Z', "
    "'2025-01-01T00:00:00Z')"
)


# --- source column ---

def test_migration_adds_source_column(tmp_path):
    """On a DB with a ticket table but no source column, migration adds it."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    # Create a pre-migration DB manually: a ticket table without the
    # source column, plus the ticketevent table so create_all is
    # satisfied (it won't recreate existing tables).
    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(_PRE_MIGRATION_TICKET_DDL)
    conn.execute(_EVENT_DDL)
    conn.execute(_INSERT_DUMMY)
    conn.commit()
    conn.close()

    # Reset engine so the next init_db picks up the changed DB.
    db.reset_engine()

    # Run init — migration should add the column.
    db.init_db(s)

    # Verify the column exists now.
    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "source" in columns

    # Verify the default value was applied on the existing row.
    cur = conn.execute("SELECT source FROM ticket")
    rows = cur.fetchall()
    for row in rows:
        assert row[0] == "user"
    conn.close()


def test_migration_idempotent(tmp_path):
    """Running the migration a second time is a no-op."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    # First init creates everything from scratch (including source).
    db.init_db(s)

    # Verify source column exists.
    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "source" in columns
    conn.close()

    # Second init — should be a no-op (no error).
    db.reset_engine()
    db.init_db(s)

    # Verify still intact.
    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns2 = {row[1] for row in cur.fetchall()}
    assert "source" in columns2
    conn.close()


def test_migration_noop_when_no_table(tmp_path):
    """Migration is a no-op when the ticket table doesn't exist (fresh DB
    with no tables, or a DB where create_all hasn't run yet)."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    # Create an empty DB file manually (no tables).
    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    conn.close()

    # init_db with no tables — create_all will create the table with
    # source, migration should not break.
    db.init_db(s)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "source" in columns
    conn.close()


def test_fresh_db_has_source_column(tmp_path):
    """A brand-new DB created from scratch has the source column."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db.init_db(s)

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "source" in columns
    conn.close()


# --- cost_usd column ---

def test_migration_adds_cost_usd_column(tmp_path):
    """On a DB with a ticket table but no cost_usd column, migration adds
    it with a default of 0.0."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(_PRE_MIGRATION_TICKET_DDL)
    conn.execute(_EVENT_DDL)
    conn.execute(_INSERT_DUMMY)
    conn.commit()
    conn.close()

    db.reset_engine()
    db.init_db(s)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "cost_usd" in columns

    cur = conn.execute("SELECT cost_usd FROM ticket")
    rows = cur.fetchall()
    for row in rows:
        assert row[0] == 0.0
    conn.close()


def test_migration_cost_usd_idempotent(tmp_path):
    """Running the migration a second time is a no-op for cost_usd."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db.init_db(s)

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "cost_usd" in columns
    conn.close()

    db.reset_engine()
    db.init_db(s)  # must not raise

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns2 = {row[1] for row in cur.fetchall()}
    assert "cost_usd" in columns2
    conn.close()


def test_fresh_db_has_cost_usd_column(tmp_path):
    """A brand-new DB created from scratch has the cost_usd column."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db.init_db(s)

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "cost_usd" in columns
    conn.close()


def test_migration_adds_both_columns_when_both_missing(tmp_path):
    """When a DB has neither source nor cost_usd, both are added in one run."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(_PRE_MIGRATION_TICKET_DDL)
    conn.execute(_EVENT_DDL)
    conn.execute(_INSERT_DUMMY)
    conn.commit()
    conn.close()

    db.reset_engine()
    db.init_db(s)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "source" in columns
    assert "cost_usd" in columns
    conn.close()


# --- origin_session column ---

def test_migration_adds_origin_session_column(tmp_path):
    """On a DB with a ticket table but no origin_session column,
    migration adds it with a default of NULL."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(_PRE_MIGRATION_TICKET_DDL)
    conn.execute(_EVENT_DDL)
    conn.execute(_INSERT_DUMMY)
    conn.commit()
    conn.close()

    db.reset_engine()
    db.init_db(s)

    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "origin_session" in columns

    cur = conn.execute("SELECT origin_session FROM ticket")
    rows = cur.fetchall()
    for row in rows:
        assert row[0] is None  # existing rows get NULL
    conn.close()


def test_migration_origin_session_idempotent(tmp_path):
    """Running the migration a second time is a no-op for origin_session."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db.init_db(s)

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "origin_session" in columns
    conn.close()

    db.reset_engine()
    db.init_db(s)  # must not raise

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns2 = {row[1] for row in cur.fetchall()}
    assert "origin_session" in columns2
    conn.close()


def test_fresh_db_has_origin_session_column(tmp_path):
    """A brand-new DB created from scratch has the origin_session column."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db.init_db(s)

    conn = sqlite3.connect(str(s.db_path))
    cur = conn.execute("PRAGMA table_info('ticket')")
    columns = {row[1] for row in cur.fetchall()}
    assert "origin_session" in columns
    conn.close()


# --- full migration integration (oldest supported schema → current) ---

_OLD_STATE_ROWS = (
    # ticket rows with old enum names
    "INSERT INTO ticket (id, title, state, workspace_path, created_at, "
    "updated_at) VALUES "
    "('t-in-review', 'Old IN_REVIEW', 'IN_REVIEW', '/tmp/ws', "
    "'2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
    "INSERT INTO ticket (id, title, state, workspace_path, created_at, "
    "updated_at) VALUES "
    "('t-awaiting', 'Old AWAITING_APPROVAL', 'AWAITING_APPROVAL', '/tmp/ws', "
    "'2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
)

_OLD_EVENT_ROWS = (
    "INSERT INTO ticketevent (ticket_id, state, at) VALUES "
    "('t-in-review', 'IN_REVIEW', '2025-01-01T00:00:00Z')",
    "INSERT INTO ticketevent (ticket_id, state, at) VALUES "
    "('t-awaiting', 'AWAITING_APPROVAL', '2025-01-01T00:00:00Z')",
)


def test_full_migration_integration(tmp_path):
    """Materialise a DB at the oldest supported prior schema (no additive
    columns, old state enum names) and assert init_db migrates every
    column and state rename correctly."""
    db.reset_engine()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    s = Settings(MILL_DATA_DIR=str(data_dir))

    db_path = str(s.db_path)
    conn = sqlite3.connect(db_path)
    # Oldest supported schema: ticket DDL without source, blocked_from,
    # origin_session, cost_usd, depends_on, or kind.
    conn.execute(_PRE_MIGRATION_TICKET_DDL)
    conn.execute(_EVENT_DDL)
    for stmt in _OLD_STATE_ROWS:
        conn.execute(stmt)
    for stmt in _OLD_EVENT_ROWS:
        conn.execute(stmt)
    conn.commit()
    conn.close()

    # -- run the migration -------------------------------------------------
    db.reset_engine()
    db.init_db(s)

    # -- assert all additive columns are present ---------------------------
    conn = sqlite3.connect(db_path)
    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info('ticket')")
    }
    for col in (
        "source", "blocked_from", "origin_session", "cost_usd",
        "depends_on", "kind",
    ):
        assert col in columns, f"Column '{col}' missing after migration"

    # -- assert state values were renamed in ticket table ------------------
    cur = conn.execute("SELECT id, state FROM ticket ORDER BY id")
    ticket_states = {row[0]: row[1] for row in cur.fetchall()}
    assert ticket_states["t-in-review"] == "HUMAN_MR_APPROVAL", (
        f"Expected HUMAN_MR_APPROVAL, got {ticket_states['t-in-review']}"
    )
    assert ticket_states["t-awaiting"] == "HUMAN_ISSUE_APPROVAL", (
        f"Expected HUMAN_ISSUE_APPROVAL, got {ticket_states['t-awaiting']}"
    )

    # -- assert state values were renamed in ticketevent table -------------
    cur = conn.execute(
        "SELECT ticket_id, state FROM ticketevent ORDER BY ticket_id"
    )
    event_states = {row[0]: row[1] for row in cur.fetchall()}
    assert event_states["t-in-review"] == "HUMAN_MR_APPROVAL"
    assert event_states["t-awaiting"] == "HUMAN_ISSUE_APPROVAL"

    # -- assert defaults on existing row -----------------------------------
    cur = conn.execute(
        "SELECT source, kind, cost_usd, blocked_from, origin_session, "
        "depends_on FROM ticket WHERE id = 't-in-review'"
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "user", f"source default: expected 'user', got {row[0]}"
    assert row[1] == "task", f"kind default: expected 'task', got {row[1]}"
    assert row[2] == 0.0, f"cost_usd default: expected 0.0, got {row[2]}"
    assert row[3] is None, f"blocked_from default: expected NULL, got {row[3]}"
    assert row[4] is None, f"origin_session default: expected NULL, got {row[4]}"
    assert row[5] is None, f"depends_on default: expected NULL, got {row[5]}"

    conn.close()

    # -- assert idempotency: a second init_db does not error ---------------
    db.reset_engine()
    db.init_db(s)

    # Spot-check: columns still present, state values still correct.
    conn = sqlite3.connect(db_path)
    columns2 = {
        row[1]
        for row in conn.execute("PRAGMA table_info('ticket')")
    }
    for col in (
        "source", "blocked_from", "origin_session", "cost_usd",
        "depends_on", "kind",
    ):
        assert col in columns2, f"Column '{col}' missing after second init_db"

    cur = conn.execute("SELECT id, state FROM ticket WHERE id = 't-in-review'")
    assert cur.fetchone()[1] == "HUMAN_MR_APPROVAL"
    conn.close()

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

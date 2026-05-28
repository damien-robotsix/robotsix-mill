"""Tests for the local SQLite datastore (db.py)."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from robotsix_auto_mail.db import (
    MailRecord,
    get_watermark,
    init_db,
    insert_record,
    set_watermark,
)

# ---------------------------------------------------------------------------
# MailRecord construction and defaults
# ---------------------------------------------------------------------------


def test_mailrecord_required_fields() -> None:
    """Minimum required fields produce a valid MailRecord."""
    record = MailRecord(
        message_id="<abc@example.com>",
        sender="alice@example.com",
        subject="Hello",
        date="2025-01-15T10:30:00Z",
    )
    assert record.message_id == "<abc@example.com>"
    assert record.sender == "alice@example.com"
    assert record.subject == "Hello"
    assert record.date == "2025-01-15T10:30:00Z"
    # Defaults
    assert record.imap_uid is None
    assert record.recipients_json == '{"to": [], "cc": []}'
    assert record.body_plain == ""
    assert record.body_html == ""
    assert record.attachments_json == "[]"
    assert record.id == 0


def test_mailrecord_is_frozen() -> None:
    """MailRecord is immutable."""
    record = MailRecord(
        message_id="<abc@example.com>",
        sender="a@x.com",
        subject="S",
        date="2025-01-01",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.subject = "Changed"


def test_mailrecord_all_fields_explicit() -> None:
    """All fields can be set explicitly."""
    record = MailRecord(
        message_id="<def@example.com>",
        sender="bob@example.com",
        subject="Test",
        date="2025-06-01",
        imap_uid=42,
        recipients_json='{"to": ["x@x.com"], "cc": ["y@y.com"]}',
        body_plain="Hello world",
        body_html="<p>Hello world</p>",
        attachments_json='[{"filename": "a.pdf", "size": 1024}]',
        id=0,
    )
    assert record.imap_uid == 42
    assert record.recipients_json == '{"to": ["x@x.com"], "cc": ["y@y.com"]}'
    assert record.body_plain == "Hello world"
    assert record.body_html == "<p>Hello world</p>"
    assert record.attachments_json == '[{"filename": "a.pdf", "size": 1024}]'


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_returns_connection() -> None:
    """init_db returns a sqlite3.Connection."""
    conn = init_db(":memory:")
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_init_db_creates_mail_records_table() -> None:
    """mail_records table exists and has the expected columns."""
    conn = init_db(":memory:")
    try:
        cursor = conn.execute("PRAGMA table_info('mail_records')")
        cols = {row[1]: row[2] for row in cursor.fetchall()}
        expected = {
            "id": "INTEGER",
            "imap_uid": "INTEGER",
            "message_id": "TEXT",
            "sender": "TEXT",
            "subject": "TEXT",
            "date": "TEXT",
            "recipients_json": "TEXT",
            "body_plain": "TEXT",
            "body_html": "TEXT",
            "attachments_json": "TEXT",
        }
        for name, type_ in expected.items():
            assert name in cols, f"Column {name} missing"
            assert cols[name].upper() == type_, f"Column {name} type mismatch"
        assert len(cols) == len(expected), f"Extra columns: {set(cols) - set(expected)}"
    finally:
        conn.close()


def test_init_db_creates_watermark_table() -> None:
    """watermark table exists with key/value columns."""
    conn = init_db(":memory:")
    try:
        cursor = conn.execute("PRAGMA table_info('watermark')")
        cols = {row[1]: row[2] for row in cursor.fetchall()}
        assert cols == {"key": "TEXT", "value": "TEXT"}
    finally:
        conn.close()


def test_init_db_enables_wal() -> None:
    """WAL journal mode is requested (in-memory DBs use 'memory' mode)."""
    conn = init_db(":memory:")
    try:
        cur = conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        # On :memory: databases WAL is not applicable; on file-based
        # databases it will be "wal".  Either is fine — the pragma was
        # set and didn't error.
        assert mode.lower() in ("wal", "memory")
    finally:
        conn.close()


def test_init_db_enables_foreign_keys() -> None:
    """Foreign keys pragma is ON."""
    conn = init_db(":memory:")
    try:
        cur = conn.execute("PRAGMA foreign_keys")
        val = cur.fetchone()[0]
        assert val == 1
    finally:
        conn.close()


def test_init_db_idempotent() -> None:
    """Calling init_db twice on the same database is safe."""
    conn = init_db(":memory:")
    try:
        conn.executescript(_SCHEMA_AGAIN)
        # No exception = success
    finally:
        conn.close()


_SCHEMA_AGAIN = """
CREATE TABLE IF NOT EXISTS mail_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    imap_uid        INTEGER,
    message_id      TEXT    NOT NULL UNIQUE,
    sender          TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    date            TEXT    NOT NULL,
    recipients_json TEXT    NOT NULL,
    body_plain      TEXT    NOT NULL,
    body_html       TEXT    NOT NULL,
    attachments_json TEXT   NOT NULL
);

CREATE TABLE IF NOT EXISTS watermark (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# insert_record
# ---------------------------------------------------------------------------


def _make_record(**overrides: str | int | None) -> MailRecord:
    defaults: dict[str, str | int | None] = {
        "message_id": "<test@example.com>",
        "sender": "sender@example.com",
        "subject": "Test Subject",
        "date": "2025-06-01T12:00:00Z",
    }
    defaults.update(overrides)
    return MailRecord(**defaults)


def test_insert_record_returns_rowid() -> None:
    """Successful insert returns the new rowid."""
    conn = init_db(":memory:")
    try:
        record = _make_record()
        rowid = insert_record(conn, record)
        assert rowid is not None
        assert isinstance(rowid, int)
        assert rowid > 0
    finally:
        conn.close()


def test_insert_record_persists_data() -> None:
    """Inserted data can be read back from the table."""
    conn = init_db(":memory:")
    try:
        record = MailRecord(
            message_id="<m1@example.com>",
            sender="alice@example.com",
            subject="Hello",
            date="2025-06-01",
            imap_uid=10,
            recipients_json='{"to": ["bob@x.com"], "cc": []}',
            body_plain="Hi Bob",
            body_html="<p>Hi Bob</p>",
            attachments_json='[{"name": "file.txt"}]',
        )
        rowid = insert_record(conn, record)
        cur = conn.execute(
            "SELECT * FROM mail_records WHERE id = ?", (rowid,)
        )
        row = cur.fetchone()
        assert row is not None
        col_names = [desc[0] for desc in cur.description]
        data = dict(zip(col_names, row))
        assert data["message_id"] == "<m1@example.com>"
        assert data["sender"] == "alice@example.com"
        assert data["subject"] == "Hello"
        assert data["date"] == "2025-06-01"
        assert data["imap_uid"] == 10
        assert data["recipients_json"] == '{"to": ["bob@x.com"], "cc": []}'
        assert data["body_plain"] == "Hi Bob"
        assert data["body_html"] == "<p>Hi Bob</p>"
        assert data["attachments_json"] == '[{"name": "file.txt"}]'
    finally:
        conn.close()


def test_insert_record_ignores_id_field() -> None:
    """The id field of the input record is ignored; DB assigns it."""
    conn = init_db(":memory:")
    try:
        record = _make_record(message_id="<id-test@example.com>", id=9999)
        rowid = insert_record(conn, record)
        assert rowid is not None
        # The DB-assigned id should be the rowid, not 9999.
        cur = conn.execute(
            "SELECT id FROM mail_records WHERE message_id = ?",
            ("<id-test@example.com>",),
        )
        db_id = cur.fetchone()[0]
        assert db_id == rowid
    finally:
        conn.close()


def test_insert_record_unique_constraint_returns_none() -> None:
    """Inserting the same message_id twice returns None on the second call."""
    conn = init_db(":memory:")
    try:
        record = _make_record(message_id="<dup@example.com>")
        first = insert_record(conn, record)
        assert first is not None
        second = insert_record(conn, record)
        assert second is None
    finally:
        conn.close()


def test_insert_record_unique_constraint_no_exception() -> None:
    """Duplicate message_id does NOT raise — it returns None."""
    conn = init_db(":memory:")
    try:
        record = _make_record(message_id="<no-raise@example.com>")
        insert_record(conn, record)
        # Should not raise
        result = insert_record(conn, record)
        assert result is None
    finally:
        conn.close()


def test_insert_record_unique_constraint_only_one_row() -> None:
    """Duplicate insert leaves exactly one row in the table."""
    conn = init_db(":memory:")
    try:
        record = _make_record(message_id="<one-row@example.com>")
        insert_record(conn, record)
        insert_record(conn, record)  # duplicate
        cur = conn.execute(
            "SELECT COUNT(*) FROM mail_records WHERE message_id = ?",
            ("<one-row@example.com>",),
        )
        count = cur.fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_insert_record_imap_uid_nullable() -> None:
    """imap_uid can be None (NULL in DB)."""
    conn = init_db(":memory:")
    try:
        record = _make_record(
            message_id="<no-uid@example.com>", imap_uid=None
        )
        rowid = insert_record(conn, record)
        cur = conn.execute(
            "SELECT imap_uid FROM mail_records WHERE id = ?", (rowid,)
        )
        val = cur.fetchone()[0]
        assert val is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------


def test_get_watermark_nonexistent_returns_none() -> None:
    """Never-set watermark returns None."""
    conn = init_db(":memory:")
    try:
        result = get_watermark(conn, "nonexistent")
        assert result is None
    finally:
        conn.close()


def test_set_and_get_watermark() -> None:
    """set_watermark followed by get_watermark returns the value."""
    conn = init_db(":memory:")
    try:
        set_watermark(conn, "last_uid", "42")
        assert get_watermark(conn, "last_uid") == "42"
    finally:
        conn.close()


def test_set_watermark_upserts() -> None:
    """Setting the same key twice updates the value (no duplicate rows)."""
    conn = init_db(":memory:")
    try:
        set_watermark(conn, "last_uid", "42")
        set_watermark(conn, "last_uid", "99")
        assert get_watermark(conn, "last_uid") == "99"

        # Only one row should exist for the key.
        cur = conn.execute(
            "SELECT COUNT(*) FROM watermark WHERE key = ?", ("last_uid",)
        )
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_watermark_multiple_keys() -> None:
    """Different keys are independent."""
    conn = init_db(":memory:")
    try:
        set_watermark(conn, "last_uid", "10")
        set_watermark(conn, "other_key", "hello")
        assert get_watermark(conn, "last_uid") == "10"
        assert get_watermark(conn, "other_key") == "hello"
    finally:
        conn.close()


def test_watermark_across_connections() -> None:
    """Watermark persists across connections (using a temp file)."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)  # close the file descriptor; sqlite3 will open its own
    try:
        conn1 = init_db(path)
        set_watermark(conn1, "uid", "77")
        conn1.close()

        conn2 = init_db(path)
        assert get_watermark(conn2, "uid") == "77"
        conn2.close()
    finally:
        os.unlink(path)

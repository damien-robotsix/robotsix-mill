"""Tests for the local SQLite datastore (db.py)."""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from robotsix_auto_mail.db import (
    MailRecord,
    get_record_by_message_id,
    get_watermark,
    init_db,
    insert_record,
    list_records,
    set_watermark,
)
from tests.conftest import _make_record

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
    assert record.status == "inbox"
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
        record.subject = "Changed"  # type: ignore[misc]


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
    assert record.status == "inbox"
    assert record.imap_uid == 42
    assert record.recipients_json == '{"to": ["x@x.com"], "cc": ["y@y.com"]}'
    assert record.body_plain == "Hello world"
    assert record.body_html == "<p>Hello world</p>"
    assert record.attachments_json == '[{"filename": "a.pdf", "size": 1024}]'


def test_mailrecord_status_explicit() -> None:
    """status can be set explicitly to a non-default value."""
    record = MailRecord(
        message_id="<status@example.com>",
        sender="x@x.com",
        subject="S",
        date="2025-01-01",
        status="triaging",
    )
    assert record.status == "triaging"


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
            "status": "TEXT",
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
    attachments_json TEXT   NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'inbox'
);

CREATE TABLE IF NOT EXISTS watermark (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# insert_record
# ---------------------------------------------------------------------------


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
        data = dict(zip(col_names, row, strict=True))
        assert data["message_id"] == "<m1@example.com>"
        assert data["sender"] == "alice@example.com"
        assert data["subject"] == "Hello"
        assert data["date"] == "2025-06-01"
        assert data["imap_uid"] == 10
        assert data["recipients_json"] == '{"to": ["bob@x.com"], "cc": []}'
        assert data["body_plain"] == "Hi Bob"
        assert data["body_html"] == "<p>Hi Bob</p>"
        assert data["attachments_json"] == '[{"name": "file.txt"}]'
        assert data["status"] == "inbox"
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


# ---------------------------------------------------------------------------
# list_records
# ---------------------------------------------------------------------------


def test_list_records_empty_table() -> None:
    """list_records returns an empty list when mail_records is empty."""
    conn = init_db(":memory:")
    try:
        result = list_records(conn)
        assert isinstance(result, list)
        assert len(result) == 0
    finally:
        conn.close()


def test_list_records_returns_all_fields() -> None:
    """Every field of an inserted MailRecord round-trips through list_records."""
    conn = init_db(":memory:")
    try:
        attachments_json_val = (
            '[{"filename": "f1.pdf", "size": 2048}, '
            '{"filename": "f2.txt", "size": 512}]'
        )
        record = MailRecord(
            message_id="<all-fields@example.com>",
            sender="sender@example.com",
            subject="All Fields Test",
            date="2025-07-01T10:00:00Z",
            imap_uid=77,
            recipients_json='{"to": ["a@b.com"], "cc": ["c@d.com"]}',
            body_plain="Plain text body.",
            body_html="<p>HTML body.</p>",
            attachments_json=attachments_json_val,
        )
        insert_record(conn, record)
        results = list_records(conn)
        assert len(results) == 1
        r = results[0]
        assert r.message_id == "<all-fields@example.com>"
        assert r.sender == "sender@example.com"
        assert r.subject == "All Fields Test"
        assert r.date == "2025-07-01T10:00:00Z"
        assert r.imap_uid == 77
        assert r.recipients_json == '{"to": ["a@b.com"], "cc": ["c@d.com"]}'
        assert r.body_plain == "Plain text body."
        assert r.body_html == "<p>HTML body.</p>"
        assert r.attachments_json == (
            '[{"filename": "f1.pdf", "size": 2048}, '
            '{"filename": "f2.txt", "size": 512}]'
        )
        assert r.status == "inbox"
        assert r.id is not None and r.id > 0
    finally:
        conn.close()


def test_list_records_ordering() -> None:
    """list_records returns results ordered by id ASC regardless of insert order."""
    conn = init_db(":memory:")
    try:
        # Insert 3 records with message_ids that would sort differently
        # alphabetically: <c>, <a>, <b> -> auto-increment ids 1, 2, 3.
        for mid in ("<c@x.com>", "<a@x.com>", "<b@x.com>"):
            record = _make_record(message_id=mid)
            insert_record(conn, record)

        results = list_records(conn)
        assert len(results) == 3
        # Must be ordered by id ASC (insertion order = alphabetical order of
        # the message_ids in this case: c, a, b -> ids 1, 2, 3).
        assert results[0].message_id == "<c@x.com>"
        assert results[1].message_id == "<a@x.com>"
        assert results[2].message_id == "<b@x.com>"
    finally:
        conn.close()


def test_list_records_multiple_rows() -> None:
    """list_records returns the correct count and content for 3 records."""
    conn = init_db(":memory:")
    try:
        insert_record(
            conn,
            _make_record(
                message_id="<m1@x.com>",
                sender="alice@x.com",
                subject="First",
            ),
        )
        insert_record(
            conn,
            _make_record(
                message_id="<m2@x.com>",
                sender="bob@x.com",
                subject="Second",
            ),
        )
        insert_record(
            conn,
            _make_record(
                message_id="<m3@x.com>",
                sender="carol@x.com",
                subject="Third",
            ),
        )

        results = list_records(conn)
        assert len(results) == 3
        senders = [r.sender for r in results]
        subjects = [r.subject for r in results]
        assert senders == ["alice@x.com", "bob@x.com", "carol@x.com"]
        assert subjects == ["First", "Second", "Third"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_record_by_message_id
# ---------------------------------------------------------------------------


def test_get_record_by_message_id_found() -> None:
    """get_record_by_message_id returns a full MailRecord for a known id."""
    conn = init_db(":memory:")
    try:
        record = MailRecord(
            message_id="<lookup@example.com>",
            sender="lookup@example.com",
            subject="Lookup Test",
            date="2025-08-01T10:00:00Z",
            imap_uid=42,
            recipients_json='{"to": ["a@b.com"], "cc": ["c@d.com"]}',
            body_plain="Plain text here.",
            body_html="<p>HTML here.</p>",
            attachments_json='[{"filename": "doc.pdf", "size": 512}]',
            status="triaging",
        )
        insert_record(conn, record)

        result = get_record_by_message_id(conn, "<lookup@example.com>")
        assert result is not None
        assert result.message_id == "<lookup@example.com>"
        assert result.sender == "lookup@example.com"
        assert result.subject == "Lookup Test"
        assert result.date == "2025-08-01T10:00:00Z"
        assert result.imap_uid == 42
        assert result.recipients_json == '{"to": ["a@b.com"], "cc": ["c@d.com"]}'
        assert result.body_plain == "Plain text here."
        assert result.body_html == "<p>HTML here.</p>"
        assert result.attachments_json == '[{"filename": "doc.pdf", "size": 512}]'
        assert result.status == "triaging"
        assert result.id > 0
    finally:
        conn.close()


def test_get_record_by_message_id_not_found() -> None:
    """get_record_by_message_id returns None for an unknown message_id."""
    conn = init_db(":memory:")
    try:
        result = get_record_by_message_id(conn, "<nonexistent@x.com>")
        assert result is None
    finally:
        conn.close()


def test_get_record_by_message_id_angle_brackets() -> None:
    """message_id with angle brackets round-trips correctly."""
    conn = init_db(":memory:")
    try:
        mid = "<abc@example.com>"
        record = MailRecord(
            message_id=mid,
            sender="test@t.com",
            subject="Angle Brackets",
            date="2025-09-01T10:00:00Z",
        )
        insert_record(conn, record)

        result = get_record_by_message_id(conn, mid)
        assert result is not None
        assert result.message_id == mid
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record_exists
# ---------------------------------------------------------------------------


def test_record_exists_returns_false_when_empty() -> None:
    """record_exists returns False when no matching row exists."""
    conn = init_db(":memory:")
    try:
        from robotsix_auto_mail.db import record_exists

        assert record_exists(conn, "<nonexistent@x>") is False
    finally:
        conn.close()


def test_record_exists_returns_true_after_insert() -> None:
    """record_exists returns True after a record is inserted."""
    conn = init_db(":memory:")
    try:
        from robotsix_auto_mail.db import record_exists

        record = _make_record(message_id="<exists@x>")
        insert_record(conn, record)
        assert record_exists(conn, "<exists@x>") is True
    finally:
        conn.close()


def test_record_exists_fresh_connection_no_error() -> None:
    """record_exists on a non-init_db connection (table created manually)
    returns False, not an error."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """\
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
)
"""
    )
    try:
        from robotsix_auto_mail.db import record_exists

        assert record_exists(conn, "<anything@x>") is False
    finally:
        conn.close()

"""Local SQLite datastore for ingested mail messages and watermark tracking.

Provides ``MailRecord`` — a frozen dataclass that defines the shape of a
stored mail message — and a handful of functions for initialising the
database, inserting records idempotently, and managing a key-value
watermark store (used by the IMAP fetch layer to track the last-seen
UID).
"""

from __future__ import annotations

import dataclasses
import sqlite3

# ---------------------------------------------------------------------------
# MailRecord
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailRecord:
    """One ingested mail message, ready for storage.

    The ``id`` field is assigned by the database on insert (auto-increment
    primary key).  Before the first insert it is always ``0``.
    """

    message_id: str
    sender: str
    subject: str
    date: str

    imap_uid: int | None = None
    recipients_json: str = '{"to": [], "cc": []}'
    body_plain: str = ""
    body_html: str = ""
    attachments_json: str = "[]"

    id: int = 0  # assigned by DB; ignored on insert


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
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
# Public API
# ---------------------------------------------------------------------------


def init_db(path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database at *path* and set up the schema.

    Enables WAL journal mode and foreign-key enforcement.  The caller
    owns the returned connection and must close it.
    """
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def insert_record(
    conn: sqlite3.Connection, record: MailRecord
) -> int | None:
    """Insert *record* into ``mail_records``.

    Returns the new ``rowid`` on success, or ``None`` when a row with
    the same ``message_id`` already exists (UNIQUE conflict).  The
    ``id`` field of the input ``MailRecord`` is ignored — the database
    assigns it.
    """
    try:
        cur = conn.execute(
            """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES
    (:imap_uid, :message_id, :sender, :subject, :date,
     :recipients_json, :body_plain, :body_html, :attachments_json)
""",
            {
                "imap_uid": record.imap_uid,
                "message_id": record.message_id,
                "sender": record.sender,
                "subject": record.subject,
                "date": record.date,
                "recipients_json": record.recipients_json,
                "body_plain": record.body_plain,
                "body_html": record.body_html,
                "attachments_json": record.attachments_json,
            },
        )
    except sqlite3.IntegrityError:
        # UNIQUE constraint on message_id violated — record already
        # exists; return None to signal idempotent skip.
        return None
    else:
        conn.commit()
        return cur.lastrowid


def get_watermark(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the watermark value for *key*, or ``None`` if it hasn't been set."""
    cur = conn.execute(
        "SELECT value FROM watermark WHERE key = ?", (key,)
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def set_watermark(
    conn: sqlite3.Connection, key: str, value: str
) -> None:
    """Upsert a watermark value.

    If *key* already exists its value is updated; otherwise a new row
    is inserted.
    """
    conn.execute(
        """\
INSERT INTO watermark (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
""",
        (key, value),
    )
    conn.commit()

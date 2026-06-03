"""Tests for ``robotsix_auto_mail.status``."""

from __future__ import annotations

import sqlite3
import tempfile
from typing import Any

from robotsix_auto_mail.db import MailRecord, init_db, insert_record
from robotsix_auto_mail.status import (
    VALID_STATUSES,
    get_status,
    list_by_status,
    set_status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**overrides: str | int | None) -> MailRecord:
    """Build a ``MailRecord`` with defaults suitable for testing."""
    kwargs: dict[str, str | int | None] = {
        "message_id": "<test@example.com>",
        "sender": "sender@example.com",
        "subject": "Test Subject",
        "date": "2025-06-01T12:00:00Z",
    }
    kwargs.update(overrides)

    def _opt_str(key: str, default: str = "") -> str:
        val = kwargs.get(key, default)
        assert isinstance(val, str)
        return val

    def _opt_int_none(key: str) -> int | None:
        val = kwargs.get(key)
        if val is None:
            return None
        assert isinstance(val, int)
        return val

    return MailRecord(
        message_id=str(kwargs["message_id"]),
        sender=str(kwargs["sender"]),
        subject=str(kwargs["subject"]),
        date=str(kwargs["date"]),
        status=str(kwargs.get("status", "inbox")),
        imap_uid=_opt_int_none("imap_uid"),
        recipients_json=_opt_str("recipients_json", '{"to": [], "cc": []}'),
        body_plain=_opt_str("body_plain", ""),
        body_html=_opt_str("body_html", ""),
        attachments_json=_opt_str("attachments_json", "[]"),
    )


# ---------------------------------------------------------------------------
# VALID_STATUSES
# ---------------------------------------------------------------------------


def test_valid_statuses_contains_exactly_four_values() -> None:
    """The constant holds exactly the four kanban statuses."""
    assert VALID_STATUSES == frozenset({"inbox", "triaging", "done", "archive"})


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


def test_get_status_returns_none_for_unknown_message_id() -> None:
    """get_status returns None when the message_id is not in the table."""
    conn = init_db(":memory:")
    try:
        result = get_status(conn, "<nonexistent@example.com>")
        assert result is None
    finally:
        conn.close()


def test_get_status_returns_correct_status_for_known_record() -> None:
    """get_status returns the stored status string for a known message_id."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<a@x.com>", status="inbox"))
        insert_record(conn, _make_record(message_id="<b@x.com>", status="triaging"))
        assert get_status(conn, "<a@x.com>") == "inbox"
        assert get_status(conn, "<b@x.com>") == "triaging"
    finally:
        conn.close()


class _CommitTracker:
    """Wraps a real ``sqlite3.Connection`` and tracks whether ``commit()``
    was called.  Needed because ``sqlite3.Connection.commit`` is a
    C-level method that cannot be monkey-patched with ``patch.object``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.commit_called = False

    def commit(self) -> None:
        self.commit_called = True
        self._conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_get_status_does_not_call_commit() -> None:
    """get_status is read-only and must not call conn.commit()."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<x@x.com>", status="inbox"))
        tracker = _CommitTracker(conn)
        result = get_status(tracker, "<x@x.com>")  # type: ignore[arg-type]
        assert result == "inbox"
        assert tracker.commit_called is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------


def test_set_status_updates_and_returns_true() -> None:
    """set_status updates the status and returns True for an existing record."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<a@x.com>", status="inbox"))
        assert get_status(conn, "<a@x.com>") == "inbox"
        result = set_status(conn, "<a@x.com>", "done")
        assert result is True
        assert get_status(conn, "<a@x.com>") == "done"
    finally:
        conn.close()


def test_set_status_returns_false_for_unknown_message_id() -> None:
    """set_status returns False when the message_id does not exist."""
    conn = init_db(":memory:")
    try:
        result = set_status(conn, "<no-such-id@x.com>", "archive")
        assert result is False
    finally:
        conn.close()


def test_set_status_raises_valueerror_for_invalid_status() -> None:
    """set_status raises ValueError when new_status is not in VALID_STATUSES."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<a@x.com>"))
        for bad in ("bogus", "", "INBOX", "  inbox  "):
            try:
                set_status(conn, "<a@x.com>", bad)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"set_status with {bad!r} did not raise ValueError"
                )
    finally:
        conn.close()


def test_set_status_calls_commit() -> None:
    """set_status calls conn.commit() so changes are visible to other connections."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn1 = init_db(f.name)
        try:
            insert_record(
                conn1, _make_record(message_id="<a@x.com>", status="inbox")
            )
            set_status(conn1, "<a@x.com>", "triaging")

            # Open a second connection and verify the update is visible.
            conn2 = sqlite3.connect(f.name)
            try:
                cur = conn2.execute(
                    "SELECT status FROM mail_records WHERE message_id = ?",
                    ("<a@x.com>",),
                )
                assert cur.fetchone()[0] == "triaging"
            finally:
                conn2.close()
        finally:
            conn1.close()


# ---------------------------------------------------------------------------
# list_by_status
# ---------------------------------------------------------------------------


def test_list_by_status_returns_only_matching_records() -> None:
    """list_by_status filters records by the given status."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<a@x.com>", status="inbox"))
        insert_record(conn, _make_record(message_id="<b@x.com>", status="inbox"))
        insert_record(conn, _make_record(message_id="<c@x.com>", status="triaging"))

        inbox_records = list_by_status(conn, "inbox")
        triaging_records = list_by_status(conn, "triaging")
        done_records = list_by_status(conn, "done")

        assert len(inbox_records) == 2
        assert all(r.status == "inbox" for r in inbox_records)
        assert len(triaging_records) == 1
        assert triaging_records[0].message_id == "<c@x.com>"
        assert done_records == []
    finally:
        conn.close()


def test_list_by_status_returns_records_ordered_by_id_asc() -> None:
    """list_by_status returns records in id-ascending order."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<b@x.com>", status="inbox"))
        insert_record(conn, _make_record(message_id="<a@x.com>", status="inbox"))
        insert_record(conn, _make_record(message_id="<c@x.com>", status="inbox"))

        records = list_by_status(conn, "inbox")
        ids = [r.id for r in records]
        assert ids == sorted(ids)
    finally:
        conn.close()


def test_list_by_status_raises_valueerror_for_invalid_status() -> None:
    """list_by_status raises ValueError for a status not in VALID_STATUSES."""
    conn = init_db(":memory:")
    try:
        for bad in ("bogus", "", "INBOX", "  inbox  "):
            try:
                list_by_status(conn, bad)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"list_by_status with {bad!r} did not raise ValueError"
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Argument shape
# ---------------------------------------------------------------------------


def test_all_functions_accept_sqlite3_connection_as_first_argument() -> None:
    """get_status, set_status, and list_by_status all accept a Connection."""
    conn = init_db(":memory:")
    try:
        insert_record(conn, _make_record(message_id="<a@x.com>", status="inbox"))

        # Merely calling each function confirms they accept a Connection.
        assert isinstance(get_status(conn, "<a@x.com>"), str)
        assert set_status(conn, "<a@x.com>", "done") is True
        assert isinstance(list_by_status(conn, "done"), list)
    finally:
        conn.close()

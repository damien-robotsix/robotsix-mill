"""Tests for the ingestion pipeline (pipeline.py)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from unittest import mock

import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.db import MailRecord, get_watermark, init_db
from robotsix_auto_mail.fetch import update_watermark as fetch_update_watermark
from robotsix_auto_mail.imap import ImapClient
from robotsix_auto_mail.parser import ParseError
from robotsix_auto_mail.pipeline import (
    IngestError,
    IngestResult,
    ingest_mail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> MailConfig:
    return MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )


@pytest.fixture
def conn() -> Generator[sqlite3.Connection, None, None]:
    c = init_db(":memory:")
    yield c
    c.close()


def _mock_imap_client() -> mock.MagicMock:
    """Return a MagicMock that looks enough like an ImapClient."""
    return mock.MagicMock(spec=ImapClient)


def _make_raw_message(
    *,
    message_id: str = "<abc123@example.com>",
    sender: str = "alice@example.com",
    subject: str = "Hello",
    date: str = "Wed, 15 Jan 2025 10:30:00 +0000",
    body: str = "plain text body",
) -> bytes:
    """Build a minimal, valid MIME message as bytes."""
    return (
        f"From: {sender}\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# IngestError / IngestResult dataclass tests
# ---------------------------------------------------------------------------


def test_ingest_error_is_frozen() -> None:
    err = IngestError(uid=1, message_id="<x@y>", error="boom")
    assert err.uid == 1
    assert err.message_id == "<x@y>"
    assert err.error == "boom"
    with pytest.raises(Exception):
        err.uid = 2  # type: ignore[misc]


def test_ingest_error_empty_message_id() -> None:
    err = IngestError(uid=5, message_id="", error="parse failed")
    assert err.message_id == ""


def test_ingest_result_is_frozen() -> None:
    result = IngestResult(
        total_fetched=3, stored=2, skipped=1, errors=[]
    )
    assert result.total_fetched == 3
    assert result.stored == 2
    assert result.skipped == 1
    assert result.errors == []
    with pytest.raises(Exception):
        result.stored = 99  # type: ignore[misc]


def test_ingest_result_defaults() -> None:
    result = IngestResult(
        total_fetched=0, stored=0, skipped=0, errors=[]
    )
    assert result.total_fetched == 0
    assert result.errors == []


# ---------------------------------------------------------------------------
# ingest_mail – happy path (acceptance criterion 1)
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_stores_three_messages_and_updates_watermark(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """3 raw messages → stored=3, skipped=0, errors=[], watermark=max uid."""
    mock_fetch.return_value = [
        (1, _make_raw_message(message_id="<a@x>", subject="One")),
        (3, _make_raw_message(message_id="<b@x>", subject="Two")),
        (5, _make_raw_message(message_id="<c@x>", subject="Three")),
    ]
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 3
    assert result.stored == 3
    assert result.skipped == 0
    assert result.errors == []

    # Watermark must be the max UID (5).
    assert get_watermark(conn, "imap_uid") == "5"

    # All three rows should be in the DB.
    cur = conn.execute("SELECT COUNT(*) FROM mail_records")
    assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# ingest_mail – idempotency (acceptance criterion 2)
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_idempotent_second_run_skips_all(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """First run stores 3; second run (same data) stores 0, skips 3."""
    messages = [
        (1, _make_raw_message(message_id="<a@x>")),
        (2, _make_raw_message(message_id="<b@x>")),
        (3, _make_raw_message(message_id="<c@x>")),
    ]
    imap = _mock_imap_client()

    # First run.
    mock_fetch.return_value = messages
    r1 = ingest_mail(conn, imap, cfg)
    assert r1.stored == 3
    assert r1.skipped == 0
    assert get_watermark(conn, "imap_uid") == "3"

    # Second run — same data returned by fetch (simulating crash before
    # watermark update on first run, or just testing idempotency).
    r2 = ingest_mail(conn, imap, cfg)
    assert r2.stored == 0
    assert r2.skipped == 3
    # Watermark still 3 (re-updated to same value).
    assert get_watermark(conn, "imap_uid") == "3"

    # Only 3 rows total.
    cur = conn.execute("SELECT COUNT(*) FROM mail_records")
    assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# ingest_mail – partial parse failure (acceptance criterion 3)
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
@mock.patch("robotsix_auto_mail.pipeline.parse_message")
def test_ingest_partial_parse_failure(
    mock_parse: mock.MagicMock,
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """5 messages, message #3 fails parse → stored=4, errors=1."""
    # Build real records for the good messages.
    r1 = _make_raw_message(message_id="<m1@x>")
    r2 = _make_raw_message(message_id="<m2@x>")
    r3 = b"garbage"  # will be intercepted by mock
    r4 = _make_raw_message(message_id="<m4@x>")
    r5 = _make_raw_message(message_id="<m5@x>")

    mock_fetch.return_value = [(1, r1), (2, r2), (3, r3), (4, r4), (5, r5)]

    # Let the real parse_message handle good messages; only fail on UID 3.
    from robotsix_auto_mail.parser import parse_message as real_parse

    def side_effect(raw_bytes: bytes, *, imap_uid: int | None = None) -> MailRecord:
        if raw_bytes == r3:
            raise ParseError("failed to parse raw bytes as MIME message")
        return real_parse(raw_bytes, imap_uid=imap_uid)

    mock_parse.side_effect = side_effect

    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 5
    assert result.stored == 4
    assert result.skipped == 0
    assert len(result.errors) == 1

    err = result.errors[0]
    assert err.uid == 3
    assert err.message_id == ""
    assert "failed to parse" in err.error

    # Watermark advances past the failed UID.
    assert get_watermark(conn, "imap_uid") == "5"

    # Verify the stored messages match expected.
    cur = conn.execute("SELECT imap_uid FROM mail_records ORDER BY imap_uid")
    stored_uids = [row[0] for row in cur.fetchall()]
    assert stored_uids == [1, 2, 4, 5]


# ---------------------------------------------------------------------------
# ingest_mail – crash simulation (acceptance criterion 4)
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_crash_before_watermark_no_duplicates(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Simulate crash by calling pipeline, then re-calling with same data."""
    messages = [
        (10, _make_raw_message(message_id="<dup1@x>")),
        (11, _make_raw_message(message_id="<dup2@x>")),
    ]
    imap = _mock_imap_client()

    # "Crash" scenario: store messages but don't update watermark.
    # We simulate this by calling ingest_mail with a patched
    # update_watermark that is a no-op on the first call.
    with mock.patch(
        "robotsix_auto_mail.pipeline.update_watermark"
    ) as mock_update:
        # First: update_watermark does nothing (crash simulation).
        mock_update.side_effect = lambda c, u: None

        mock_fetch.return_value = messages
        r1 = ingest_mail(conn, imap, cfg)
        assert r1.stored == 2
        assert r1.skipped == 0
        # Watermark was not persisted.
        assert get_watermark(conn, "imap_uid") is None

    # "Re-run" after crash: same fetch result, watermark still None.
    mock_fetch.return_value = messages
    r2 = ingest_mail(conn, imap, cfg)
    assert r2.stored == 0
    assert r2.skipped == 2
    assert get_watermark(conn, "imap_uid") == "11"

    # No duplicate rows.
    cur = conn.execute("SELECT COUNT(*) FROM mail_records")
    assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# ingest_mail – empty batch
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_empty_batch(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Empty fetch → all zeros, watermark untouched."""
    mock_fetch.return_value = []
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 0
    assert result.stored == 0
    assert result.skipped == 0
    assert result.errors == []

    # Watermark unchanged (was never set).
    assert get_watermark(conn, "imap_uid") is None


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_empty_batch_does_not_touch_existing_watermark(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Empty batch leaves an existing watermark alone."""
    fetch_update_watermark(conn, 42)

    mock_fetch.return_value = []
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 0
    assert get_watermark(conn, "imap_uid") == "42"


# ---------------------------------------------------------------------------
# ingest_mail – DB insert failure
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
@mock.patch("robotsix_auto_mail.pipeline.insert_record")
def test_ingest_insert_failure_is_collected(
    mock_insert: mock.MagicMock,
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """insert_record raises an exception → error collected, others still stored."""
    msg_ok1 = _make_raw_message(message_id="<ok1@x>")
    msg_bad = _make_raw_message(message_id="<bad@x>")
    msg_ok2 = _make_raw_message(message_id="<ok2@x>")

    mock_fetch.return_value = [(1, msg_ok1), (2, msg_bad), (3, msg_ok2)]
    imap = _mock_imap_client()

    # Only the middle insert fails.
    def side_effect(c: sqlite3.Connection, r: MailRecord) -> int | None:
        if r.message_id == "<bad@x>":
            raise sqlite3.DatabaseError("disk I/O error")
        # Use the real insert_record.
        from robotsix_auto_mail.db import insert_record as real_insert

        return real_insert(c, r)

    mock_insert.side_effect = side_effect

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 3
    assert result.stored == 2
    assert result.skipped == 0
    assert len(result.errors) == 1
    assert result.errors[0].uid == 2
    assert result.errors[0].message_id == "<bad@x>"
    assert "disk I/O error" in result.errors[0].error

    # Watermark still advances.
    assert get_watermark(conn, "imap_uid") == "3"


# ---------------------------------------------------------------------------
# ingest_mail – record_exists dance
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_record_exists_skips(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Pre-populate DB with a message, then re-feed it — counted as skipped."""
    # Pre-populate one message directly.
    from robotsix_auto_mail.db import insert_record

    rec = MailRecord(
        message_id="<existing@x>",
        sender="alice@x.com",
        subject="Old",
        date="2025-01-01T00:00:00",
        imap_uid=5,
    )
    insert_record(conn, rec)

    # Now feed two messages — one duplicate, one new.
    mock_fetch.return_value = [
        (6, _make_raw_message(message_id="<existing@x>")),
        (7, _make_raw_message(message_id="<new@x>")),
    ]
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 2
    assert result.stored == 1
    assert result.skipped == 1
    assert result.errors == []

    # Only 2 total rows (the pre-existing one + the new one).
    cur = conn.execute("SELECT COUNT(*) FROM mail_records")
    assert cur.fetchone()[0] == 2

    # Watermark at max UID (7).
    assert get_watermark(conn, "imap_uid") == "7"


# ---------------------------------------------------------------------------
# ingest_mail – watermark advances to max UID in batch
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
def test_ingest_watermark_advances_to_max_uid(
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Watermark is set to the highest UID in the batch, even with skips."""
    # Pre-populate to make some messages get skipped.
    from robotsix_auto_mail.db import insert_record

    rec = MailRecord(
        message_id="<skip@x>",
        sender="a@x.com",
        subject="Skip",
        date="2025-01-01",
        imap_uid=44,
    )
    insert_record(conn, rec)

    mock_fetch.return_value = [
        (42, _make_raw_message(message_id="<m42@x>")),
        (44, _make_raw_message(message_id="<skip@x>")),  # will be skipped
        (45, _make_raw_message(message_id="<m45@x>")),
    ]
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.stored == 2
    assert result.skipped == 1
    # Watermark is 45, not 42 or 44.
    assert get_watermark(conn, "imap_uid") == "45"


# ---------------------------------------------------------------------------
# ingest_mail – ParseError with non-empty message
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
@mock.patch("robotsix_auto_mail.pipeline.parse_message")
def test_ingest_parse_error_message(
    mock_parse: mock.MagicMock,
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """ParseError returns a human-readable error string."""
    mock_fetch.return_value = [(1, b"valid raw bytes")]
    mock_parse.side_effect = ParseError("failed to parse raw bytes as MIME message")
    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert len(result.errors) == 1
    assert "failed to parse" in result.errors[0].error


# ---------------------------------------------------------------------------
# ingest_mail – mixing stored, skipped, errors in one batch
# ---------------------------------------------------------------------------


@mock.patch("robotsix_auto_mail.pipeline.fetch_new_messages")
@mock.patch("robotsix_auto_mail.pipeline.parse_message")
def test_ingest_mixed_store_skip_error(
    mock_parse: mock.MagicMock,
    mock_fetch: mock.MagicMock,
    conn: sqlite3.Connection,
    cfg: MailConfig,
) -> None:
    """Batch with new, duplicate, and unparseable messages."""
    # Pre-populate one.
    from robotsix_auto_mail.db import insert_record

    rec = MailRecord(
        message_id="<dup@x>",
        sender="s@x.com",
        subject="Dup",
        date="2025-01-01",
    )
    insert_record(conn, rec)

    r10 = _make_raw_message(message_id="<new@x>")
    r11 = _make_raw_message(message_id="<dup@x>")  # duplicate
    r12 = b"garbage bytes not mime at all"
    r13 = _make_raw_message(message_id="<new2@x>")

    mock_fetch.return_value = [(10, r10), (11, r11), (12, r12), (13, r13)]

    from robotsix_auto_mail.parser import parse_message as real_parse

    def side_effect(raw_bytes: bytes, *, imap_uid: int | None = None) -> MailRecord:
        if raw_bytes == r12:
            raise ParseError("failed to parse raw bytes as MIME message")
        return real_parse(raw_bytes, imap_uid=imap_uid)

    mock_parse.side_effect = side_effect

    imap = _mock_imap_client()

    result = ingest_mail(conn, imap, cfg)

    assert result.total_fetched == 4
    assert result.stored == 2
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert result.errors[0].uid == 12
    assert get_watermark(conn, "imap_uid") == "13"


# ---------------------------------------------------------------------------
# Module boundary tests
# ---------------------------------------------------------------------------


def test_pipeline_imports_from_expected_modules() -> None:
    """pipeline.py imports from db, fetch, imap, parser, config."""
    import robotsix_auto_mail.pipeline as mod

    source = mod.__file__
    assert source is not None
    content = open(source).read()
    assert "from robotsix_auto_mail.db import" in content
    assert "from robotsix_auto_mail.fetch import" in content
    assert "from robotsix_auto_mail.imap import" in content
    assert "from robotsix_auto_mail.parser import" in content
    assert "from robotsix_auto_mail.config import" in content
    # Must not import smtp_client.
    assert "smtp_client" not in content


# ---------------------------------------------------------------------------
# CLI ingest subcommand tests
# ---------------------------------------------------------------------------


def test_cli_ingest_subcommand_in_parser() -> None:
    """build_parser includes the ingest subcommand."""
    from robotsix_auto_mail.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["ingest"])
    assert args.command == "ingest"


def test_cli_ingest_rejects_extra_args() -> None:
    """ingest rejects extra arguments."""
    from robotsix_auto_mail.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "--foo"])


@pytest.fixture
def env_cfg_ingest() -> MailConfig:
    return MailConfig(
        imap_host="imap.example.com",
        imap_port=993,
        imap_tls_mode="direct-tls",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_tls_mode="starttls",
        username="user@example.com",
        password="s3cret",
        db_path=":memory:",
    )


@mock.patch("robotsix_auto_mail.cli.ImapClient")
@mock.patch("robotsix_auto_mail.cli.init_db")
@mock.patch(
    "robotsix_auto_mail.config.MailConfig.from_env",
)
def test_cli_ingest_success(
    mock_from_env: mock.MagicMock,
    mock_init_db: mock.MagicMock,
    mock_imap_cls: mock.MagicMock,
    env_cfg_ingest: MailConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ingest subcommand prints summary and exits 1 when errors are present."""
    mock_from_env.return_value = env_cfg_ingest

    # Set up an in-memory DB for init_db.
    db = init_db(":memory:")
    mock_init_db.return_value = db

    # Mock ImapClient context manager.
    mock_imap = mock.MagicMock(spec=ImapClient)
    mock_imap_cls.return_value.__enter__.return_value = mock_imap

    # Mock ingest_mail return.
    with mock.patch(
        "robotsix_auto_mail.cli.ingest_mail"
    ) as mock_ingest:
        mock_ingest.return_value = IngestResult(
            total_fetched=12,
            stored=10,
            skipped=1,
            errors=[
                IngestError(
                    uid=42,
                    message_id="<msg-id@example.com>",
                    error="failed to parse raw bytes as MIME message",
                ),
            ],
        )

        from robotsix_auto_mail.cli import main

        rc = main(["ingest"])

    db.close()

    assert rc == 1  # errors present → exit 1

    captured = capsys.readouterr()
    out = captured.out

    assert "Fetched: 12 messages" in out
    assert "Stored:  10 new" in out
    assert "Skipped:  1 duplicate" in out
    assert "Errors:   1" in out
    assert "UID 42 (<msg-id@example.com>)" in out
    assert "failed to parse raw bytes as MIME message" in out


@mock.patch("robotsix_auto_mail.cli.ImapClient")
@mock.patch("robotsix_auto_mail.cli.init_db")
@mock.patch(
    "robotsix_auto_mail.config.MailConfig.from_env",
)
def test_cli_ingest_success_no_errors(
    mock_from_env: mock.MagicMock,
    mock_init_db: mock.MagicMock,
    mock_imap_cls: mock.MagicMock,
    env_cfg_ingest: MailConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ingest subcommand exits 0 when there are no errors."""
    mock_from_env.return_value = env_cfg_ingest

    db = init_db(":memory:")
    mock_init_db.return_value = db

    mock_imap = mock.MagicMock(spec=ImapClient)
    mock_imap_cls.return_value.__enter__.return_value = mock_imap

    with mock.patch(
        "robotsix_auto_mail.cli.ingest_mail"
    ) as mock_ingest:
        mock_ingest.return_value = IngestResult(
            total_fetched=5,
            stored=5,
            skipped=0,
            errors=[],
        )

        from robotsix_auto_mail.cli import main

        rc = main(["ingest"])

    db.close()

    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "Fetched:  5 messages" in out
    assert "Stored:   5 new" in out
    assert "Errors:   0" in out


@mock.patch("robotsix_auto_mail.config.MailConfig.from_env")
def test_cli_ingest_config_load_failure(
    mock_from_env: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ingest returns 1 when config loading fails."""
    mock_from_env.side_effect = RuntimeError("boom")

    from robotsix_auto_mail.cli import main

    rc = main(["ingest"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error loading configuration" in err
    assert "boom" in err
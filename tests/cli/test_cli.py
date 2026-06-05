"""Tests for the CLI module."""

from __future__ import annotations

import builtins
import imaplib
import json
import os
import smtplib
import ssl
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from robotsix_auto_mail.cli import build_parser, main
from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.config_sync import (
    ConfigSyncError,
    ConfigSyncResult,
    DriftProposal,
)
from robotsix_auto_mail.detect import DetectionError, MailProvider
from robotsix_auto_mail.imap import ImapClient
from robotsix_auto_mail.smtp_client import SmtpClient
from robotsix_auto_mail.triage import (
    TriageError,
    TriageItem,
    TriageResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------



def _make_mock_imap_ssl() -> mock.MagicMock:
    m = mock.MagicMock(spec=imaplib.IMAP4_SSL)
    m.welcome = b"* OK IMAP4 ready"
    m.capabilities = ("IMAP4rev1", "STARTTLS", "AUTH=PLAIN")
    m.login.return_value = ("OK", [b"Logged in"])
    m.list.return_value = (
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
        ],
    )
    m.select.return_value = ("OK", [b"5"])
    m.logout.return_value = ("OK", [b"Logged out"])
    m.sock = mock.MagicMock()
    return m


def _make_mock_smtp() -> mock.MagicMock:
    m = mock.MagicMock(spec=smtplib.SMTP)
    m.ehlo_resp = b"250-smtp.example.com\n250 STARTTLS"
    m.esmtp_features = {"STARTTLS": "", "AUTH": "PLAIN LOGIN"}
    m.login.return_value = (235, b"2.7.0 Authentication successful")
    m.send_message.return_value = {}
    m.noop.return_value = (250, b"OK")
    return m


# ---------------------------------------------------------------------------
# ImapClient / SmtpClient property defaults
# ---------------------------------------------------------------------------


def test_imap_client_properties_before_connect(cfg: MailConfig) -> None:
    """server_greeting / capabilities return safe defaults when not connected."""
    client = ImapClient(cfg)
    assert client.server_greeting is None
    assert client.capabilities == ()


def test_smtp_client_properties_before_connect(cfg: MailConfig) -> None:
    """ehlo_response / esmtp_features return safe defaults when not connected."""
    client = SmtpClient(cfg)
    assert client.ehlo_response is None
    assert client.esmtp_features == {}


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints the version and exits."""
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "0.0.0" in captured.out


# ---------------------------------------------------------------------------
# probe - success
# ---------------------------------------------------------------------------


def test_probe_success(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe exits 0 and prints IMAP + SMTP metadata when both succeed."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    # IMAP output
    assert "IMAP Probe" in out
    assert "* OK IMAP4 ready" in out
    assert "IMAP4rev1" in out
    assert "INBOX" in out
    assert "[Gmail]" in out

    # SMTP output
    assert "SMTP Probe" in out
    assert "250-smtp.example.com" in out
    assert "STARTTLS" in out
    assert "AUTH" in out

    # No errors on stderr
    assert err == ""


# ---------------------------------------------------------------------------
# probe - IMAP failure, SMTP succeeds
# ---------------------------------------------------------------------------


def test_probe_imap_failure_smtp_ok(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """When IMAP fails, SMTP is still probed and exit code is 1."""
    mock_imap = mock.MagicMock(spec=imaplib.IMAP4_SSL)
    mock_imap.login.side_effect = imaplib.IMAP4.error(
        "AUTHENTICATIONFAILED"
    )
    mock_imap.sock = mock.MagicMock()

    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    # SMTP probe still ran
    assert "SMTP Probe" in out
    assert "250-smtp.example.com" in out

    # IMAP error on stderr
    assert "Error:" in err
    assert "AUTHENTICATIONFAILED" in err


# ---------------------------------------------------------------------------
# probe - SMTP failure, IMAP succeeds
# ---------------------------------------------------------------------------


def test_probe_smtp_failure_imap_ok(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """When SMTP fails, IMAP is still probed and exit code is 1."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = mock.MagicMock(spec=smtplib.SMTP)
    mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Authentication failed"
    )

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    # IMAP probe still ran
    assert "IMAP Probe" in out
    assert "INBOX" in out

    # SMTP error on stderr
    assert "Error:" in err
    assert "Authentication failed" in err


# ---------------------------------------------------------------------------
# probe - both fail
# ---------------------------------------------------------------------------


def test_probe_both_fail(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """When both fail, exit code is 1 and both errors are reported."""
    mock_imap = mock.MagicMock(spec=imaplib.IMAP4_SSL)
    mock_imap.login.side_effect = imaplib.IMAP4.error("BAD")
    mock_imap.sock = mock.MagicMock()

    mock_smtp = mock.MagicMock(spec=smtplib.SMTP)
    mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"bad creds"
    )

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    # Both errors reported
    assert err.count("Error:") == 2


# ---------------------------------------------------------------------------
# probe - never calls send_message
# ---------------------------------------------------------------------------


def test_probe_never_calls_send_message(
    cfg: MailConfig,
) -> None:
    """The SMTP mock's send_message is never called."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        main(["probe"])

    mock_smtp.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# probe - connection refusal for IMAP
# ---------------------------------------------------------------------------


def test_probe_imap_connection_refused(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles IMAP connection-refused gracefully."""
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL",
        side_effect=ConnectionRefusedError("Connection refused"),
    ), mock.patch("smtplib.SMTP", return_value=mock_smtp), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Connection refused" in err


# ---------------------------------------------------------------------------
# probe - connection refusal for SMTP
# ---------------------------------------------------------------------------


def test_probe_smtp_connection_refused(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles SMTP connection-refused gracefully."""
    mock_imap = _make_mock_imap_ssl()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP",
        side_effect=ConnectionRefusedError("Connection refused"),
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Connection refused" in err


# ---------------------------------------------------------------------------
# probe - TLS failure for IMAP
# ---------------------------------------------------------------------------


def test_probe_imap_tls_failure(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles IMAP TLS failure gracefully (for STARTTLS)."""
    # Use a config with starttls so we can inject a TLS error
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_tls_mode="starttls",
        username="user@example.com",
        password="s3cret",
    )

    mock_imap = mock.MagicMock(spec=imaplib.IMAP4)
    mock_imap.starttls.side_effect = ssl.SSLError("handshake failed")
    mock_imap.sock = mock.MagicMock()

    mock_smtp = _make_mock_smtp()

    with mock.patch("imaplib.IMAP4", return_value=mock_imap), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "handshake" in err.lower()


# ---------------------------------------------------------------------------
# probe - SMTP STARTTLS failure
# ---------------------------------------------------------------------------


def test_probe_smtp_tls_failure(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles SMTP TLS failure gracefully."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = mock.MagicMock(spec=smtplib.SMTP)
    mock_smtp.ehlo_or_helo_if_needed.return_value = (250, b"OK")
    mock_smtp.starttls.side_effect = ssl.SSLError("certificate verify failed")

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch("smtplib.SMTP", return_value=mock_smtp), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "STARTTLS" in err or "certificate" in err


# ---------------------------------------------------------------------------
# probe - IMAP authentication failure
# ---------------------------------------------------------------------------


def test_probe_imap_auth_failure(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles IMAP authentication failure gracefully."""
    mock_imap = mock.MagicMock(spec=imaplib.IMAP4_SSL)
    mock_imap.login.side_effect = imaplib.IMAP4.error(
        "AUTHENTICATIONFAILED invalid credentials"
    )
    mock_imap.sock = mock.MagicMock()

    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Authentication failed" in err


# ---------------------------------------------------------------------------
# Config loading failure
# ---------------------------------------------------------------------------


def test_probe_config_load_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """probe exits with code 1 when config loading fails."""
    with mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(SystemExit) as exc:
            main(["probe"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Error loading configuration" in err
    assert "boom" in err


# ---------------------------------------------------------------------------
# Parser shape
# ---------------------------------------------------------------------------


def test_parser_has_version() -> None:
    """The parser accepts --version."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0


def test_parser_has_probe_subcommand() -> None:
    """The parser knows the probe subcommand."""
    parser = build_parser()
    args = parser.parse_args(["probe"])
    assert args.command == "probe"


def test_probe_takes_no_extra_args() -> None:
    """probe rejects extra arguments."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["probe", "--foo"])


def test_no_subcommand_prints_help_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calling main() with no subcommand prints help to stderr and exits 1."""
    # Need to patch load() to avoid a real config call, but we want to
    # ensure we reach the dispatch.  With no command, we won't hit load().
    rc = main([])
    assert rc == 1
    # help goes to stderr
    captured = capsys.readouterr()
    assert "usage:" in captured.err.lower() or "usage:" in captured.out.lower()


# ---------------------------------------------------------------------------
# SmtpClient / ImapClient properties after connect
# ---------------------------------------------------------------------------


def test_smtp_client_properties_after_connect(cfg: MailConfig) -> None:
    """ehlo_response / esmtp_features reflect the mock after connect."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with SmtpClient(cfg) as client:
            assert client.ehlo_response == b"250-smtp.example.com\n250 STARTTLS"
            assert client.esmtp_features == {
                "STARTTLS": "",
                "AUTH": "PLAIN LOGIN",
            }


def test_imap_client_properties_after_connect(cfg: MailConfig) -> None:
    """server_greeting / capabilities reflect the mock after connect."""
    mock_imap = _make_mock_imap_ssl()

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        with ImapClient(cfg) as client:
            assert client.server_greeting == b"* OK IMAP4 ready"
            assert client.capabilities == (
                "IMAP4rev1",
                "STARTTLS",
                "AUTH=PLAIN",
            )


# ---------------------------------------------------------------------------
# board subcommand
# ---------------------------------------------------------------------------


def test_parser_has_board_subcommand() -> None:
    """The parser knows the board subcommand."""
    parser = build_parser()
    args = parser.parse_args(["board"])
    assert args.command == "board"


def test_board_takes_no_extra_args() -> None:
    """board rejects extra arguments."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["board", "--foo"])


def test_board_empty_inbox(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board prints a friendly message when the database is empty."""
    from robotsix_auto_mail.db import init_db as real_init_db

    conn = real_init_db(":memory:")  # schema lives in db.py — no DDL duplication
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        rc = main(["board"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "Inbox" in captured.out
    assert "Your inbox is empty." in captured.out
    # No card-like content should appear
    assert "From:" not in captured.out
    # The header emits one 60-dash line; there should be no second one
    # (no card separator).
    assert captured.out.count("-" * 60) == 1


def test_board_with_records(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board prints cards with sender, subject, date, body preview and count."""
    from robotsix_auto_mail.db import init_db as real_init_db

    conn = real_init_db(":memory:")  # schema lives in db.py — no DDL duplication
    conn.execute(
        """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        (
            1, "<a@x.com>", "alice@example.com", "Hello",
            "2025-06-01T14:30:00", '{"to":[],"cc":[]}',
            "Just checking in!", "", "[]",
        ),
    )
    conn.execute(
        """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        (
            2, "<b@x.com>", "bob@example.com", "Hi",
            "2025-06-02T09:15:00", '{"to":[],"cc":[]}',
            "See you at 10.\n\n--Bob", "", "[]",
        ),
    )
    conn.commit()
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        rc = main(["board"])

    assert rc == 0
    captured = capsys.readouterr()
    out = captured.out

    assert "Inbox" in out
    assert "2 message(s)" in out

    # Card 1 content
    assert "alice@example.com" in out
    assert "Subject: Hello" in out
    assert "Date:    2025-06-01 14:30" in out
    assert "Just checking in!" in out

    # Card 2 content
    assert "bob@example.com" in out
    assert "Subject: Hi" in out
    assert "Date:    2025-06-02 09:15" in out
    assert "See you at 10." in out

    # Separator between cards (dashed line) — plus one from the header = 2
    assert out.count("-" * 60) == 2

    # No empty-inbox message
    assert "Your inbox is empty." not in out


def test_board_body_preview_truncation(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Body preview truncates at 150 chars with '…' only when longer."""
    from robotsix_auto_mail.db import init_db as real_init_db

    # Body exactly at the limit — no ellipsis
    body_150 = "x" * 150
    # Body over the limit — should truncate with ellipsis
    body_200 = "y" * 200

    conn = real_init_db(":memory:")
    conn.execute(
        """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        (
            1, "<a@x.com>", "a@x.com", "150 chars",
            "2025-06-01T14:30:00", '{"to":[],"cc":[]}',
            body_150, "", "[]",
        ),
    )
    conn.execute(
        """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        (
            2, "<b@x.com>", "b@x.com", "200 chars",
            "2025-06-02T09:15:00", '{"to":[],"cc":[]}',
            body_200, "", "[]",
        ),
    )
    conn.commit()

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        rc = main(["board"])

    assert rc == 0
    out = capsys.readouterr().out

    # 150-char body: full text, no ellipsis in its card
    assert body_150 in out
    assert body_150 + "\u2026" not in out

    # 200-char body: truncated at 150 chars + ellipsis
    truncated = body_200[:150] + "\u2026"
    assert truncated in out
    assert body_200 not in out  # full 200-char string not present


def test_board_config_load_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """board exits with code 1 when config loading fails."""
    with mock.patch(
        "robotsix_auto_mail.cli.load",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(SystemExit) as exc:
            main(["board"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Error loading configuration" in err
    assert "boom" in err


def test_board_header_uses_print_header(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board output includes the _print_header-style header."""
    from robotsix_auto_mail.db import init_db as real_init_db

    conn = real_init_db(":memory:")  # schema lives in db.py — no DDL duplication
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        main(["board"])

    captured = capsys.readouterr()
    # _print_header produces:
    # "\nInbox\n------------------------------------------------------------\n"
    assert "\nInbox\n" in captured.out
    assert "-" * 60 in captured.out


def test_board_does_not_mutate_database(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(["board"]) must not add, delete, or modify any rows in the database."""
    import os
    import sqlite3
    import tempfile

    from robotsix_auto_mail.db import init_db as real_init_db

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        conn = real_init_db(db_path)

        # Pre-populate with 2 records whose values we can snapshot.
        row1 = (
            10, "<x@a.com>", "alice@x.com", "Hello",
            "2025-01-01T12:00:00", '{"to":["bob@x.com"],"cc":[]}',
            "Body A", "<p>Body A</p>", '[{"name":"a.txt"}]',
        )
        row2 = (
            20, "<y@b.com>", "bob@x.com", "Hi",
            "2025-01-02T13:00:00", '{"to":["carol@x.com"],"cc":[]}',
            "Body B", "<p>Body B</p>", "[]",
        )
        conn.execute(
            """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            row1,
        )
        conn.execute(
            """\
INSERT INTO mail_records
    (imap_uid, message_id, sender, subject, date,
     recipients_json, body_plain, body_html, attachments_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            row2,
        )
        conn.commit()

        # Snapshot the full table state before the board command runs.
        def _snapshot(c: sqlite3.Connection) -> dict[str, Any]:

            cur = c.execute("SELECT * FROM mail_records ORDER BY id")
            col_names = [desc[0] for desc in cur.description]
            rows = [dict(zip(col_names, r, strict=True)) for r in cur.fetchall()]
            cur = c.execute("SELECT COUNT(*) FROM watermark")
            wm_count = cur.fetchone()[0]
            return {"mail_records": rows, "watermark_count": wm_count}

        before = _snapshot(conn)
        conn.close()

        # Now run main(["board"]) — it will call init_db(db_path) via load().
        # We patch only load(); init_db will be the real one, which opens the
        # same file-backed database.  The db_path comes from the config.
        cfg_with_db = MailConfig(
            imap_host="imap.example.com",
            imap_port=993,
            imap_tls_mode="direct-tls",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_tls_mode="starttls",
            username="user@example.com",
            password="s3cret",
            db_path=db_path,
        )

        with mock.patch(
            "robotsix_auto_mail.cli.load", return_value=cfg_with_db
        ):
            rc = main(["board"])

        assert rc == 0

        # Re-open the same file and snapshot again.
        conn2 = real_init_db(db_path)
        after = _snapshot(conn2)
        conn2.close()

        # Row count must be identical.
        assert len(after["mail_records"]) == 2
        assert len(after["mail_records"]) == len(before["mail_records"])

        # Every column of every row must be bit-for-bit unchanged.
        for i, (b_row, a_row) in enumerate(
            zip(before["mail_records"], after["mail_records"], strict=True)
        ):
            for col in b_row:
                assert a_row[col] == b_row[col], (
                    f"Row {i} column {col} changed: "
                    f"{b_row[col]!r} -> {a_row[col]!r}"
                )

        # Watermark table must be untouched.
        assert after["watermark_count"] == before["watermark_count"]

        # No interactive prompts or write-action indicators in output.
        captured = capsys.readouterr()
        assert "write" not in captured.out.lower()
        assert "delete" not in captured.out.lower()
        assert "edit" not in captured.out.lower()
        assert "modify" not in captured.out.lower()
        assert "select an action" not in captured.out.lower()

    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# detect subcommand
# ---------------------------------------------------------------------------


def test_parser_has_detect_subcommand() -> None:
    """The parser knows the detect subcommand with expected defaults."""
    parser = build_parser()
    args = parser.parse_args(["detect", "user@gmail.com"])
    assert args.command == "detect"
    assert args.email == "user@gmail.com"
    assert args.stdout is False
    assert args.output == "config/mail.local.yaml"


def test_detect_missing_pydantic_ai(capsys: pytest.CaptureFixture[str]) -> None:
    """detect exits 1 when pydantic_ai package is not installed."""
    import sys

    # Remove detect module from cache so the lazy import inside
    # _cmd_detect is forced to re-import (and we can block it).
    real_detect = sys.modules.pop("robotsix_auto_mail.detect", None)
    original_import = builtins.__import__

    def _block_detect(
        name: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        if name == "robotsix_auto_mail.detect":
            raise ImportError("No module named 'pydantic_ai'")
        return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

    try:
        with mock.patch("builtins.__import__", side_effect=_block_detect):
            rc = main(["detect", "user@gmail.com"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "requires the pydantic-ai package" in err
    finally:
        if real_detect is not None:
            sys.modules["robotsix_auto_mail.detect"] = real_detect


@pytest.fixture
def no_autoconfig() -> object:
    """Force autoconfig + MX detection to miss so tests reach the LLM path."""
    with mock.patch(
        "robotsix_auto_mail.detect.autoconfig_lookup", return_value=None
    ), mock.patch(
        "robotsix_auto_mail.detect.mx_lookup", return_value=[]
    ), mock.patch(
        "robotsix_auto_mail.detect.provider_from_mx", return_value=None
    ):
        yield


def _ok_result() -> object:
    from robotsix_auto_mail.cli import _VerifyResult

    return _VerifyResult(imap_ok=True, smtp_ok=True)


def _auth_fail_result() -> object:
    from robotsix_auto_mail.cli import _VerifyResult

    return _VerifyResult(
        imap_ok=False, smtp_ok=False, imap_auth=True, smtp_auth=True,
        imap_error="auth", smtp_error="auth",
    )


def _host_fail_result() -> object:
    """IMAP host unreachable, SMTP ok — a connection (not auth) failure."""
    from robotsix_auto_mail.cli import _VerifyResult

    return _VerifyResult(
        imap_ok=False, smtp_ok=True, imap_error="connection refused",
    )


def test_detect_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect writes a single config file (password included) on success."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch("getpass.getpass", return_value="testpass"), mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(
            ["detect", "user@gmail.com", "--output", str(output), "--no-verify"]
        )

    assert rc == 0
    content = output.read_text()
    assert "imap.gmail.com" in content
    assert "smtp.gmail.com" in content
    assert "user@gmail.com" in content
    # Password is written into the config file itself — no separate file.
    assert "testpass" in content
    assert not (tmp_path / "secrets.yaml").exists()

    captured = capsys.readouterr()
    assert "Config written" in captured.err


def test_detect_password_supplied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect --password skips the interactive prompt and writes the config."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch("getpass.getpass") as mock_getpass, mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(
            [
                "detect",
                "user@gmail.com",
                "--output", str(output),
                "--password", "cli-pass",
                "--no-verify",
            ]
        )

    assert rc == 0
    mock_getpass.assert_not_called()

    content = output.read_text()
    assert "cli-pass" in content
    assert not (tmp_path / "secrets.yaml").exists()


def test_detect_empty_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect with an empty password writes the config and warns the user."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch("getpass.getpass", return_value=""), mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(["detect", "user@gmail.com", "--output", str(output)])

    assert rc == 0
    content = output.read_text()
    assert "imap.gmail.com" in content

    captured = capsys.readouterr()
    assert "No password provided" in captured.err


def test_detect_stdout(
    capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect --stdout prints config and emits a verification banner."""
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(["detect", "user@gmail.com", "--stdout"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "imap.gmail.com" in captured.out
    assert "smtp.gmail.com" in captured.out
    assert "user@gmail.com" in captured.out
    assert "verify" in captured.err.lower()


def test_detect_stdout_with_password(
    capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect --stdout --password embeds the password in the printed config."""
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            ["detect", "user@gmail.com", "--stdout", "--password", "cli-pass"]
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "imap.gmail.com" in captured.out
    assert "cli-pass" in captured.out


def test_detect_detection_error(
    capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect exits 1 when DetectionError is raised (and autoconfig missed)."""
    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider",
        side_effect=DetectionError("test error"),
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(["detect", "user@gmail.com", "--stdout"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "test error" in captured.err


def test_detect_llm_model_env(
    capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """detect passes LLM_API_KEY from the environment to
    detect_provider (model is no longer forwarded — the tier bakes the model
    choice)."""
    mock_provider = MailProvider(
        imap_host="imap.gmail.com",
        smtp_host="smtp.gmail.com",
    )
    mock_dp = mock.MagicMock(return_value=mock_provider)

    with mock.patch.dict(
        os.environ,
        {"LLM_MODEL": "test-model", "LLM_API_KEY": "sk-test"},
    ):
        with mock.patch(
            "robotsix_auto_mail.detect.detect_provider", mock_dp
        ):
            rc = main(["detect", "user@x.com", "--stdout"])

    assert rc == 0
    mock_dp.assert_called_once_with(
        "user@x.com", api_key="sk-test", mx_hosts=[]
    )


def test_detect_uses_autoconfig_when_available(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When autoconfig resolves, the LLM is not consulted."""
    output = tmp_path / "cfg.yaml"
    autoconf_provider = MailProvider(
        imap_host="imap.fromautoconfig.net",
        smtp_host="smtp.fromautoconfig.net",
    )
    mock_llm = mock.MagicMock()

    with mock.patch(
        "robotsix_auto_mail.detect.autoconfig_lookup",
        return_value=autoconf_provider,
    ), mock.patch(
        "robotsix_auto_mail.detect.detect_provider", mock_llm
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@custom.net",
                "--output", str(output),
                "--password", "pw",
                "--no-verify",
            ]
        )

    assert rc == 0
    mock_llm.assert_not_called()
    assert "imap.fromautoconfig.net" in output.read_text()
    assert "autoconfig" in capsys.readouterr().err


def test_detect_verifies_connection_on_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """After writing the config, detect verifies by connecting (default)."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com", smtp_host="smtp.gmail.com"
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch(
        "robotsix_auto_mail.cli._verify_config", return_value=_ok_result()
    ) as mock_verify, mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
            ]
        )

    assert rc == 0
    mock_verify.assert_called_once()
    assert mock_verify.call_args.args[0].password == "pw"
    assert "Verification succeeded" in capsys.readouterr().err


def test_detect_verify_failure_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """A failed verification (auth, no retries) surfaces as exit code 1."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com", smtp_host="smtp.gmail.com"
    )

    # --password ⇒ no interactive password retry budget, so an auth-only
    # failure ends the loop immediately.
    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch(
        "robotsix_auto_mail.cli._verify_config",
        return_value=_auth_fail_result(),
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
            ]
        )

    assert rc == 1
    assert output.exists()
    assert "Verification FAILED" in capsys.readouterr().err


def test_detect_no_verify_skips_check(
    tmp_path: Path, no_autoconfig: object
) -> None:
    """--no-verify writes the config without connecting."""
    output = tmp_path / "cfg.yaml"
    mock_provider = MailProvider(
        imap_host="imap.gmail.com", smtp_host="smtp.gmail.com"
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch(
        "robotsix_auto_mail.cli._verify_config"
    ) as mock_verify, mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
                "--no-verify",
            ]
        )

    assert rc == 0
    mock_verify.assert_not_called()


def test_detect_refines_host_with_llm_on_connection_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """A connection failure triggers an LLM refinement that then succeeds."""
    output = tmp_path / "cfg.yaml"
    bad = MailProvider(imap_host="imap.bad.net", smtp_host="smtp.gmail.com")
    good = MailProvider(imap_host="imap.good.net", smtp_host="smtp.gmail.com")

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider",
        side_effect=[bad, good],
    ) as mock_dp, mock.patch(
        "robotsix_auto_mail.cli._verify_config",
        side_effect=[_host_fail_result(), _ok_result()],
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
            ]
        )

    assert rc == 0
    # initial guess + one refinement
    assert mock_dp.call_count == 2
    # the refinement was given failure feedback
    assert mock_dp.call_args.kwargs.get("feedback")
    assert "imap.good.net" in output.read_text()
    assert "Refining" in capsys.readouterr().err


def test_detect_prompts_for_host_when_llm_cannot_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_autoconfig: object
) -> None:
    """When LLM refinement errors, detect prompts for the host, then verifies."""
    output = tmp_path / "cfg.yaml"
    bad = MailProvider(imap_host="imap.bad.net", smtp_host="smtp.gmail.com")

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider",
        side_effect=[bad, DetectionError("llm down")],
    ), mock.patch(
        "robotsix_auto_mail.cli._verify_config",
        side_effect=[_host_fail_result(), _ok_result()],
    ), mock.patch(
        "builtins.input", return_value="mail.manual.net"
    ) as mock_input, mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
            ]
        )

    assert rc == 0
    mock_input.assert_called()
    assert "mail.manual.net" in output.read_text()
    assert "manually" in capsys.readouterr().err


def test_detect_preserves_existing_llm_section(
    tmp_path: Path, no_autoconfig: object
) -> None:
    """Re-running detect over a file keeps its llm: section."""
    output = tmp_path / "mail.local.yaml"
    output.write_text(
        """\
imap:
  host: old.example.com

smtp:
  host: old.example.com

auth:
  username: old@example.com

llm:
  api_key: sk-keep-me
  model: anthropic/claude-3-haiku
"""
    )
    mock_provider = MailProvider(
        imap_host="imap.gmail.com", smtp_host="smtp.gmail.com"
    )

    with mock.patch(
        "robotsix_auto_mail.detect.detect_provider", return_value=mock_provider
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(
            [
                "detect", "user@gmail.com",
                "--output", str(output),
                "--password", "pw",
                "--no-verify",
            ]
        )

    assert rc == 0
    content = output.read_text()
    # mail fields updated…
    assert "imap.gmail.com" in content
    assert "user@gmail.com" in content
    # …but the llm section is preserved
    assert "sk-keep-me" in content
    assert "anthropic/claude-3-haiku" in content


# ---------------------------------------------------------------------------
# ingest --watch
# ---------------------------------------------------------------------------


def test_ingest_watch_parser() -> None:
    """The ingest subcommand exposes --watch (default False)."""
    parser = build_parser()
    assert parser.parse_args(["ingest", "--watch"]).watch is True
    assert parser.parse_args(["ingest"]).watch is False


def test_ingest_watch_loops_then_stops_on_interrupt(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Watch mode runs a cycle, then exits 0 when interrupted during sleep."""
    from robotsix_auto_mail.cli import _cmd_ingest

    with mock.patch(
        "robotsix_auto_mail.cli._ingest_cycle", return_value=0
    ) as mock_cycle, mock.patch(
        "robotsix_auto_mail.cli.time.sleep", side_effect=KeyboardInterrupt
    ):
        rc = _cmd_ingest(cfg, watch=True)

    assert rc == 0
    mock_cycle.assert_called_once()
    assert "Watch stopped" in capsys.readouterr().out


def test_ingest_watch_survives_cycle_error(
    cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing cycle is logged and does not abort the watch loop."""
    from robotsix_auto_mail.cli import _cmd_ingest

    with mock.patch(
        "robotsix_auto_mail.cli._ingest_cycle",
        side_effect=RuntimeError("boom"),
    ), mock.patch(
        "robotsix_auto_mail.cli.time.sleep", side_effect=KeyboardInterrupt
    ):
        rc = _cmd_ingest(cfg, watch=True)

    assert rc == 0
    assert "Ingest cycle failed" in capsys.readouterr().err


def test_ingest_single_pass_unaffected(
    cfg: MailConfig,
) -> None:
    """Without --watch, _cmd_ingest delegates to a single cycle."""
    from robotsix_auto_mail.cli import _cmd_ingest

    with mock.patch(
        "robotsix_auto_mail.cli._ingest_cycle", return_value=0
    ) as mock_cycle:
        rc = _cmd_ingest(cfg, watch=False)

    assert rc == 0
    mock_cycle.assert_called_once_with(cfg, dry_run=False)


# ---------------------------------------------------------------------------
# config-sync subcommand
# ---------------------------------------------------------------------------


def _patch_config_sync_llm(
    result_obj: ConfigSyncResult,
) -> mock._patch[mock.MagicMock]:
    """Patch OpenRouterDeepseekProvider so the agent returns *result_obj*."""
    mock_run_result = mock.MagicMock()
    mock_run_result.output = result_obj
    mock_handle = mock.MagicMock()
    mock_handle.run_sync.return_value = mock_run_result

    mock_provider = mock.MagicMock()
    mock_provider.build_agent.return_value = mock_handle
    mock_provider.call_with_retry.side_effect = lambda fn, what: fn()

    return mock.patch(
        "robotsix_auto_mail.config_sync.OpenRouterDeepseekProvider",
        return_value=mock_provider,
    )


def test_parser_has_config_sync_subcommand() -> None:
    """The parser knows the config-sync subcommand with expected defaults."""
    args = build_parser().parse_args(
        ["config-sync", "--output-format", "json"]
    )
    assert args.command == "config-sync"
    assert args.output_format == "json"
    assert args.dedup is False
    assert args.api_key is None


def test_config_sync_text_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A result with proposals prints title + body to stdout and returns 0."""
    result = ConfigSyncResult(
        proposals=[
            DriftProposal(
                title="imap_folder default mismatch",
                body="Docs say INBOX.All but the dataclass default is INBOX.",
                affected_field="imap_folder",
                confidence="high",
            )
        ]
    )
    with _patch_config_sync_llm(result), mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(["config-sync"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "imap_folder default mismatch" in out
    assert "Docs say INBOX.All but the dataclass default is INBOX." in out
    assert "imap_folder" in out


def test_config_sync_json_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--output-format json prints a parseable object and returns 0."""
    result = ConfigSyncResult(
        proposals=[
            DriftProposal(
                title="env key drift",
                body="The .env.example uses MAIL_USER but config expects USERNAME.",
                affected_field="username",
                confidence="medium",
            )
        ]
    )
    with _patch_config_sync_llm(result), mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(["config-sync", "--output-format", "json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "proposals" in payload
    assert len(payload["proposals"]) == 1
    assert payload["proposals"][0]["title"] == "env key drift"
    assert payload["proposals"][0]["affected_field"] == "username"


def test_config_sync_no_drift(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty result prints the no-drift message and returns 0."""
    with _patch_config_sync_llm(ConfigSyncResult(proposals=[])), mock.patch.dict(
        os.environ, {"LLM_API_KEY": "sk-test"}
    ):
        rc = main(["config-sync"])

    assert rc == 0
    assert "No config drift detected." in capsys.readouterr().out


def test_config_sync_error_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ConfigSyncError returns 1 and writes an Error: line to stderr."""
    with mock.patch(
        "robotsix_auto_mail.config_sync.run_config_sync_agent",
        side_effect=ConfigSyncError("surface read failed"),
    ):
        rc = main(["config-sync"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "surface read failed" in err


def test_config_sync_api_key_precedence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--api-key overrides LLM_API_KEY env when constructing the provider."""
    with _patch_config_sync_llm(ConfigSyncResult(proposals=[])) as cls, (
        mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-env"})
    ):
        rc = main(["config-sync", "--api-key", "sk-cli"])

    assert rc == 0
    cls.assert_called_once_with(api_key="sk-cli")


def test_config_sync_dedup_forwards_conn(
    tmp_path: Path,
) -> None:
    """--dedup forwards an open DB connection to the agent."""
    cfg_with_db = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=str(tmp_path / "ledger.db"),
    )
    with mock.patch(
        "robotsix_auto_mail.config_sync.run_config_sync_agent",
        return_value=ConfigSyncResult(proposals=[]),
    ) as mock_agent, mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg_with_db
    ):
        rc = main(["config-sync", "--dedup"])

    assert rc == 0
    assert mock_agent.call_args.kwargs["conn"] is not None


def test_parser_has_config_sync_set_subcommand() -> None:
    """The parser knows the config-sync-set subcommand with positional args."""
    args = build_parser().parse_args(
        ["config-sync-set", "abc123", "accepted"]
    )
    assert args.command == "config-sync-set"
    assert args.fingerprint == "abc123"
    assert args.state == "accepted"


def test_config_sync_set_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """config-sync-set transitions a known finding and exits 0."""
    from robotsix_auto_mail.config_sync import (
        _load_ledger,
        _proposal_fingerprint,
        record_and_filter_proposals,
    )
    from robotsix_auto_mail.db import init_db as real_init_db

    cfg_db = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=str(tmp_path / "ledger.db"),
    )
    proposal = DriftProposal(
        title="imap_folder default mismatch",
        body="Docs say INBOX.All but the dataclass default is INBOX.",
        affected_field="imap_folder",
        confidence="high",
    )
    fingerprint = _proposal_fingerprint(proposal)
    conn = real_init_db(cfg_db.db_path)
    try:
        record_and_filter_proposals(conn, [proposal])
    finally:
        conn.close()

    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["config-sync-set", fingerprint, "accepted"])

    assert rc == 0
    assert "Recorded config-drift finding state" in capsys.readouterr().out

    conn = real_init_db(cfg_db.db_path)
    try:
        ledger = _load_ledger(conn)
        assert ledger[fingerprint].state == "accepted"
    finally:
        conn.close()


def test_config_sync_set_invalid_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """config-sync-set exits 1 with a clear message on an invalid state."""
    cfg_db = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=str(tmp_path / "ledger.db"),
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["config-sync-set", "abc123", "banana"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid state" in err
    assert "banana" in err


def test_config_sync_set_unknown_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """config-sync-set exits 1 when the fingerprint is unknown."""
    cfg_db = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=str(tmp_path / "ledger.db"),
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["config-sync-set", "deadbeef", "accepted"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "No ledger finding" in err
    assert "deadbeef" in err


# ---------------------------------------------------------------------------
# triage subcommand
# ---------------------------------------------------------------------------


def _patch_triage_llm(
    result_obj: TriageResult,
) -> mock._patch[mock.MagicMock]:
    """Patch OpenRouterDeepseekProvider so the agent returns *result_obj*."""
    mock_run_result = mock.MagicMock()
    mock_run_result.output = result_obj
    mock_handle = mock.MagicMock()
    mock_handle.run_sync.return_value = mock_run_result

    mock_provider = mock.MagicMock()
    mock_provider.build_agent.return_value = mock_handle
    mock_provider.call_with_retry.side_effect = lambda fn, what: fn()

    return mock.patch(
        "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider",
        return_value=mock_provider,
    )


def _cfg_with_inbox(tmp_path: Path, message_id: str = "<a@x.com>") -> MailConfig:
    """A MailConfig pointing at a temp DB seeded with one inbox record."""
    from robotsix_auto_mail.db import (
        MailRecord,
        insert_record,
    )
    from robotsix_auto_mail.db import (
        init_db as real_init_db,
    )

    db_path = str(tmp_path / "triage.db")
    conn = real_init_db(db_path)
    insert_record(
        conn,
        MailRecord(
            message_id=message_id,
            sender="alice@example.com",
            subject="Hello",
            date="2025-06-01T12:00:00",
            body_plain="Just checking in!",
        ),
    )
    conn.close()
    return MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=db_path,
    )


def test_parser_has_triage_subcommand() -> None:
    """The parser knows the triage subcommand with expected defaults."""
    args = build_parser().parse_args(["triage", "--output-format", "json"])
    assert args.command == "triage"
    assert args.output_format == "json"
    assert args.api_key is None


def test_parser_has_triage_set_subcommand() -> None:
    """The parser knows the triage-set subcommand with positional args."""
    args = build_parser().parse_args(["triage-set", "<a@x.com>", "answer"])
    assert args.command == "triage-set"
    assert args.message_id == "<a@x.com>"
    assert args.action == "answer"


def test_triage_text_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage prints decisions and exits 0 (text)."""
    cfg_db = _cfg_with_inbox(tmp_path)
    result = TriageResult(
        items=[TriageItem(index=1, action="answer", reason="needs reply")]
    )
    with _patch_triage_llm(result), mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg_db
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(["triage"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Inbox Triage" in out
    assert "<a@x.com>" in out
    assert "answer" in out
    assert "needs reply" in out


def test_triage_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage --output-format json prints a parseable list and exits 0."""
    cfg_db = _cfg_with_inbox(tmp_path)
    result = TriageResult(
        items=[TriageItem(index=1, action="archive", confidence="high")]
    )
    with _patch_triage_llm(result), mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg_db
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(["triage", "--output-format", "json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["message_id"] == "<a@x.com>"
    assert payload[0]["action"] == "archive"
    assert payload[0]["source"] == "agent"


def test_triage_empty_inbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage prints a friendly message when there is no inbox mail."""
    cfg_db = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
        db_path=str(tmp_path / "empty.db"),
    )
    with mock.patch(
        "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider"
    ) as cls, mock.patch(
        "robotsix_auto_mail.cli.load", return_value=cfg_db
    ), mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
        rc = main(["triage"])

    assert rc == 0
    assert "No inbox mail to triage." in capsys.readouterr().out
    cls.assert_not_called()


def test_triage_error_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A TriageError returns 1 and writes an Error: line to stderr."""
    cfg_db = _cfg_with_inbox(tmp_path)
    with mock.patch(
        "robotsix_auto_mail.triage.run_triage_agent",
        side_effect=TriageError("llm exploded"),
    ), mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["triage"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "llm exploded" in err


def test_triage_set_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-set records a user decision and exits 0."""
    from robotsix_auto_mail.db import init_db as real_init_db
    from robotsix_auto_mail.triage import _load_memory, get_triage_decision

    cfg_db = _cfg_with_inbox(tmp_path)
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["triage-set", "<a@x.com>", "archive"])

    assert rc == 0
    assert "Recorded user triage decision" in capsys.readouterr().out

    conn = real_init_db(cfg_db.db_path)
    try:
        decision = get_triage_decision(conn, "<a@x.com>")
        assert decision is not None
        assert decision.action == "archive"
        assert decision.source == "user"
        # The user decision also updates the human-decision memory ledger.
        memory = _load_memory(conn)
        assert "alice@example.com" in memory
        assert memory["alice@example.com"].action == "archive"
    finally:
        conn.close()


def test_triage_set_invalid_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-set exits 1 with a clear message on an invalid action."""
    cfg_db = _cfg_with_inbox(tmp_path)
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["triage-set", "<a@x.com>", "banana"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid action" in err
    assert "banana" in err


def test_triage_set_unknown_message_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-set exits 1 with a clear message when the message_id is unknown."""
    cfg_db = _cfg_with_inbox(tmp_path)
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg_db):
        rc = main(["triage-set", "<missing@x.com>", "answer"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no mail with message_id" in err
    assert "<missing@x.com>" in err


# ---------------------------------------------------------------------------
# triage-rules / triage-rules-set subcommands
# ---------------------------------------------------------------------------


def _seed_rule_history(
    db_path: str, sender: str, action: str, count: int
) -> None:
    """Seed a DB with *count* user decisions from *sender* as *action*."""
    from robotsix_auto_mail.db import (
        MailRecord,
        insert_record,
    )
    from robotsix_auto_mail.db import (
        init_db as real_init_db,
    )
    from robotsix_auto_mail.triage import set_triage_decision

    conn = real_init_db(db_path)
    try:
        for i in range(count):
            mid = f"<r{i}@x.com>"
            insert_record(
                conn,
                MailRecord(
                    message_id=mid,
                    sender=sender,
                    subject="Hello",
                    date="2025-06-01T12:00:00",
                ),
            )
            set_triage_decision(conn, mid, action, source="user")
    finally:
        conn.close()


def test_parser_has_triage_rules_subcommands() -> None:
    """The parser knows triage-rules and triage-rules-set."""
    args = build_parser().parse_args(["triage-rules", "--output-format", "json"])
    assert args.command == "triage-rules"
    assert args.output_format == "json"
    args2 = build_parser().parse_args(["triage-rules-set", "abc123", "accepted"])
    assert args2.command == "triage-rules-set"
    assert args2.fingerprint == "abc123"
    assert args2.state == "accepted"


def test_triage_rules_text_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-rules prints derived proposals with fingerprints and exits 0."""
    db_path = str(tmp_path / "rules.db")
    _seed_rule_history(db_path, "news@a.com", "archive", 3)
    cfg = MailConfig(
        imap_host="imap.example.com", smtp_host="smtp.example.com",
        username="u@example.com", password="s3cret", db_path=db_path,
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg):
        rc = main(["triage-rules"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Triage Rule Proposals" in out
    assert "news@a.com" in out
    assert "archive" in out


def test_triage_rules_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-rules --output-format json emits proposals + active rules."""
    db_path = str(tmp_path / "rules.db")
    _seed_rule_history(db_path, "news@a.com", "archive", 3)
    cfg = MailConfig(
        imap_host="imap.example.com", smtp_host="smtp.example.com",
        username="u@example.com", password="s3cret", db_path=db_path,
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg):
        rc = main(["triage-rules", "--output-format", "json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "proposals" in payload
    assert "active_rules" in payload
    assert payload["proposals"]
    fp = payload["proposals"][0]["fingerprint"]
    assert isinstance(fp, str) and fp


def test_triage_rules_set_accept_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-rules-set accepted adds an active rule visible to the agent."""
    from robotsix_auto_mail.db import init_db as real_init_db
    from robotsix_auto_mail.triage import (
        _rule_fingerprint,
        list_active_rules,
        propose_triage_rules,
    )

    db_path = str(tmp_path / "rules.db")
    _seed_rule_history(db_path, "news@a.com", "archive", 3)
    cfg = MailConfig(
        imap_host="imap.example.com", smtp_host="smtp.example.com",
        username="u@example.com", password="s3cret", db_path=db_path,
    )

    conn = real_init_db(db_path)
    try:
        proposals = propose_triage_rules(conn)
        fp = _rule_fingerprint(proposals[0])
    finally:
        conn.close()

    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg):
        rc = main(["triage-rules", "--output-format", "json"])  # record proposals
        assert rc == 0
        rc = main(["triage-rules-set", fp, "accepted"])

    assert rc == 0
    assert "accepted" in capsys.readouterr().out

    conn = real_init_db(db_path)
    try:
        active = list_active_rules(conn)
        assert len(active) == 1
        assert active[0].match_value == "news@a.com"
    finally:
        conn.close()


def test_triage_rules_set_invalid_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-rules-set exits 1 on an invalid state."""
    cfg = MailConfig(
        imap_host="imap.example.com", smtp_host="smtp.example.com",
        username="u@example.com", password="s3cret",
        db_path=str(tmp_path / "rules.db"),
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg):
        rc = main(["triage-rules-set", "abc123", "pending"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid state" in err
    assert "pending" in err


def test_triage_rules_set_unknown_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """triage-rules-set exits 1 on an unknown fingerprint."""
    cfg = MailConfig(
        imap_host="imap.example.com", smtp_host="smtp.example.com",
        username="u@example.com", password="s3cret",
        db_path=str(tmp_path / "rules.db"),
    )
    with mock.patch("robotsix_auto_mail.cli.load", return_value=cfg):
        rc = main(["triage-rules-set", "deadbeefdeadbeef", "accepted"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "deadbeefdeadbeef" in err

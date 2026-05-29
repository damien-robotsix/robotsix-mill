"""Tests for the CLI module."""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from unittest import mock

import pytest

from robotsix_auto_mail.cli import build_parser, main
from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.imap import ImapClient
from robotsix_auto_mail.smtp_client import SmtpClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_cfg() -> MailConfig:
    """A valid MailConfig matching what the env-based mocks will supply."""
    return MailConfig(
        imap_host="imap.example.com",
        imap_port=993,
        imap_tls_mode="direct-tls",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_tls_mode="starttls",
        username="user@example.com",
        password="s3cret",
    )


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


def test_imap_client_properties_before_connect(env_cfg: MailConfig) -> None:
    """server_greeting / capabilities return safe defaults when not connected."""
    client = ImapClient(env_cfg)
    assert client.server_greeting is None
    assert client.capabilities == ()


def test_smtp_client_properties_before_connect(env_cfg: MailConfig) -> None:
    """ehlo_response / esmtp_features return safe defaults when not connected."""
    client = SmtpClient(env_cfg)
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
# probe – success
# ---------------------------------------------------------------------------


def test_probe_success(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe exits 0 and prints IMAP + SMTP metadata when both succeed."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
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
# probe – IMAP failure, SMTP succeeds
# ---------------------------------------------------------------------------


def test_probe_imap_failure_smtp_ok(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
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
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
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
# probe – SMTP failure, IMAP succeeds
# ---------------------------------------------------------------------------


def test_probe_smtp_failure_imap_ok(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
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
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
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
# probe – both fail
# ---------------------------------------------------------------------------


def test_probe_both_fail(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
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
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    # Both errors reported
    assert err.count("Error:") == 2


# ---------------------------------------------------------------------------
# probe – never calls send_message
# ---------------------------------------------------------------------------


def test_probe_never_calls_send_message(
    env_cfg: MailConfig,
) -> None:
    """The SMTP mock's send_message is never called."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP", return_value=mock_smtp
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
    ):
        main(["probe"])

    mock_smtp.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# probe – connection refusal for IMAP
# ---------------------------------------------------------------------------


def test_probe_imap_connection_refused(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles IMAP connection-refused gracefully."""
    mock_smtp = _make_mock_smtp()

    with mock.patch(
        "imaplib.IMAP4_SSL",
        side_effect=ConnectionRefusedError("Connection refused"),
    ), mock.patch("smtplib.SMTP", return_value=mock_smtp), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Connection refused" in err


# ---------------------------------------------------------------------------
# probe – connection refusal for SMTP
# ---------------------------------------------------------------------------


def test_probe_smtp_connection_refused(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles SMTP connection-refused gracefully."""
    mock_imap = _make_mock_imap_ssl()

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch(
        "smtplib.SMTP",
        side_effect=ConnectionRefusedError("Connection refused"),
    ), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "Connection refused" in err


# ---------------------------------------------------------------------------
# probe – TLS failure for IMAP
# ---------------------------------------------------------------------------


def test_probe_imap_tls_failure(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
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
# probe – SMTP STARTTLS failure
# ---------------------------------------------------------------------------


def test_probe_smtp_tls_failure(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """probe handles SMTP TLS failure gracefully."""
    mock_imap = _make_mock_imap_ssl()
    mock_smtp = mock.MagicMock(spec=smtplib.SMTP)
    mock_smtp.ehlo_or_helo_if_needed.return_value = (250, b"OK")
    mock_smtp.starttls.side_effect = ssl.SSLError("certificate verify failed")

    with mock.patch(
        "imaplib.IMAP4_SSL", return_value=mock_imap
    ), mock.patch("smtplib.SMTP", return_value=mock_smtp), mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
    ):
        rc = main(["probe"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error:" in err
    assert "STARTTLS" in err or "certificate" in err


# ---------------------------------------------------------------------------
# probe – IMAP authentication failure
# ---------------------------------------------------------------------------


def test_probe_imap_auth_failure(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
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
        "robotsix_auto_mail.config.MailConfig.from_env", return_value=env_cfg
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
    """probe returns 1 when config loading fails."""
    with mock.patch(
        "robotsix_auto_mail.config.MailConfig.from_env",
        side_effect=RuntimeError("boom"),
    ):
        rc = main(["probe"])

    assert rc == 1
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


def test_smtp_client_properties_after_connect(env_cfg: MailConfig) -> None:
    """ehlo_response / esmtp_features reflect the mock after connect."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with SmtpClient(env_cfg) as client:
            assert client.ehlo_response == b"250-smtp.example.com\n250 STARTTLS"
            assert client.esmtp_features == {
                "STARTTLS": "",
                "AUTH": "PLAIN LOGIN",
            }


def test_imap_client_properties_after_connect(env_cfg: MailConfig) -> None:
    """server_greeting / capabilities reflect the mock after connect."""
    mock_imap = _make_mock_imap_ssl()

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_imap):
        with ImapClient(env_cfg) as client:
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
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board prints '(no mail)' when the database is empty."""
    from robotsix_auto_mail.db import init_db as real_init_db

    conn = real_init_db(":memory:")  # schema lives in db.py — no DDL duplication
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=env_cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        rc = main(["board"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "Inbox" in captured.out
    assert "(no mail)" in captured.out


def test_board_with_records(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board prints a message count when records exist."""
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
            "2025-06-01", '{"to":[],"cc":[]}', "", "", "[]",
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
            "2025-06-02", '{"to":[],"cc":[]}', "", "", "[]",
        ),
    )
    conn.commit()
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=env_cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        rc = main(["board"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "Inbox" in captured.out
    assert "2 message(s)" in captured.out


def test_board_config_load_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """board returns 1 when config loading fails."""
    with mock.patch(
        "robotsix_auto_mail.cli.load",
        side_effect=RuntimeError("boom"),
    ):
        rc = main(["board"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Error loading configuration" in err
    assert "boom" in err


def test_board_header_uses_print_header(
    env_cfg: MailConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """board output includes the _print_header-style header."""
    from robotsix_auto_mail.db import init_db as real_init_db

    conn = real_init_db(":memory:")  # schema lives in db.py — no DDL duplication
    # Keep conn open — _cmd_board's finally block closes it.

    with mock.patch(
        "robotsix_auto_mail.cli.load", return_value=env_cfg
    ), mock.patch(
        "robotsix_auto_mail.cli.init_db", return_value=conn
    ):
        main(["board"])

    captured = capsys.readouterr()
    # The _print_header produces: "\nInbox\n------------------------------------------------------------\n"
    assert "\nInbox\n" in captured.out
    assert "-" * 60 in captured.out
"""Tests for the SMTP client module."""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.mime.text import MIMEText
from unittest import mock

import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.smtp_client import (
    SmtpAuthError,
    SmtpClient,
    SmtpConnectionError,
    SmtpError,
    SmtpSendError,
    SmtpTlsError,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------



def _make_mock_smtp_ssl() -> mock.MagicMock:
    """Factory for a mock ``SMTP_SSL`` instance."""
    m = mock.MagicMock(spec=smtplib.SMTP_SSL)
    m.login.return_value = (235, b"2.7.0 Authentication successful")
    m.send_message.return_value = {}
    m.noop.return_value = (250, b"OK")
    return m


def _make_mock_smtp() -> mock.MagicMock:
    """Factory for a mock ``SMTP`` instance (plain, for STARTTLS / none)."""
    m = mock.MagicMock(spec=smtplib.SMTP)
    m.login.return_value = (235, b"2.7.0 Authentication successful")
    m.send_message.return_value = {}
    m.noop.return_value = (250, b"OK")
    return m


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_smtp_error_is_exception() -> None:
    """SmtpError is a proper Exception subclass."""
    assert issubclass(SmtpError, Exception)


def test_smtp_connection_error_is_smtp_error() -> None:
    """SmtpConnectionError is a subclass of SmtpError."""
    assert issubclass(SmtpConnectionError, SmtpError)


def test_smtp_tls_error_is_smtp_error() -> None:
    """SmtpTlsError is a subclass of SmtpError."""
    assert issubclass(SmtpTlsError, SmtpError)


def test_smtp_auth_error_is_smtp_error() -> None:
    """SmtpAuthError is a subclass of SmtpError."""
    assert issubclass(SmtpAuthError, SmtpError)


def test_smtp_send_error_is_smtp_error() -> None:
    """SmtpSendError is a subclass of SmtpError."""
    assert issubclass(SmtpSendError, SmtpError)


def test_specific_errors_caught_by_base() -> None:
    """Callers can catch SmtpError to handle all SMTP failure modes."""
    for exc_cls in (
        SmtpConnectionError,
        SmtpTlsError,
        SmtpAuthError,
        SmtpSendError,
    ):
        try:
            raise exc_cls("test")
        except SmtpError:
            pass
        else:
            pytest.fail(f"{exc_cls.__name__} not caught by SmtpError")


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------


def test_repr_redacts_password(cfg: MailConfig) -> None:
    """repr(SmtpClient) must not expose the password."""
    client = SmtpClient(cfg)
    r = repr(client)
    assert "s3cret" not in r
    assert "<redacted>" in r
    assert "smtp.example.com" in r


def test_repr_includes_user(cfg: MailConfig) -> None:
    """repr(SmtpClient) includes the username."""
    client = SmtpClient(cfg)
    r = repr(client)
    assert "user@example.com" in r


# ===================================================================
# connect() tests
# ===================================================================


# -- direct-tls -----------------------------------------------------------


def test_connect_direct_tls_creates_smtp_ssl(cfg: MailConfig) -> None:
    """connect() with tls_mode='direct-tls' creates SMTP_SSL with
    ssl.create_default_context()."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
        username="user@example.com",
        password="s3cret",
    )

    mock_smtp = _make_mock_smtp_ssl()

    with mock.patch("smtplib.SMTP_SSL", return_value=mock_smtp) as patched:
        client = SmtpClient(cfg)
        client.connect()

        patched.assert_called_once()
        _, kwargs = patched.call_args
        assert kwargs["context"] is not None
        assert isinstance(kwargs["context"], ssl.SSLContext)

    mock_smtp.login.assert_called_once_with("user@example.com", "s3cret")


# -- starttls -------------------------------------------------------------


def test_connect_starttls_creates_plain_smtp_calls_starttls(
    cfg: MailConfig,
) -> None:
    """connect() with tls_mode='starttls' creates plain SMTP,
    calls ehlo_or_helo_if_needed() before and after starttls()."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp) as patched:
        client = SmtpClient(cfg)
        client.connect()

        patched.assert_called_once_with("smtp.example.com", 587)

    # ehlo before starttls
    assert mock_smtp.ehlo_or_helo_if_needed.call_count == 2

    # starttls called
    mock_smtp.starttls.assert_called_once()
    _, starttls_kwargs = mock_smtp.starttls.call_args
    assert isinstance(starttls_kwargs["context"], ssl.SSLContext)

    mock_smtp.login.assert_called_once_with("user@example.com", "s3cret")


# -- none -----------------------------------------------------------------


def test_connect_none_creates_plain_smtp_no_tls(cfg: MailConfig) -> None:
    """connect() with tls_mode='none' creates plain SMTP, no TLS."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_tls_mode="none",
        username="user@example.com",
        password="s3cret",
    )

    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp) as patched:
        client = SmtpClient(cfg)
        client.connect()

        patched.assert_called_once_with("smtp.example.com", 25)

    mock_smtp.starttls.assert_not_called()
    mock_smtp.login.assert_called_once_with("user@example.com", "s3cret")


# -- connection failure ---------------------------------------------------


def test_connect_connection_refused_direct_tls(cfg: MailConfig) -> None:
    """Connection refused on direct-tls → SmtpConnectionError."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
        username="user@example.com",
        password="s3cret",
    )

    original = ConnectionRefusedError("Connection refused")
    with mock.patch("smtplib.SMTP_SSL", side_effect=original):
        with pytest.raises(SmtpConnectionError) as exc:
            SmtpClient(cfg).connect()
        assert "Direct-TLS" in str(exc.value)
        assert exc.value.__cause__ is original


def test_connect_connection_refused_plain(cfg: MailConfig) -> None:
    """Connection refused on plain → SmtpConnectionError."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_tls_mode="none",
        username="u",
        password="p",
    )

    original = ConnectionRefusedError("Connection refused")
    with mock.patch("smtplib.SMTP", side_effect=original):
        with pytest.raises(SmtpConnectionError) as exc:
            SmtpClient(cfg).connect()
        assert exc.value.__cause__ is original


def test_connect_socket_gaierror(cfg: MailConfig) -> None:
    """socket.gaierror → SmtpConnectionError."""
    original = socket.gaierror("Name or service not known")
    with mock.patch("smtplib.SMTP", side_effect=original):
        with pytest.raises(SmtpConnectionError) as exc:
            SmtpClient(cfg).connect()
        assert exc.value.__cause__ is original


def test_connect_ehlo_failure_on_starttls(cfg: MailConfig) -> None:
    """Pre-STARTTLS EHLO failure → SmtpConnectionError."""
    mock_smtp = _make_mock_smtp()
    ehlo_error = smtplib.SMTPException("EHLO failed")
    mock_smtp.ehlo_or_helo_if_needed.side_effect = ehlo_error

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with pytest.raises(SmtpConnectionError) as exc:
            SmtpClient(cfg).connect()
        assert "EHLO/HELO failed" in str(exc.value)
        assert exc.value.__cause__ is ehlo_error


# -- TLS failure ----------------------------------------------------------


def test_connect_starttls_failure_not_advertised(cfg: MailConfig) -> None:
    """STARTTLS not advertised → SmtpTlsError."""
    mock_smtp = _make_mock_smtp()
    tls_error = smtplib.SMTPException("STARTTLS not available")
    mock_smtp.starttls.side_effect = tls_error

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with pytest.raises(SmtpTlsError) as exc:
            SmtpClient(cfg).connect()
        assert "STARTTLS" in str(exc.value)
        assert exc.value.__cause__ is tls_error


def test_connect_starttls_ssl_handshake_failure(cfg: MailConfig) -> None:
    """STARTTLS cert validation failure → SmtpTlsError."""
    mock_smtp = _make_mock_smtp()
    ssl_error = ssl.SSLError("certificate verify failed")
    mock_smtp.starttls.side_effect = ssl_error

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with pytest.raises(SmtpTlsError) as exc:
            SmtpClient(cfg).connect()
        assert "STARTTLS" in str(exc.value)
        assert exc.value.__cause__ is ssl_error


def test_connect_post_starttls_ehlo_failure(cfg: MailConfig) -> None:
    """Post-STARTTLS EHLO failure → SmtpTlsError."""
    mock_smtp = _make_mock_smtp()
    # first ehlo succeeds, starttls succeeds, second ehlo fails
    ehlo_error = smtplib.SMTPException("EHLO failed after TLS")
    mock_smtp.ehlo_or_helo_if_needed.side_effect = [
        (250, b"OK"),
        ehlo_error,
    ]

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with pytest.raises(SmtpTlsError) as exc:
            SmtpClient(cfg).connect()
        assert "Post-STARTTLS EHLO/HELO" in str(exc.value)
        assert exc.value.__cause__ is ehlo_error


# -- auth failure ---------------------------------------------------------


def test_connect_authentication_rejected(cfg: MailConfig) -> None:
    """login() fails → SmtpAuthError."""
    mock_smtp = _make_mock_smtp()
    auth_error = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Authentication failed"
    )
    mock_smtp.login.side_effect = auth_error

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with pytest.raises(SmtpAuthError) as exc:
            SmtpClient(cfg).connect()
        assert "Authentication failed" in str(exc.value)
        assert "user@example.com" in str(exc.value)
        assert exc.value.__cause__ is auth_error


def test_connect_auth_failure_direct_tls(cfg: MailConfig) -> None:
    """login() fails on direct-tls → SmtpAuthError."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
        username="user@example.com",
        password="s3cret",
    )

    mock_smtp = _make_mock_smtp_ssl()
    auth_error = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Authentication failed"
    )
    mock_smtp.login.side_effect = auth_error

    with mock.patch("smtplib.SMTP_SSL", return_value=mock_smtp):
        with pytest.raises(SmtpAuthError) as exc:
            SmtpClient(cfg).connect()
        assert exc.value.__cause__ is auth_error


# ===================================================================
# send() tests
# ===================================================================


def test_send_constructs_mime_and_calls_send_message(cfg: MailConfig) -> None:
    """send() builds a MIMEText with correct headers and calls
    send_message()."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.send(
            from_addr="bot@example.com",
            to_addr="user@example.com",
            subject="Hello",
            body="Test body",
        )

    mock_smtp.send_message.assert_called_once()
    call_args, call_kwargs = mock_smtp.send_message.call_args

    msg = call_args[0]
    assert isinstance(msg, MIMEText)
    assert msg["From"] == "bot@example.com"
    assert msg["To"] == "user@example.com"
    assert msg["Subject"] == "Hello"
    assert "Date" in msg
    # MIMEText defaults to text/plain; charset utf-8
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content_charset() == "utf-8"

    # Keyword arguments
    assert call_kwargs["from_addr"] == "bot@example.com"
    assert call_kwargs["to_addrs"] == ["user@example.com"]


def test_send_body_is_utf8_encoded(cfg: MailConfig) -> None:
    """send() properly encodes non-ASCII bodies."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.send(
            from_addr="bot@example.com",
            to_addr="user@example.com",
            subject="Café",
            body="résumé —  résumé",
        )

    msg = mock_smtp.send_message.call_args[0][0]
    # MIMEText base64-encodes non-ASCII bodies; verify the decoded payload.
    decoded = msg.get_payload(decode=True)
    assert decoded is not None
    assert "résumé" in decoded.decode("utf-8")


def test_send_includes_date_header(cfg: MailConfig) -> None:
    """send() includes a Date header via email.utils.formatdate()."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.send(
            from_addr="a@b.com",
            to_addr="c@d.com",
            subject="S",
            body="B",
        )

    msg = mock_smtp.send_message.call_args[0][0]
    assert "Date" in msg
    assert msg["Date"] is not None


def test_send_failure_raises_smtp_send_error(cfg: MailConfig) -> None:
    """send() failure (SMTP rejection) → SmtpSendError."""
    mock_smtp = _make_mock_smtp()
    send_error = smtplib.SMTPException("Message rejected")
    mock_smtp.send_message.side_effect = send_error

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        with pytest.raises(SmtpSendError) as exc:
            client.send(
                from_addr="bot@example.com",
                to_addr="user@example.com",
                subject="Hello",
                body="World",
            )
        assert "Failed to send" in str(exc.value)
        assert exc.value.__cause__ is send_error


def test_send_before_connect_raises(cfg: MailConfig) -> None:
    """Calling send() before connect() raises SmtpError."""
    client = SmtpClient(cfg)
    with pytest.raises(SmtpError, match="Not connected"):
        client.send(
            from_addr="a@b.com",
            to_addr="c@d.com",
            subject="S",
            body="B",
        )


# ===================================================================
# close() tests
# ===================================================================


def test_close_calls_quit(cfg: MailConfig) -> None:
    """close() calls smtp.quit()."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.close()

    mock_smtp.quit.assert_called_once()


def test_close_does_not_raise_if_already_disconnected(
    cfg: MailConfig,
) -> None:
    """close() swallows quit() failure (connection already closed)."""
    mock_smtp = _make_mock_smtp()
    mock_smtp.quit.side_effect = smtplib.SMTPException("already closed")

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.close()  # must not raise


def test_close_safe_to_call_multiple_times(cfg: MailConfig) -> None:
    """close() is safe to call multiple times."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        client = SmtpClient(cfg)
        client.connect()
        client.close()
        client.close()  # second call is a no-op

    # quit() called exactly once
    mock_smtp.quit.assert_called_once()


def test_close_before_connect_does_nothing(cfg: MailConfig) -> None:
    """close() is a no-op if we never connected."""
    client = SmtpClient(cfg)
    client.close()  # must not raise


# ===================================================================
# Context manager tests
# ===================================================================


def test_context_manager_connects_on_enter_and_closes_on_exit(
    cfg: MailConfig,
) -> None:
    """__enter__ calls connect(), __exit__ calls close()."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        with SmtpClient(cfg) as client:
            mock_smtp.login.assert_called_once()
            assert client is not None

    mock_smtp.quit.assert_called_once()


def test_context_manager_closes_on_exception(cfg: MailConfig) -> None:
    """quit() is called even when the block raises."""
    mock_smtp = _make_mock_smtp()

    with mock.patch("smtplib.SMTP", return_value=mock_smtp):
        try:
            with SmtpClient(cfg):
                raise RuntimeError("something went wrong inside the block")
        except RuntimeError:
            pass

    mock_smtp.quit.assert_called_once()


def test_context_manager_direct_tls_flow(cfg: MailConfig) -> None:
    """Direct-TLS context manager: lease → use → close."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_tls_mode="direct-tls",
        username="user@example.com",
        password="s3cret",
    )

    mock_smtp = _make_mock_smtp_ssl()

    with mock.patch("smtplib.SMTP_SSL", return_value=mock_smtp):
        with SmtpClient(cfg) as client:
            client.send(
                from_addr="bot@example.com",
                to_addr="user@example.com",
                subject="S",
                body="B",
            )

    mock_smtp.login.assert_called_once_with("user@example.com", "s3cret")
    mock_smtp.send_message.assert_called_once()
    mock_smtp.quit.assert_called_once()


# ===================================================================
# Doesn't depend on IMAP
# ===================================================================


def test_smtp_client_does_not_import_imap() -> None:
    """The smtp_client module must not reference the IMAP module."""
    import robotsix_auto_mail.smtp_client as mod

    source = mod.__file__
    assert source is not None
    content = open(source).read()
    assert "from robotsix_auto_mail.imap" not in content


def test_smtp_client_only_uses_smtp_fields(cfg: MailConfig) -> None:
    """SmtpClient constructor extracts only SMTP fields from MailConfig."""
    client = SmtpClient(cfg)
    assert client._host == "smtp.example.com"
    assert client._port == 587
    assert client._tls_mode == "starttls"
    assert client._username == "user@example.com"
    assert client._password == "s3cret"
    # IMAP fields are never stored
    assert not hasattr(client, "_imap_host")
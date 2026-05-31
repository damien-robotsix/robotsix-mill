"""SMTP client built on stdlib ``smtplib``.

Provides ``SmtpClient`` - a context manager that connects to an SMTP
server, negotiates TLS, authenticates, and sends plain-text MIME messages.

Depends only on ``MailConfig`` from ``robotsix_auto_mail.config`` and the
Python standard library (``smtplib``, ``ssl``, ``email``).
"""

from __future__ import annotations

import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from robotsix_auto_mail.config import MailConfig

# Store a reference to SMTPException *before* any mocking can replace
# smtplib.SMTP and turn ``SMTPException`` into a MagicMock attribute.
# Using this reference in except clauses keeps tests reliable.
_SMTP_EXCEPTION = smtplib.SMTPException


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SmtpError(Exception):
    """Base exception for all SMTP client errors."""


class SmtpConnectionError(SmtpError):
    """Socket-level or SMTP connection failure.

    Wraps ``OSError`` / ``socket.gaierror`` (unreachable host, connection
    refused, timeout) and ``smtplib.SMTPException`` from a bad server
    greeting or EHLO/HELO failure.
    """


class SmtpTlsError(SmtpError):
    """TLS negotiation failure.

    Wraps ``STARTTLS`` negotiation failures (``smtplib.SMTPException``
    when the server does not advertise the capability) and TLS handshake
    errors (``ssl.SSLError``).
    """


class SmtpAuthError(SmtpError):
    """Authentication failure.

    Wraps ``smtplib.SMTPException`` raised by ``login()`` when the server
    responds with an authentication error (bad credentials, etc.).
    """


class SmtpSendError(SmtpError):
    """Send failure.

    Wraps ``smtplib.SMTPException`` raised by ``send_message()`` when
    the server rejects the message or the connection is lost mid-send.
    """


# ---------------------------------------------------------------------------
# SmtpClient
# ---------------------------------------------------------------------------


class SmtpClient:
    """Context-managed SMTP client.

    Constructor accepts a ``MailConfig`` and extracts only the
    SMTP-relevant fields (``smtp_host``, ``smtp_port``, ``smtp_tls_mode``,
    ``username``, ``password``).  The IMAP fields are never referenced.

    Typical usage::

        cfg = MailConfig.from_env()
        with SmtpClient(cfg) as client:
            client.send(
                from_addr="bot@example.com",
                to_addr="user@example.com",
                subject="Hello",
                body="World",
            )
    """

    def __init__(self, config: MailConfig) -> None:
        self._host = config.smtp_host
        self._port = config.smtp_port
        self._tls_mode = config.smtp_tls_mode
        self._username = config.username
        self._password = config.password

        self._smtp: smtplib.SMTP | None = None

    # -- read-only server metadata ---------------------------------------

    @property
    def ehlo_response(self) -> bytes | None:
        """Full EHLO response bytes, or ``None`` when not connected."""
        if self._smtp is None:
            return None
        return self._smtp.ehlo_resp

    @property
    def esmtp_features(self) -> dict[str, str]:
        """Copy of ``esmtp_features`` dict, or ``{}`` when not connected."""
        if self._smtp is None:
            return {}
        return dict(self._smtp.esmtp_features)

    # -- repr --------------------------------------------------------------

    def __repr__(self) -> str:
        cls = type(self).__name__
        return (
            f"{cls}(host={self._host!r}, port={self._port!r}, "
            f"user={self._username!r}, password=<redacted>)"
        )

    # -- public API --------------------------------------------------------

    def connect(self) -> None:
        """Connect, negotiate TLS, and authenticate.

        Raises:
            SmtpConnectionError: Connection refused, host unreachable,
                or bad server greeting.
            SmtpTlsError: STARTTLS negotiation or certificate validation
                failure.
            SmtpAuthError: Login rejected (bad credentials, etc.).
        """
        tls_mode = self._tls_mode

        if tls_mode == "direct-tls":
            self._connect_direct_tls()
        elif tls_mode == "starttls":
            self._connect_starttls()
        elif tls_mode == "none":
            self._connect_plain()
        else:
            raise ValueError(f"Unknown TLS mode: {tls_mode!r}")

        self._authenticate()

    def send(
        self,
        *,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
    ) -> None:
        """Compose and transmit a plain-text MIME message.

        Args:
            from_addr: ``From`` header value.
            to_addr: ``To`` header value (single recipient).
            subject: ``Subject`` header value.
            body: Plain-text message body (UTF-8).

        Raises:
            SmtpError: The client is not connected.
            SmtpSendError: The server rejected the message.
        """
        if self._smtp is None:
            raise SmtpError("Not connected")

        msg = MIMEText(body, _charset="utf-8")
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)

        try:
            self._smtp.send_message(
                msg, from_addr=from_addr, to_addrs=[to_addr]
            )
        except _SMTP_EXCEPTION as exc:
            raise SmtpSendError(
                f"Failed to send message to {to_addr!r}: {exc}"
            ) from exc

    def close(self) -> None:
        """Disconnect gracefully (best-effort).  Safe to call multiple times."""
        if self._smtp is None:
            return
        try:
            self._smtp.quit()
        except _SMTP_EXCEPTION:
            pass
        self._smtp = None

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> SmtpClient:
        """Connect + authenticate, returning the ready-to-use client."""
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        """Disconnect, even if an exception occurred."""
        self.close()

    # -- connection helpers ------------------------------------------------

    def _connect_direct_tls(self) -> None:
        ctx = ssl.create_default_context()
        try:
            self._smtp = smtplib.SMTP_SSL(
                self._host, self._port, context=ctx
            )
        except (OSError, _SMTP_EXCEPTION) as exc:
            raise SmtpConnectionError(
                f"Direct-TLS connection to {self._host}:{self._port} "
                f"failed: {exc}"
            ) from exc

    def _connect_starttls(self) -> None:
        # 1. Plain connection.
        try:
            self._smtp = smtplib.SMTP(self._host, self._port)
        except (OSError, _SMTP_EXCEPTION) as exc:
            raise SmtpConnectionError(
                f"Plain connection to {self._host}:{self._port} "
                f"failed: {exc}"
            ) from exc

        # 2. Post-connect EHLO — the server may advertise STARTTLS
        #    (and possibly other extensions we don't use).
        try:
            self._smtp.ehlo_or_helo_if_needed()
        except _SMTP_EXCEPTION as exc:
            raise SmtpConnectionError(
                f"EHLO/HELO failed: {exc}"
            ) from exc

        # 3. Upgrade to TLS.
        ctx = ssl.create_default_context()
        try:
            self._smtp.starttls(context=ctx)
        except (_SMTP_EXCEPTION, ssl.SSLError) as exc:
            raise SmtpTlsError(
                f"STARTTLS negotiation with {self._host}:{self._port} "
                f"failed: {exc}"
            ) from exc

        # 4. Post-TLS EHLO — the server may advertise different
        #    extensions after upgrading.
        try:
            self._smtp.ehlo_or_helo_if_needed()
        except _SMTP_EXCEPTION as exc:
            raise SmtpTlsError(
                f"Post-STARTTLS EHLO/HELO failed: {exc}"
            ) from exc

    def _connect_plain(self) -> None:
        try:
            self._smtp = smtplib.SMTP(self._host, self._port)
        except (OSError, _SMTP_EXCEPTION) as exc:
            raise SmtpConnectionError(
                f"Plain (no-TLS) connection to {self._host}:{self._port} "
                f"failed: {exc}"
            ) from exc

    def _authenticate(self) -> None:
        if self._smtp is None:
            raise RuntimeError("_authenticate() called before _connect_*()")
        try:
            self._smtp.login(self._username, self._password)
        except _SMTP_EXCEPTION as exc:
            raise SmtpAuthError(
                f"Authentication failed for user {self._username!r} "
                f"on {self._host}:{self._port}: {exc}"
            ) from exc

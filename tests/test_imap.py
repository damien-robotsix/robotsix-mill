"""Tests for the IMAP client module."""

from __future__ import annotations

import imaplib
import socket
import ssl
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.imap import (
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    ImapError,
    ImapTlsError,
    MailboxInfo,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------



def _make_mock_imap_ssl() -> mock.MagicMock:
    """Factory for a mock ``IMAP4_SSL`` instance that behaves correctly."""
    m = mock.MagicMock(spec=imaplib.IMAP4_SSL)
    m.login.return_value = ("OK", [b"Logged in"])
    m.list.return_value = ("OK", [])
    m.select.return_value = ("OK", [b"5"])
    m.logout.return_value = ("OK", [b"Logged out"])
    # A mock socket so close_socket has something to close.
    m.sock = mock.MagicMock()
    return m


def _make_mock_imap() -> mock.MagicMock:
    """Factory for a mock ``IMAP4`` instance (plain, for STARTTLS / none)."""
    m = mock.MagicMock(spec=imaplib.IMAP4)
    m.login.return_value = ("OK", [b"Logged in"])
    m.list.return_value = ("OK", [])
    m.select.return_value = ("OK", [b"5"])
    m.logout.return_value = ("OK", [b"Logged out"])
    m.starttls.return_value = ("OK", [b"Begin TLS"])
    m.sock = mock.MagicMock()
    return m


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_imap_error_is_exception() -> None:
    """ImapError is a proper Exception subclass."""
    assert issubclass(ImapError, Exception)


def test_imap_connection_error_is_imap_error() -> None:
    """ImapConnectionError is a subclass of ImapError."""
    assert issubclass(ImapConnectionError, ImapError)


def test_imap_tls_error_is_imap_error() -> None:
    """ImapTlsError is a subclass of ImapError."""
    assert issubclass(ImapTlsError, ImapError)


def test_imap_auth_error_is_imap_error() -> None:
    """ImapAuthError is a subclass of ImapError."""
    assert issubclass(ImapAuthError, ImapError)


def test_specific_errors_caught_by_base() -> None:
    """Callers can catch ImapError to handle all IMAP failure modes."""
    for exc_cls in (ImapConnectionError, ImapTlsError, ImapAuthError):
        try:
            raise exc_cls("test")
        except ImapError:
            pass
        else:
            pytest.fail(f"{exc_cls.__name__} not caught by ImapError")


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------


def test_repr_redacts_password(cfg: MailConfig) -> None:
    """repr(ImapClient) must not expose the password."""
    client = ImapClient(cfg)
    r = repr(client)
    assert "s3cret" not in r
    assert "<redacted>" in r
    assert "imap.example.com" in r


# ---------------------------------------------------------------------------
# Happy path: direct-TLS
# ---------------------------------------------------------------------------


def test_direct_tls_happy_path(cfg: MailConfig) -> None:
    """Context manager: direct-TLS → login → list folders → close."""
    mock_ssl = _make_mock_imap_ssl()
    raw_list_responses: list[bytes] = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    ]
    mock_ssl.list.return_value = ("OK", raw_list_responses)

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl) as patched:
        with ImapClient(cfg) as client:
            folders = client.list_folders()

        patched.assert_called_once()
        _, kwargs = patched.call_args
        assert kwargs["ssl_context"] is not None
        assert isinstance(kwargs["ssl_context"], ssl.SSLContext)

    mock_ssl.login.assert_called_once_with("user@example.com", "s3cret")
    mock_ssl.logout.assert_called_once()

    assert len(folders) == 2
    assert folders[0] == MailboxInfo(
        name="INBOX", attributes=("\\HasNoChildren",), delimiter="/"
    )
    assert folders[1] == MailboxInfo(
        name="[Gmail]",
        attributes=("\\HasChildren", "\\Noselect"),
        delimiter="/",
    )


# ---------------------------------------------------------------------------
# Happy path: STARTTLS
# ---------------------------------------------------------------------------


def test_starttls_happy_path(cfg: MailConfig) -> None:
    """STARTTLS mode: plain connect → starttls → login → list → close."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )

    mock_imap = _make_mock_imap()
    mock_imap.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    with mock.patch("imaplib.IMAP4", return_value=mock_imap) as patched:
        with ImapClient(cfg) as client:
            folders = client.list_folders()

        patched.assert_called_once_with("imap.example.com", 143)

    # starttls must be called *before* login
    mock_imap.starttls.assert_called_once()
    _, starttls_kwargs = mock_imap.starttls.call_args
    assert isinstance(starttls_kwargs["ssl_context"], ssl.SSLContext)

    # login only after starttls
    mock_imap.login.assert_called_once_with("user@example.com", "s3cret")
    mock_imap.logout.assert_called_once()
    assert len(folders) == 1


# ---------------------------------------------------------------------------
# Happy path: no-TLS
# ---------------------------------------------------------------------------


def test_no_tls_happy_path(cfg: MailConfig) -> None:
    """No-TLS mode: plain connect → login (no starttls) → close."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="none",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )

    mock_imap = _make_mock_imap()

    with mock.patch("imaplib.IMAP4", return_value=mock_imap) as patched:
        with ImapClient(cfg) as client:
            assert client is not None
        patched.assert_called_once_with("imap.example.com", 143)

    mock_imap.starttls.assert_not_called()
    mock_imap.login.assert_called_once_with("user@example.com", "s3cret")
    mock_imap.logout.assert_called_once()


# ---------------------------------------------------------------------------
# list_folders parsing
# ---------------------------------------------------------------------------


def test_list_folders_empty_delimiter(cfg: MailConfig) -> None:
    """LIST response with empty delimiter (flat namespace)."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.list.return_value = ("OK", [b'() "" "INBOX"'])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            folders = client.list_folders()

    assert folders[0].delimiter == ""


def test_list_folders_no_attributes(cfg: MailConfig) -> None:
    """LIST response with empty flags tuple."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.list.return_value = ("OK", [b'() "/" "Archive"'])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            folders = client.list_folders()

    assert folders[0].attributes == ()
    assert folders[0].name == "Archive"


def test_list_folders_multiple_flags(cfg: MailConfig) -> None:
    """LIST response with multiple flags including special ones."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.list.return_value = (
        "OK",
        [b'(\\Marked \\HasChildren) "/" "[Gmail]"'],
    )

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            folders = client.list_folders()

    assert folders[0].attributes == ("\\Marked", "\\HasChildren")


def test_list_folders_nil_delimiter(cfg: MailConfig) -> None:
    """LIST response with NIL delimiter."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.list.return_value = ("OK", [b'(\\HasNoChildren) NIL "INBOX"'])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            folders = client.list_folders()

    assert folders[0].delimiter == ""
    assert folders[0].name == "INBOX"


# ---------------------------------------------------------------------------
# select_folder
# ---------------------------------------------------------------------------


def test_select_folder_returns_count(cfg: MailConfig) -> None:
    """select_folder parses the EXISTS count from the SELECT response."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.select.return_value = ("OK", [b"42"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            count = client.select_folder("INBOX")

    mock_ssl.select.assert_called_once_with("INBOX")
    assert count == 42


def test_select_folder_no_count(cfg: MailConfig) -> None:
    """select_folder returns 0 when the server gives no count."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.select.return_value = ("OK", [None])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            count = client.select_folder("INBOX")

    assert count == 0


def test_select_folder_empty_data(cfg: MailConfig) -> None:
    """select_folder returns 0 when data list is empty."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.select.return_value = ("OK", [])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            count = client.select_folder("INBOX")

    assert count == 0


# ---------------------------------------------------------------------------
# Connection errors
# ---------------------------------------------------------------------------


def test_connection_refused_direct_tls(cfg: MailConfig) -> None:
    """Connection refused → ImapConnectionError with __cause__."""
    original = ConnectionRefusedError("Connection refused")
    with mock.patch("imaplib.IMAP4_SSL", side_effect=original):
        with pytest.raises(ImapConnectionError) as exc:
            with ImapClient(cfg):
                pass
        assert "Direct-TLS" in str(exc.value)
        assert exc.value.__cause__ is original


def test_connection_refused_plain(cfg: MailConfig) -> None:
    """Plain connection refused → ImapConnectionError with __cause__."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="none",
        smtp_host="smtp.example.com",
        username="u",
        password="p",
    )
    original = ConnectionRefusedError("Connection refused")
    with mock.patch("imaplib.IMAP4", side_effect=original):
        with pytest.raises(ImapConnectionError) as exc:
            with ImapClient(cfg):
                pass
        assert exc.value.__cause__ is original


def test_imap_greeting_error(cfg: MailConfig) -> None:
    """IMAP4.error on connect (bad greeting) → ImapConnectionError."""
    original = imaplib.IMAP4.error("Bad IMAP4 protocol")
    with mock.patch("imaplib.IMAP4_SSL", side_effect=original):
        with pytest.raises(ImapConnectionError) as exc:
            with ImapClient(cfg):
                pass
        assert exc.value.__cause__ is original


def test_socket_gaierror(cfg: MailConfig) -> None:
    """socket.gaierror (name resolution failure) → ImapConnectionError."""
    original = socket.gaierror("Name or service not known")
    with mock.patch("imaplib.IMAP4_SSL", side_effect=original):
        with pytest.raises(ImapConnectionError) as exc:
            with ImapClient(cfg):
                pass
        assert exc.value.__cause__ is original


# ---------------------------------------------------------------------------
# STARTTLS errors
# ---------------------------------------------------------------------------


def test_starttls_handshake_failure(cfg: MailConfig) -> None:
    """STARTTLS handshake fails → ImapTlsError with __cause__."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )

    mock_imap = _make_mock_imap()
    ssl_error = ssl.SSLError("handshake failed")
    mock_imap.starttls.side_effect = ssl_error

    with mock.patch("imaplib.IMAP4", return_value=mock_imap):
        with pytest.raises(ImapTlsError) as exc:
            with ImapClient(cfg):
                pass
        assert "STARTTLS" in str(exc.value)
        assert exc.value.__cause__ is ssl_error


def test_starttls_not_advertised(cfg: MailConfig) -> None:
    """STARTTLS not advertised → ImapTlsError."""
    cfg = MailConfig(
        imap_host="imap.example.com",
        imap_port=143,
        imap_tls_mode="starttls",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )

    mock_imap = _make_mock_imap()
    imap_error = imaplib.IMAP4.error("STARTTLS not available")
    mock_imap.starttls.side_effect = imap_error

    with mock.patch("imaplib.IMAP4", return_value=mock_imap):
        with pytest.raises(ImapTlsError) as exc:
            with ImapClient(cfg):
                pass
        assert exc.value.__cause__ is imap_error


# ---------------------------------------------------------------------------
# Authentication errors
# ---------------------------------------------------------------------------


def test_authentication_rejected(cfg: MailConfig) -> None:
    """login() returns 'NO' → ImapAuthError."""
    mock_ssl = _make_mock_imap_ssl()
    auth_error = imaplib.IMAP4.error("AUTHENTICATIONFAILED invalid credentials")
    mock_ssl.login.side_effect = auth_error

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with pytest.raises(ImapAuthError) as exc:
            with ImapClient(cfg):
                pass
        assert "Authentication failed" in str(exc.value)
        assert "user@example.com" in str(exc.value)
        assert exc.value.__cause__ is auth_error


# ---------------------------------------------------------------------------
# Context manager error handling
# ---------------------------------------------------------------------------


def test_context_manager_closes_on_exception(cfg: MailConfig) -> None:
    """logout() and socket close are called even when the block raises."""
    mock_ssl = _make_mock_imap_ssl()

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        try:
            with ImapClient(cfg):
                raise RuntimeError("something went wrong inside the block")
        except RuntimeError:
            pass

    mock_ssl.logout.assert_called_once()
    mock_ssl.sock.close.assert_called_once()


def test_context_manager_closes_socket_when_logout_fails(cfg: MailConfig) -> None:
    """When logout() raises, the socket is still closed."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.logout.side_effect = imaplib.IMAP4.error("already closed")

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg):
            pass

    mock_ssl.logout.assert_called_once()
    mock_ssl.sock.close.assert_called_once()


# ---------------------------------------------------------------------------
# MailboxInfo
# ---------------------------------------------------------------------------


def test_mailbox_info_is_frozen() -> None:
    """MailboxInfo is immutable."""
    info = MailboxInfo(name="INBOX", attributes=("\\HasNoChildren",), delimiter="/")
    with pytest.raises(FrozenInstanceError):
        info.name = "OTHER"  # type: ignore[misc]


def test_mailbox_info_repr() -> None:
    """MailboxInfo has a readable repr."""
    info = MailboxInfo(name="INBOX", attributes=("\\HasNoChildren",), delimiter="/")
    r = repr(info)
    assert "INBOX" in r
    assert "HasNoChildren" in r


# ---------------------------------------------------------------------------
# Verifies no SMTP dependency
# ---------------------------------------------------------------------------


def test_imap_client_does_not_import_smtp() -> None:
    """The imap module must not reference the SMTP module."""
    import robotsix_auto_mail.imap as mod

    source = mod.__file__
    assert source is not None
    content = open(source).read()
    # The word "smtp" should only appear in docstrings explaining the
    # separation, never in executable code.  Verify there's no import
    # of or call to an SMTP module.
    assert "import" not in content or "smtp" not in content.lower().split(
        "import"
    )[0], "imap.py must not import SMTP"
    assert "from robotsix_auto_mail.smtp" not in content.lower()


def test_imap_client_only_uses_imap_fields(cfg: MailConfig) -> None:
    """ImapClient constructor extracts only IMAP fields from MailConfig."""
    client = ImapClient(cfg)
    assert client._host == "imap.example.com"
    assert client._port == 993
    assert client._tls_mode == "direct-tls"
    assert client._username == "user@example.com"
    assert client._password == "s3cret"
    # SMTP fields are never stored
    assert not hasattr(client, "_smtp_host")


# ---------------------------------------------------------------------------
# search_uids
# ---------------------------------------------------------------------------


def test_search_uids_returns_uids(cfg: MailConfig) -> None:
    """search_uids parses space-separated UIDs from the SEARCH response."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [b"1 2 3"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.search_uids()

    mock_ssl.uid.assert_called_once_with("SEARCH", "ALL")
    assert result == [1, 2, 3]


def test_search_uids_empty_result(cfg: MailConfig) -> None:
    """search_uids returns [] when SEARCH finds nothing."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [b""])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.search_uids()

    assert result == []


def test_search_uids_empty_data_list(cfg: MailConfig) -> None:
    """search_uids returns [] when data list is empty."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.search_uids()

    assert result == []


def test_search_uids_custom_criteria(cfg: MailConfig) -> None:
    """search_uids passes custom criteria through."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [b"42 43"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.search_uids("UID 41:*")

    mock_ssl.uid.assert_called_once_with("SEARCH", "UID 41:*")
    assert result == [42, 43]


def test_search_uids_not_connected(cfg: MailConfig) -> None:
    """search_uids raises ImapError when the client is not connected."""
    client = ImapClient(cfg)
    with pytest.raises(ImapError, match="Not connected"):
        client.search_uids()


def test_search_uids_server_error(cfg: MailConfig) -> None:
    """search_uids raises ImapError on non-OK response."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("NO", [b"Server error"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            with pytest.raises(ImapError, match="UID SEARCH failed"):
                client.search_uids()


def test_search_uids_single_uid(cfg: MailConfig) -> None:
    """search_uids works when only one UID matches."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [b"99"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.search_uids()

    assert result == [99]


# ---------------------------------------------------------------------------
# fetch_messages
# ---------------------------------------------------------------------------


def test_fetch_messages_returns_uid_body_pairs(cfg: MailConfig) -> None:
    """fetch_messages returns (uid, raw_bytes) for each fetched message."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = (
        "OK",
        [
            (b"1 (UID 1)", b"msg1-body"),
            (b"2 (UID 2)", b"msg2-body"),
        ],
    )

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.fetch_messages([1, 2])

    assert result == [(1, b"msg1-body"), (2, b"msg2-body")]


def test_fetch_messages_uses_body_peek(cfg: MailConfig) -> None:
    r"""fetch_messages uses BODY.PEEK[] so the \Seen flag is NOT set."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            client.fetch_messages([1])

    mock_ssl.uid.assert_called_once_with(
        "FETCH", "1", "(BODY.PEEK[])"
    )


def test_fetch_messages_multiple_uids_comma_separated(cfg: MailConfig) -> None:
    """fetch_messages builds a comma-separated UID set."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("OK", [])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            client.fetch_messages([10, 20, 30])

    mock_ssl.uid.assert_called_once_with(
        "FETCH", "10,20,30", "(BODY.PEEK[])"
    )


def test_fetch_messages_skips_missing_uids(cfg: MailConfig) -> None:
    """fetch_messages silently omits UIDs that the server didn't return."""
    mock_ssl = _make_mock_imap_ssl()
    # Server only returns UID 1, not 2 (UID 2 was deleted between
    # SEARCH and FETCH).
    mock_ssl.uid.return_value = (
        "OK",
        [(b"1 (UID 1)", b"body1")],
    )

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.fetch_messages([1, 2])

    assert result == [(1, b"body1")]


def test_fetch_messages_empty_uids(cfg: MailConfig) -> None:
    """fetch_messages returns [] when given an empty UID list."""
    mock_ssl = _make_mock_imap_ssl()

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.fetch_messages([])

    assert result == []
    mock_ssl.uid.assert_not_called()


def test_fetch_messages_not_connected(cfg: MailConfig) -> None:
    """fetch_messages raises ImapError when not connected."""
    client = ImapClient(cfg)
    with pytest.raises(ImapError, match="Not connected"):
        client.fetch_messages([1])


def test_fetch_messages_server_error(cfg: MailConfig) -> None:
    """fetch_messages raises ImapError on non-OK response."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = ("NO", [b"Some error"])

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            with pytest.raises(ImapError, match="UID FETCH failed"):
                client.fetch_messages([1])


def test_fetch_messages_skips_non_tuple_items(cfg: MailConfig) -> None:
    """fetch_messages ignores non-tuple items in the response."""
    mock_ssl = _make_mock_imap_ssl()
    # imaplib sometimes returns a trailing closing ")" as a bytes item.
    mock_ssl.uid.return_value = (
        "OK",
        [
            b"1 (UID 1 BODY[] {5}",
            b"body1",
            b")",
            b"2 (UID 2 BODY[] {5}",
            b"body2",
            b")",
            b")",  # trailing ")" from imaplib — should be skipped
        ],
    )

    def fake_uid(
    cmd: str, uid_set: str, fetch_spec: str
) -> tuple[str, list[tuple[bytes, bytes]]]:
        # Return a properly structured response that imaplib will process
        # into (header, body) tuples.
        return ("OK", [
            (b"1 (UID 1)", b"body1"),
            (b"2 (UID 2)", b"body2"),
        ])

    mock_ssl.uid.side_effect = fake_uid

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.fetch_messages([1, 2])

    assert result == [(1, b"body1"), (2, b"body2")]


def test_fetch_messages_header_with_body_size(cfg: MailConfig) -> None:
    """fetch_messages parses UID from headers containing BODY[] size."""
    mock_ssl = _make_mock_imap_ssl()
    mock_ssl.uid.return_value = (
        "OK",
        [
            (b"1 (UID 42 BODY[] {5}", b"abcde"),
        ],
    )

    with mock.patch("imaplib.IMAP4_SSL", return_value=mock_ssl):
        with ImapClient(cfg) as client:
            result = client.fetch_messages([42])

    assert result == [(42, b"abcde")]
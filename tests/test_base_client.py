"""Tests for the shared protocol base class (``_ProtocolClient``)."""

from __future__ import annotations

import secrets
from unittest import mock

import pytest

from robotsix_auto_mail._base_client import _ProtocolClient

# Runtime-generated fake passwords avoid CodeQL py/hardcoded-credentials
# false positives on these test fixtures (no real secret involved).
_FAKE_PASSWORD = secrets.token_urlsafe(8)
_FAKE_PASSWORD_ALT = secrets.token_urlsafe(8)


class _FakeClient(_ProtocolClient):
    """Minimal concrete subclass that spies on the connection helpers."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        tls_mode: str,
        username: str,
        password: str,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            tls_mode=tls_mode,
            username=username,
            password=password,
        )
        self.direct_tls_spy = mock.MagicMock()
        self.starttls_spy = mock.MagicMock()
        self.plain_spy = mock.MagicMock()

    def _connect_direct_tls(self) -> None:
        self.direct_tls_spy()

    def _connect_starttls(self) -> None:
        self.starttls_spy()

    def _connect_plain(self) -> None:
        self.plain_spy()


def _make_client(**overrides: object) -> _FakeClient:
    """Build a ``_FakeClient`` with sensible defaults, overridable per test."""
    fields = {
        "host": "mail.example.com",
        "port": 993,
        "tls_mode": "direct-tls",
        "username": "user@example.com",
        "password": _FAKE_PASSWORD,
    }
    fields.update(overrides)
    return _FakeClient(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# __init__ storage
# ---------------------------------------------------------------------------


def test_init_stores_all_fields() -> None:
    """__init__ stores all five config fields on private attributes."""
    client = _make_client(
        host="imap.example.com",
        port=143,
        tls_mode="starttls",
        username="alice@example.com",
        password=_FAKE_PASSWORD_ALT,
    )
    assert client._host == "imap.example.com"
    assert client._port == 143
    assert client._tls_mode == "starttls"
    assert client._username == "alice@example.com"
    assert client._password == _FAKE_PASSWORD_ALT


# ---------------------------------------------------------------------------
# __repr__ format and redaction
# ---------------------------------------------------------------------------


def test_repr_format_and_redaction() -> None:
    """repr() shows class/host/port/user and redacts the password."""
    client = _make_client(
        host="imap.example.com",
        port=143,
        tls_mode="starttls",
        username="alice@example.com",
        password=_FAKE_PASSWORD_ALT,
    )
    r = repr(client)
    assert type(client).__name__ in r
    assert "imap.example.com" in r
    assert "143" in r
    assert "alice@example.com" in r
    assert "<redacted>" in r
    assert _FAKE_PASSWORD_ALT not in r


# ---------------------------------------------------------------------------
# _dispatch_tls happy-path routing
# ---------------------------------------------------------------------------


def test_dispatch_tls_direct_tls() -> None:
    """_dispatch_tls() with 'direct-tls' calls only _connect_direct_tls."""
    client = _make_client(tls_mode="direct-tls")
    client._dispatch_tls()
    client.direct_tls_spy.assert_called_once_with()
    client.starttls_spy.assert_not_called()
    client.plain_spy.assert_not_called()


def test_dispatch_tls_starttls() -> None:
    """_dispatch_tls() with 'starttls' calls only _connect_starttls."""
    client = _make_client(tls_mode="starttls")
    client._dispatch_tls()
    client.starttls_spy.assert_called_once_with()
    client.direct_tls_spy.assert_not_called()
    client.plain_spy.assert_not_called()


def test_dispatch_tls_none() -> None:
    """_dispatch_tls() with 'none' calls only _connect_plain."""
    client = _make_client(tls_mode="none")
    client._dispatch_tls()
    client.plain_spy.assert_called_once_with()
    client.direct_tls_spy.assert_not_called()
    client.starttls_spy.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_tls error handling
# ---------------------------------------------------------------------------


def test_dispatch_tls_unknown_mode_raises() -> None:
    """_dispatch_tls() raises ValueError for an unknown mode, no helper run."""
    client = _make_client(tls_mode="bogus")
    with pytest.raises(ValueError) as exc:
        client._dispatch_tls()
    assert "bogus" in str(exc.value)
    client.direct_tls_spy.assert_not_called()
    client.starttls_spy.assert_not_called()
    client.plain_spy.assert_not_called()

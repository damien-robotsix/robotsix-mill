from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.db import MailRecord, init_db


@pytest.fixture(autouse=True)
def _isolate_env() -> Generator[None, None, None]:
    """Strip MAIL_* / LLM_* env vars before each test; restore after."""
    saved: dict[str, str] = {}
    for key in list(os.environ):
        if key.startswith("MAIL_") or key.startswith("LLM_"):
            saved[key] = os.environ.pop(key)
    yield
    for key, value in saved.items():
        os.environ[key] = value


@pytest.fixture(autouse=True)
def _block_network() -> Generator[None, None, None]:
    """Block socket.create_connection so no test accidentally hits the network.

    Localhost connections (127.0.0.1 / ::1) are allowed so that tests
    which spin up a local HTTP server (e.g. test_server.py) still work.
    """
    original = socket.create_connection

    def _blocked(address: Any, *args: Any, **kwargs: Any) -> Any:
        # Allow localhost connections for tests that run a local server.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(address, *args, **kwargs)
        raise RuntimeError(
            "Test attempted a real network connection via socket.create_connection. "
            "Mock the IMAP/SMTP client instead."
        )

    socket.create_connection = _blocked
    yield
    socket.create_connection = original


@pytest.fixture
def cfg() -> MailConfig:
    """A MailConfig with placeholder credentials suitable for most tests."""
    return MailConfig(
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="user@example.com",
        password="s3cret",
    )


@pytest.fixture
def conn() -> Generator[sqlite3.Connection, None, None]:
    """In-memory SQLite connection with the application schema applied."""
    c = init_db(":memory:")
    yield c
    c.close()


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """A file-backed database path inside pytest's tmp_path."""
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Shared test helpers
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

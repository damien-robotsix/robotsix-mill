from __future__ import annotations

import os
import socket
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.db import init_db


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

    def _blocked(address: object, *args: object, **kwargs: object) -> object:
        # Allow localhost connections for tests that run a local server.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(address, *args, **kwargs)
        raise RuntimeError(
            "Test attempted a real network connection via socket.create_connection. "
            "Mock the IMAP/SMTP client instead."
        )

    socket.create_connection = _blocked  # type: ignore[assignment]
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

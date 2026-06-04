"""Tests for the self-managed archive folder structure subsystem."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest
from robotsix_llmio.core import Tier

from robotsix_auto_mail.archive import (
    _ARCHIVE_WATERMARK_KEY,
    ARCHIVE_ROOT,
    ArchiveError,
    ArchiveStructure,
    determine_archive_structure,
    setup_archive,
)
from robotsix_auto_mail.db import get_watermark, init_db, set_watermark
from robotsix_auto_mail.imap import MailboxInfo


class _FakeImapClient:
    """Minimal stand-in exposing list_folders() and create_folder()."""

    def __init__(self, folders: list[MailboxInfo]) -> None:
        self._folders = folders
        self.created: list[str] = []

    def list_folders(self) -> list[MailboxInfo]:
        return self._folders

    def create_folder(self, name: str) -> None:
        self.created.append(name)


def _folder(name: str, delimiter: str = "/") -> MailboxInfo:
    return MailboxInfo(name=name, attributes=(), delimiter=delimiter)


def _patch_llm(folders: list[str]) -> mock._patch[mock.MagicMock]:
    """Patch OpenRouterDeepseekProvider to return *folders* from the LLM."""
    mock_run_result = mock.MagicMock()
    mock_run_result.output = ArchiveStructure(folders=folders)
    mock_handle = mock.MagicMock()
    mock_handle.run_sync.return_value = mock_run_result

    mock_provider = mock.MagicMock()
    mock_provider.build_agent.return_value = mock_handle
    mock_provider.call_with_retry.side_effect = lambda fn, what: fn()

    return mock.patch(
        "robotsix_auto_mail.archive.OpenRouterDeepseekProvider",
        return_value=mock_provider,
    )


# ---------------------------------------------------------------------------
# ArchiveStructure
# ---------------------------------------------------------------------------


def test_archive_structure_defaults_empty() -> None:
    """folders defaults to an empty list."""
    assert ArchiveStructure().folders == []


def test_archive_structure_accepts_folders() -> None:
    """folders is populated from input."""
    s = ArchiveStructure(folders=["a", "a/b"])
    assert s.folders == ["a", "a/b"]


# ---------------------------------------------------------------------------
# ArchiveError
# ---------------------------------------------------------------------------


def test_archive_error_is_exception() -> None:
    err = ArchiveError("boom")
    assert isinstance(err, Exception)
    assert str(err) == "boom"


# ---------------------------------------------------------------------------
# determine_archive_structure
# ---------------------------------------------------------------------------


def test_determine_archive_structure_success() -> None:
    """The model's relative sub-paths are returned."""
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        with _patch_llm(["Receipts", "Work/2024"]):
            result = determine_archive_structure(["INBOX", "Sent"])
    assert result == ["Receipts", "Work/2024"]


def test_determine_archive_structure_uses_cheap_tier() -> None:
    """build_agent is called with Tier.CHEAP by default."""
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        with mock.patch(
            "robotsix_auto_mail.archive.OpenRouterDeepseekProvider"
        ) as cls:
            mock_run_result = mock.MagicMock()
            mock_run_result.output = ArchiveStructure(folders=[])
            mock_handle = mock.MagicMock()
            mock_handle.run_sync.return_value = mock_run_result
            provider = cls.return_value
            provider.build_agent.return_value = mock_handle
            provider.call_with_retry.side_effect = lambda fn, what: fn()

            determine_archive_structure(["INBOX"])

        provider.build_agent.assert_called_once()
        assert provider.build_agent.call_args.kwargs["tier"] == Tier.CHEAP
        mock_handle.close.assert_called_once()


def test_determine_archive_structure_missing_api_key() -> None:
    """No api_key and no LLM_API_KEY env var → ArchiveError."""
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ArchiveError) as exc:
            determine_archive_structure(["INBOX"])
    assert "LLM_API_KEY" in str(exc.value)


def test_determine_archive_structure_llm_error_wrapped() -> None:
    """A call_with_retry failure is wrapped in ArchiveError."""
    with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}, clear=True):
        mock_handle = mock.MagicMock()
        mock_provider = mock.MagicMock()
        mock_provider.build_agent.return_value = mock_handle
        mock_provider.call_with_retry.side_effect = RuntimeError("timeout")
        with mock.patch(
            "robotsix_auto_mail.archive.OpenRouterDeepseekProvider",
            return_value=mock_provider,
        ):
            with pytest.raises(ArchiveError) as exc:
                determine_archive_structure(["INBOX"])
    assert "timeout" in str(exc.value)
    mock_handle.close.assert_called_once()


# ---------------------------------------------------------------------------
# setup_archive — first run
# ---------------------------------------------------------------------------


def test_setup_archive_first_run_creates_and_persists() -> None:
    """First run lists folders, creates archive folders, persists, returns."""
    conn = init_db(":memory:")
    try:
        client = _FakeImapClient([_folder("INBOX"), _folder("Sent")])
        with mock.patch.dict(
            os.environ, {"LLM_API_KEY": "sk-test"}, clear=True
        ):
            with _patch_llm(["Receipts", "Work/2024"]):
                result = setup_archive(conn, client)

        expected = [
            ARCHIVE_ROOT,
            f"{ARCHIVE_ROOT}/Receipts",
            f"{ARCHIVE_ROOT}/Work/2024",
        ]
        assert result == expected
        assert client.created == expected
        stored = get_watermark(conn, _ARCHIVE_WATERMARK_KEY)
        assert stored is not None
        assert json.loads(stored) == expected
    finally:
        conn.close()


def test_setup_archive_translates_delimiter() -> None:
    """Sub-path separators are translated to the server delimiter."""
    conn = init_db(":memory:")
    try:
        client = _FakeImapClient([_folder("INBOX", delimiter=".")])
        with mock.patch.dict(
            os.environ, {"LLM_API_KEY": "sk-test"}, clear=True
        ):
            with _patch_llm(["Work/2024"]):
                result = setup_archive(conn, client)
        assert result == [ARCHIVE_ROOT, f"{ARCHIVE_ROOT}.Work.2024"]
        assert client.created == result
    finally:
        conn.close()


def test_setup_archive_skips_existing_folders() -> None:
    """Folders already present on the server are not recreated."""
    conn = init_db(":memory:")
    try:
        client = _FakeImapClient(
            [_folder("INBOX"), _folder(ARCHIVE_ROOT)]
        )
        with mock.patch.dict(
            os.environ, {"LLM_API_KEY": "sk-test"}, clear=True
        ):
            with _patch_llm(["Receipts"]):
                result = setup_archive(conn, client)
        assert result == [ARCHIVE_ROOT, f"{ARCHIVE_ROOT}/Receipts"]
        # ARCHIVE_ROOT already existed → only the sub-folder is created.
        assert client.created == [f"{ARCHIVE_ROOT}/Receipts"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# setup_archive — subsequent run
# ---------------------------------------------------------------------------


def test_setup_archive_subsequent_run_short_circuits() -> None:
    """Watermark present → no folder listing, no LLM, no create_folder."""
    conn = init_db(":memory:")
    try:
        persisted = [ARCHIVE_ROOT, f"{ARCHIVE_ROOT}/Receipts"]
        set_watermark(
            conn, _ARCHIVE_WATERMARK_KEY, json.dumps(persisted)
        )
        client = mock.MagicMock()
        with mock.patch(
            "robotsix_auto_mail.archive.OpenRouterDeepseekProvider"
        ) as cls:
            result = setup_archive(conn, client)
        assert result == persisted
        client.list_folders.assert_not_called()
        client.create_folder.assert_not_called()
        cls.assert_not_called()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# setup_archive — no API key fallback
# ---------------------------------------------------------------------------


def test_setup_archive_no_api_key_falls_back_to_root() -> None:
    """Without an LLM key, only the root is created and persisted."""
    conn = init_db(":memory:")
    try:
        client = _FakeImapClient([_folder("INBOX")])
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch(
                "robotsix_auto_mail.archive.OpenRouterDeepseekProvider"
            ) as cls:
                result = setup_archive(conn, client)
        assert result == [ARCHIVE_ROOT]
        assert client.created == [ARCHIVE_ROOT]
        cls.assert_not_called()
        stored = get_watermark(conn, _ARCHIVE_WATERMARK_KEY)
        assert stored is not None
        assert json.loads(stored) == [ARCHIVE_ROOT]
    finally:
        conn.close()

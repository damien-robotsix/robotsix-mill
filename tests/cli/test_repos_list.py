"""Tests for the ``repos list`` CLI subcommand (_repos_list)."""

import argparse
from unittest.mock import MagicMock

import pytest

from robotsix_mill.cli.serve import _repos_list
from robotsix_mill.config import ReposRegistry, RepoConfig


def _make_repo_config(repo_id: str, board_id: str, source: str) -> RepoConfig:
    """Build a minimal RepoConfig for testing."""
    return RepoConfig(
        repo_id=repo_id,
        
        langfuse_project_name="test-proj",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        source=source,  # type: ignore[arg-type]
    )


@pytest.fixture
def _patch_get_repos_config(monkeypatch, request):
    """Fixture to monkeypatch get_repos_config for _repos_list tests."""
    repos = request.param
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config",
        lambda: repos,
    )


@pytest.mark.parametrize(
    "_patch_get_repos_config",
    [
        ReposRegistry(
            repos={"my-repo": _make_repo_config("my-repo", "board-1", "config")}
        )
    ],
    indirect=True,
)
def test_repos_list_shows_source_config(capsys, _patch_get_repos_config):
    """Entries from the operator repos: block display ``config``."""
    args = argparse.Namespace()
    result = _repos_list(args, MagicMock())
    assert result == 0
    captured = capsys.readouterr()
    assert "config" in captured.out
    assert "my-repo" in captured.out
    assert "board-1" in captured.out


@pytest.mark.parametrize(
    "_patch_get_repos_config",
    [
        ReposRegistry(
            repos={"auto-repo": _make_repo_config("auto-repo", "board-2", "auto")}
        )
    ],
    indirect=True,
)
def test_repos_list_shows_source_auto(capsys, _patch_get_repos_config):
    """Entries auto-registered display ``auto``."""
    args = argparse.Namespace()
    result = _repos_list(args, MagicMock())
    assert result == 0
    captured = capsys.readouterr()
    assert "auto" in captured.out
    assert "auto-repo" in captured.out


def test_repos_list_header_has_source_column(capsys, monkeypatch):
    """The header line must contain ``SOURCE``."""
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config",
        lambda: ReposRegistry(repos={}),
    )
    args = argparse.Namespace()
    result = _repos_list(args, MagicMock())
    assert result == 0
    captured = capsys.readouterr()
    assert "SOURCE" in captured.out

"""Tests for RepoConfig.follows_robotsix_standards tri-state resolution."""

from robotsix_mill.config import RepoConfig


def _repo(repo_id: str, **kwargs) -> RepoConfig:
    return RepoConfig(
        repo_id=repo_id,
        board_id=repo_id,
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        **kwargs,
    )


def test_default_none_fleet_prefix_detected():
    assert _repo("robotsix-chat").follows_robotsix_standards() is True


def test_default_none_non_fleet_prefix_not_detected():
    assert _repo("hexarchy").follows_robotsix_standards() is False


def test_explicit_true_overrides_non_fleet_id():
    assert (
        _repo("hexarchy", follows_standards=True).follows_robotsix_standards() is True
    )


def test_explicit_false_overrides_fleet_id():
    assert (
        _repo("robotsix-chat", follows_standards=False).follows_robotsix_standards()
        is False
    )

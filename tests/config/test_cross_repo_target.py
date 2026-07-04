"""Round-trip a ``cross_repo_target`` block from repos.yaml → RepoConfig.

Verifies the optional nested model loads, applies defaults, and is
absent (``None``) when the block is omitted — a repo without it behaves
exactly as before.
"""

from robotsix_mill.config import (
    CrossRepoTarget,
    load_repos_config,
)

_BASE_REPO = """\
repos:
  example:
    forge_remote_url: "https://github.com/me/example.git"
    langfuse:
      public_key: "pk"
      secret_key: "sk"
"""


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


def test_cross_repo_target_round_trips(tmp_path):
    body = (
        _BASE_REPO
        + """\
    cross_repo_target:
      upstream_remote_url: "https://github.com/up/example.git"
      fork_remote_url: "https://github.com/me/example.git"
      base_branch: "develop"
      auto_fork: true
"""
    )
    reg = load_repos_config(_write(tmp_path, body))
    cct = reg.repos["example"].cross_repo_target
    assert isinstance(cct, CrossRepoTarget)
    assert cct.upstream_remote_url == "https://github.com/up/example.git"
    assert cct.fork_remote_url == "https://github.com/me/example.git"
    assert cct.base_branch == "develop"
    assert cct.auto_fork is True


def test_cross_repo_target_defaults(tmp_path):
    body = (
        _BASE_REPO
        + """\
    cross_repo_target:
      upstream_remote_url: "https://github.com/up/example.git"
      fork_remote_url: "https://github.com/me/example.git"
"""
    )
    reg = load_repos_config(_write(tmp_path, body))
    cct = reg.repos["example"].cross_repo_target
    assert cct is not None
    assert cct.base_branch == "main"
    assert cct.auto_fork is False


def test_cross_repo_target_omitted_is_none(tmp_path):
    reg = load_repos_config(_write(tmp_path, _BASE_REPO))
    assert reg.repos["example"].cross_repo_target is None


def test_working_branch_round_trips(tmp_path):
    body = _BASE_REPO + '    working_branch: "lyrical"\n'
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["example"].working_branch == "lyrical"


def test_working_branch_omitted_is_none(tmp_path):
    reg = load_repos_config(_write(tmp_path, _BASE_REPO))
    assert reg.repos["example"].working_branch is None


def test_sandbox_image_round_trips(tmp_path):
    body = _BASE_REPO + '    sandbox_image: "ros:rolling-ros-base"\n'
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["example"].sandbox_image == "ros:rolling-ros-base"


def test_sandbox_image_omitted_is_none(tmp_path):
    reg = load_repos_config(_write(tmp_path, _BASE_REPO))
    assert reg.repos["example"].sandbox_image is None

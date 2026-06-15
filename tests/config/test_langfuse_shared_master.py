"""Validate the top-level ``langfuse_shared_master`` consolidation switch.

When set, every repo (and the meta board) is forced onto the named
master repo's Langfuse project, overriding any per-repo ``langfuse``
block — one switch to collapse the whole workspace into a single project.
"""

import pytest

from robotsix_mill.config import load_repos_config
from robotsix_mill.config.loader import ConfigError

_THREE_REPOS_PLUS_META = """\
langfuse_shared_master: "mill"
repos:
  mill:
    board_id: "mill"
    forge_remote_url: "https://github.com/org/mill.git"
    langfuse:
      project_name: "robotsix-mill"
      public_key: "pk-mill"
      secret_key: "sk-mill"
      base_url: "https://lf.example.com"
  other:
    board_id: "other"
    forge_remote_url: "https://github.com/org/other.git"
    langfuse:
      project_name: "robotsix-other"
      public_key: "pk-other"
      secret_key: "sk-other"
meta:
  langfuse:
    project_name: "robotsix-meta"
    public_key: "pk-meta"
    secret_key: "sk-meta"
"""


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


def test_shared_master_forces_all_repos_and_meta(tmp_path):
    reg = load_repos_config(_write(tmp_path, _THREE_REPOS_PLUS_META))
    # The master keeps its own keys; every other repo is overridden onto it.
    other = reg.repos["other"]
    assert other.langfuse_public_key == "pk-mill"
    assert other.langfuse_secret_key == "sk-mill"
    assert other.langfuse_project_name == "robotsix-mill"
    assert other.langfuse_base_url == "https://lf.example.com"
    assert reg.repos["mill"].langfuse_public_key == "pk-mill"
    # The meta board is consolidated too (its own project is overridden).
    assert reg.meta is not None
    assert reg.meta.langfuse_public_key == "pk-mill"
    assert reg.meta.langfuse_project_name == "robotsix-mill"


def test_shared_master_unknown_repo_raises(tmp_path):
    body = _THREE_REPOS_PLUS_META.replace(
        'langfuse_shared_master: "mill"', 'langfuse_shared_master: "ghost"'
    )
    with pytest.raises(ConfigError, match="not a known repo"):
        load_repos_config(_write(tmp_path, body))


def test_no_switch_leaves_per_repo_projects_intact(tmp_path):
    body = _THREE_REPOS_PLUS_META.replace('langfuse_shared_master: "mill"\n', "")
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["other"].langfuse_public_key == "pk-other"
    assert reg.repos["mill"].langfuse_public_key == "pk-mill"

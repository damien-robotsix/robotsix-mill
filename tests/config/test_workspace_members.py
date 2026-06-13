"""Tests for ``workspace_members.detect_workspace_members``.

The detector reads ``<repo_dir>/repos.yaml`` (a vcs2l manifest) plus the
master repo's per-member upstream policy from
``<repo_dir>/.robotsix-mill/config.yaml`` and returns a list of
``DetectedMember``. It must NEVER raise on any missing/malformed input —
a managed repo can't be allowed to crash mill by committing a broken
file.
"""

from __future__ import annotations

import logging

from robotsix_mill.config import CrossRepoTarget
from robotsix_mill.config.workspace_members import (
    DetectedMember,
    detect_workspace_members,
)


def _write_manifest(repo_dir, text: str):
    (repo_dir / "repos.yaml").write_text(text, encoding="utf-8")


def _write_config(repo_dir, text: str):
    cfg_dir = repo_dir / ".robotsix-mill"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(text, encoding="utf-8")


_TWO_MEMBER_MANIFEST = """\
repositories:
  src/zeta/pkg:
    type: git
    url: https://github.com/upstream/zeta.git
    version: lyrical
  src/alpha/pkg:
    type: git
    url: https://github.com/upstream/alpha.git
"""

_POLICY_CONFIG = """\
members:
  src/zeta/pkg:
    cross_repo_target:
      upstream_remote_url: https://github.com/upstream/zeta.git
      fork_remote_url: https://github.com/fork/zeta.git
      base_branch: lyrical
      auto_fork: true
"""


def test_happy_path(tmp_path):
    _write_manifest(tmp_path, _TWO_MEMBER_MANIFEST)
    _write_config(tmp_path, _POLICY_CONFIG)
    members = detect_workspace_members(tmp_path)
    assert [m.path for m in members] == ["src/alpha/pkg", "src/zeta/pkg"]
    alpha, zeta = members
    assert isinstance(alpha, DetectedMember)
    assert alpha.url == "https://github.com/upstream/alpha.git"
    assert alpha.version is None
    assert alpha.cross_repo_target is None
    assert zeta.url == "https://github.com/upstream/zeta.git"
    assert zeta.version == "lyrical"
    assert isinstance(zeta.cross_repo_target, CrossRepoTarget)
    assert zeta.cross_repo_target.fork_remote_url == "https://github.com/fork/zeta.git"
    assert zeta.cross_repo_target.base_branch == "lyrical"
    assert zeta.cross_repo_target.auto_fork is True


def test_none_repo_dir_returns_empty():
    assert detect_workspace_members(None) == []


def test_missing_manifest_returns_empty_no_warning(tmp_path, caplog):
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert caplog.records == []


def test_malformed_yaml_warns_and_returns_empty(tmp_path, caplog):
    _write_manifest(tmp_path, "repositories: [unterminated\n")
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert any("read/parse error" in r.message for r in caplog.records)


def test_non_mapping_top_level_warns_and_returns_empty(tmp_path, caplog):
    _write_manifest(tmp_path, "- just\n- a\n- list\n")
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert any("mapping" in r.message for r in caplog.records)


def test_missing_repositories_key_warns_and_returns_empty(tmp_path, caplog):
    _write_manifest(tmp_path, "other_key: value\n")
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert any("repositories" in r.message for r in caplog.records)


def test_repositories_not_mapping_warns_and_returns_empty(tmp_path, caplog):
    _write_manifest(tmp_path, "repositories:\n  - a\n  - b\n")
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert any("repositories" in r.message for r in caplog.records)


def test_member_missing_url_is_skipped_with_warning(tmp_path, caplog):
    _write_manifest(
        tmp_path,
        "repositories:\n"
        "  src/bad/pkg:\n"
        "    version: lyrical\n"
        "  src/good/pkg:\n"
        "    url: https://github.com/upstream/good.git\n",
    )
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        members = detect_workspace_members(tmp_path)
    assert [m.path for m in members] == ["src/good/pkg"]
    assert any("url" in r.message for r in caplog.records)


def test_member_empty_url_is_skipped(tmp_path, caplog):
    _write_manifest(
        tmp_path,
        "repositories:\n  src/bad/pkg:\n    url: '   '\n",
    )
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        assert detect_workspace_members(tmp_path) == []
    assert any("url" in r.message for r in caplog.records)


def test_member_entry_not_a_dict_is_skipped(tmp_path, caplog):
    _write_manifest(
        tmp_path,
        "repositories:\n"
        "  src/bad/pkg: just-a-string\n"
        "  src/good/pkg:\n"
        "    url: https://github.com/upstream/good.git\n",
    )
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        members = detect_workspace_members(tmp_path)
    assert [m.path for m in members] == ["src/good/pkg"]
    assert any("mapping" in r.message for r in caplog.records)


def test_member_without_version_is_none(tmp_path):
    _write_manifest(
        tmp_path,
        "repositories:\n  src/a/pkg:\n    url: https://github.com/upstream/a.git\n",
    )
    members = detect_workspace_members(tmp_path)
    assert len(members) == 1
    assert members[0].version is None


def test_no_master_config_all_cross_repo_target_none(tmp_path):
    _write_manifest(tmp_path, _TWO_MEMBER_MANIFEST)
    members = detect_workspace_members(tmp_path)
    assert len(members) == 2
    assert all(m.cross_repo_target is None for m in members)


def test_partial_cross_repo_target_tolerated(tmp_path, caplog):
    _write_manifest(
        tmp_path,
        "repositories:\n  src/a/pkg:\n    url: https://github.com/upstream/a.git\n",
    )
    _write_config(
        tmp_path,
        "members:\n"
        "  src/a/pkg:\n"
        "    cross_repo_target:\n"
        "      upstream_remote_url: https://github.com/upstream/a.git\n",
    )
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        members = detect_workspace_members(tmp_path)
    assert len(members) == 1
    assert members[0].cross_repo_target is None
    assert any("cross_repo_target" in r.message for r in caplog.records)


def test_members_not_a_mapping_tolerated(tmp_path, caplog):
    _write_manifest(tmp_path, _TWO_MEMBER_MANIFEST)
    _write_config(tmp_path, "members:\n  - a\n  - b\n")
    with caplog.at_level(
        logging.WARNING, logger="robotsix_mill.config.workspace_members"
    ):
        members = detect_workspace_members(tmp_path)
    assert len(members) == 2
    assert all(m.cross_repo_target is None for m in members)
    assert any("members" in r.message for r in caplog.records)


def test_policy_path_not_in_manifest_is_ignored(tmp_path):
    _write_manifest(
        tmp_path,
        "repositories:\n  src/a/pkg:\n    url: https://github.com/upstream/a.git\n",
    )
    _write_config(
        tmp_path,
        "members:\n"
        "  src/ghost/pkg:\n"
        "    cross_repo_target:\n"
        "      upstream_remote_url: https://github.com/upstream/ghost.git\n"
        "      fork_remote_url: https://github.com/fork/ghost.git\n",
    )
    members = detect_workspace_members(tmp_path)
    assert [m.path for m in members] == ["src/a/pkg"]
    assert members[0].cross_repo_target is None


def test_never_raises_on_malformed_inputs(tmp_path):
    # None.
    detect_workspace_members(None)
    # Missing manifest.
    detect_workspace_members(tmp_path)
    # Malformed YAML.
    _write_manifest(tmp_path, "repositories: [unterminated\n")
    detect_workspace_members(tmp_path)
    # Top-level list.
    _write_manifest(tmp_path, "- a\n- b\n")
    detect_workspace_members(tmp_path)
    # repositories not a mapping.
    _write_manifest(tmp_path, "repositories: scalar\n")
    detect_workspace_members(tmp_path)
    # member not a dict + bad policy.
    _write_manifest(tmp_path, "repositories:\n  p: x\n")
    _write_config(tmp_path, "members: not-a-mapping\n")
    detect_workspace_members(tmp_path)

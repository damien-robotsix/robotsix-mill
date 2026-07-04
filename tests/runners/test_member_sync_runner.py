"""Tests for the member-sync runner.

The deterministic member-sync pass clones a managed repo, detects its
vcs2l workspace members from ``repos.yaml``, and upserts them into
``config/repos.yaml`` via :func:`sync_workspace_members`. These tests
monkeypatch ``git_ops.clone`` to populate a temp "clone" dir with a
manifest, the ``Settings`` seam, and the forge-token resolver.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from robotsix_mill.config import RepoConfig, Settings, _reset_repos_config
from robotsix_mill.core import db
from robotsix_mill.runners.member_sync_runner import run_member_sync_pass
from robotsix_mill.repo_scaffold.member_sync import MemberSyncResult
from robotsix_mill.vcs import git_ops


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _master_cfg():
    return RepoConfig(
        repo_id="ros2-workspace",
        langfuse_project_name="proj-ros2",
        langfuse_public_key="pk-master",
        langfuse_secret_key="sk-master",
        forge_remote_url="https://github.com/robotsix/ros2-workspace.git",
    )


def _make_settings(tmp_path, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


def _install_seams(monkeypatch, settings, manifest: str | None):
    """Wire the Settings + token + clone seams.

    ``manifest`` is the YAML written into ``<clone>/repos.yaml`` by the
    fake clone; ``None`` populates the clone dir WITHOUT a manifest (the
    no-op path).
    """
    monkeypatch.setattr(
        "robotsix_mill.runners.member_sync_runner.Settings", lambda: settings
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.member_sync_runner._forge_token",
        lambda _s, _rc: None,
    )

    def fake_clone(url, dest, branch, token):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if manifest is not None:
            (dest / "repos.yaml").write_text(manifest, encoding="utf-8")

    monkeypatch.setattr(git_ops, "clone", fake_clone)


_MANIFEST = """\
repositories:
  src/zeta/pkg:
    type: git
    url: https://github.com/upstream/zeta.git
    version: lyrical
  src/alpha/pkg:
    type: git
    url: https://github.com/upstream/alpha.git
"""


def _read_registry(repos_file):
    with open(repos_file, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["repos"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_member_sync_pass_requires_repo_config(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.runners.member_sync_runner.Settings", lambda: settings
    )
    import pytest

    with pytest.raises(ValueError):
        run_member_sync_pass(session_id="sid", repo_config=None)


def test_run_member_sync_pass_adds_members(tmp_path, monkeypatch):
    repos_file = tmp_path / "repos.yaml"
    monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
    settings = _make_settings(tmp_path)
    _install_seams(monkeypatch, settings, _MANIFEST)
    # Member boards materialise on first ticket write.
    db.reset_engine()

    result = run_member_sync_pass(session_id="sid", repo_config=_master_cfg())

    assert isinstance(result, MemberSyncResult)
    assert sorted(result.added) == ["src-alpha-pkg", "src-zeta-pkg"]
    assert result.updated == []
    assert result.flagged_for_removal == []

    registry = _read_registry(repos_file)
    zeta = registry["src-zeta-pkg"]
    assert zeta["forge_remote_url"] == "https://github.com/upstream/zeta.git"
    assert zeta["working_branch"] == "lyrical"
    assert zeta["member_of"] == "ros2-workspace"
    # Langfuse is global now — member entries carry no per-repo langfuse config.
    assert "langfuse_from" not in zeta
    assert "langfuse" not in zeta
    _reset_repos_config()
    db.reset_engine()


def test_run_member_sync_pass_flags_removed_member(tmp_path, monkeypatch):
    repos_file = tmp_path / "repos.yaml"
    # Pre-seed a member that the manifest no longer lists.
    existing = {
        "repos": {
            "src-gone-pkg": {
                "board_id": "src-gone-pkg",
                "langfuse_from": "ros2-workspace",
                "member_of": "ros2-workspace",
                "forge_remote_url": "https://github.com/upstream/gone.git",
            },
        }
    }
    repos_file.write_text(yaml.dump(existing), encoding="utf-8")
    monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
    settings = _make_settings(tmp_path)
    _install_seams(monkeypatch, settings, _MANIFEST)
    db.reset_engine()

    result = run_member_sync_pass(session_id="sid", repo_config=_master_cfg())

    assert result.flagged_for_removal == ["src-gone-pkg"]
    registry = _read_registry(repos_file)
    assert registry["src-gone-pkg"]["pending_removal"] is True
    _reset_repos_config()
    db.reset_engine()


def test_run_member_sync_pass_no_manifest_is_noop(tmp_path, monkeypatch):
    repos_file = tmp_path / "repos.yaml"
    monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
    settings = _make_settings(tmp_path)
    # Clone populates the dir but commits NO repos.yaml manifest.
    _install_seams(monkeypatch, settings, None)

    result = run_member_sync_pass(session_id="sid", repo_config=_master_cfg())

    assert result.added == []
    assert result.updated == []
    assert result.flagged_for_removal == []
    # Silent no-op: no registry file written.
    assert not repos_file.exists()
    _reset_repos_config()

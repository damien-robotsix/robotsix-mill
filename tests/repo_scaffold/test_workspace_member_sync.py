"""Tests for ``robotsix_mill.repo_scaffold.member_sync``.

The sync turns detected vcs2l workspace members into ``config/repos.yaml``
registry entries: deriving repo_id/forge_remote_url/working_branch/
cross_repo_target, inheriting the master's Langfuse project, flagging
vanished members for operator removal, and filing build-out tickets on
each new member's board.
"""

from __future__ import annotations

import yaml

from robotsix_mill.config import (
    CrossRepoTarget,
    RepoConfig,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.repo_scaffold.member_sync import (
    _member_repo_id,
    sync_workspace_members,
)
from robotsix_mill.config.workspace_members import DetectedMember


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


def _master_cfg(repo_id="ros2-workspace"):
    return RepoConfig(
        repo_id=repo_id,
        board_id=repo_id,
        langfuse_project_name=f"proj-{repo_id}",
        langfuse_public_key="pk-master",
        langfuse_secret_key="sk-master",
        langfuse_base_url="https://lf.master.com",
    )


def _register_master(monkeypatch, master_cfg, *extra):
    """No-op now: member-sync no longer looks up the master via
    get_repos_config — it writes langfuse_from unconditionally."""
    pass


def _member(path, url, version=None, cross_repo_target=None):
    return DetectedMember(
        path=path, url=url, version=version, cross_repo_target=cross_repo_target
    )


def _read(repos_file):
    with open(repos_file, "r") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# _member_repo_id
# ---------------------------------------------------------------------------


class TestMemberRepoId:
    def test_path_slashes_to_hyphens(self):
        assert _member_repo_id("src/zeta/pkg") == "src-zeta-pkg"

    def test_collapses_runs_and_strips(self):
        assert _member_repo_id("src//Alpha.Pkg/") == "src-alpha-pkg"

    def test_lowercases(self):
        assert _member_repo_id("My/Repo") == "my-repo"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_new_member_entry_structure(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        crt = CrossRepoTarget(
            upstream_remote_url="https://github.com/upstream/zeta.git",
            fork_remote_url="https://github.com/fork/zeta.git",
            base_branch="lyrical",
            auto_fork=True,
        )
        members = [
            _member(
                "src/zeta/pkg",
                "https://github.com/upstream/zeta.git",
                version="lyrical",
                cross_repo_target=crt,
            )
        ]
        result = sync_workspace_members(
            settings, "ros2-workspace", members, file_tickets=False
        )

        assert result.added == ["src-zeta-pkg"]
        entry = _read(repos_file)["repos"]["src-zeta-pkg"]
        assert entry["board_id"] == "src-zeta-pkg"
        assert entry["forge_remote_url"] == "https://github.com/upstream/zeta.git"
        assert entry["working_branch"] == "lyrical"
        assert entry["member_of"] == "ros2-workspace"
        # Langfuse is configured globally; member repos carry NO per-repo
        # langfuse config (neither a block nor a langfuse_from reference).
        assert "langfuse_from" not in entry
        assert "langfuse" not in entry
        # cross_repo_target derived from the manifest policy.
        assert entry["cross_repo_target"]["fork_remote_url"] == (
            "https://github.com/fork/zeta.git"
        )
        assert entry["cross_repo_target"]["base_branch"] == "lyrical"
        _reset_repos_config()

    def test_member_without_version_or_policy_omits_keys(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
        sync_workspace_members(settings, "ros2-workspace", members, file_tickets=False)

        entry = _read(repos_file)["repos"]["src-alpha-pkg"]
        assert "working_branch" not in entry
        assert "cross_repo_target" not in entry
        _reset_repos_config()

    def test_preserves_existing_entries(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        existing = {
            "repos": {
                "robotsix-mill": {"board_id": "robotsix-mill", "langfuse": {}},
            }
        }
        repos_file.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
        sync_workspace_members(settings, "ros2-workspace", members, file_tickets=False)

        repos = _read(repos_file)["repos"]
        assert "robotsix-mill" in repos
        assert "src-alpha-pkg" in repos
        _reset_repos_config()

    def test_upsert_refreshes_member_and_clears_pending_removal(
        self, tmp_path, monkeypatch
    ):
        repos_file = tmp_path / "repos.yaml"
        existing = {
            "repos": {
                "src-alpha-pkg": {
                    "board_id": "src-alpha-pkg",
                    "langfuse": {},
                    "member_of": "ros2-workspace",
                    "forge_remote_url": "https://old.example/alpha.git",
                    "pending_removal": True,
                },
            }
        }
        repos_file.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        members = [
            _member(
                "src/alpha/pkg", "https://github.com/upstream/alpha.git", version="main"
            )
        ]
        result = sync_workspace_members(
            settings, "ros2-workspace", members, file_tickets=False
        )

        assert result.updated == ["src-alpha-pkg"]
        assert result.added == []
        entry = _read(repos_file)["repos"]["src-alpha-pkg"]
        assert entry["forge_remote_url"] == "https://github.com/upstream/alpha.git"
        assert entry["working_branch"] == "main"
        assert "pending_removal" not in entry
        _reset_repos_config()

    def test_non_member_collision_is_skipped(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        existing = {
            "repos": {
                "src-alpha-pkg": {
                    "board_id": "src-alpha-pkg",
                    "langfuse": {},
                    "forge_remote_url": "https://manual.example/alpha.git",
                },
            }
        }
        repos_file.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
        result = sync_workspace_members(
            settings, "ros2-workspace", members, file_tickets=False
        )

        assert result.skipped == ["src-alpha-pkg"]
        assert result.added == []
        entry = _read(repos_file)["repos"]["src-alpha-pkg"]
        # Manual config untouched.
        assert entry["forge_remote_url"] == "https://manual.example/alpha.git"
        assert "member_of" not in entry
        _reset_repos_config()

    def test_missing_master_falls_back_to_empty_langfuse(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)

        members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
        sync_workspace_members(settings, "ros2-workspace", members, file_tickets=False)

        entry = _read(repos_file)["repos"]["src-alpha-pkg"]
        # Langfuse is configured globally — member entries carry NO per-repo
        # langfuse config (neither a block nor a langfuse_from reference).
        assert "langfuse_from" not in entry
        assert "langfuse" not in entry
        _reset_repos_config()


# ---------------------------------------------------------------------------
# Disappearance flagging
# ---------------------------------------------------------------------------


class TestDisappearance:
    def test_vanished_member_flagged_not_deleted(self, tmp_path, monkeypatch):
        repos_file = tmp_path / "repos.yaml"
        existing = {
            "repos": {
                "src-gone-pkg": {
                    "board_id": "src-gone-pkg",
                    "langfuse": {},
                    "member_of": "ros2-workspace",
                    "forge_remote_url": "https://github.com/upstream/gone.git",
                },
                "manual-repo": {
                    "board_id": "manual-repo",
                    "langfuse": {},
                },
            }
        }
        repos_file.write_text(yaml.dump(existing), encoding="utf-8")
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        settings = _make_settings(tmp_path)
        _register_master(monkeypatch, _master_cfg())

        # Manifest now only carries a different member.
        members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
        result = sync_workspace_members(
            settings, "ros2-workspace", members, file_tickets=False
        )

        assert result.flagged_for_removal == ["src-gone-pkg"]
        repos = _read(repos_file)["repos"]
        # Entry + board NOT deleted, just flagged.
        assert repos["src-gone-pkg"]["pending_removal"] is True
        # A non-member entry is never flagged.
        assert "pending_removal" not in repos["manual-repo"]
        _reset_repos_config()


# ---------------------------------------------------------------------------
# Build-out ticket + no-op
# ---------------------------------------------------------------------------


def test_files_buildout_ticket_on_member_board(tmp_path, monkeypatch):
    repos_file = tmp_path / "repos.yaml"
    monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
    settings = _make_settings(tmp_path)
    _register_master(monkeypatch, _master_cfg())

    db.reset_engine()
    db.init_db(settings, board_id="src-zeta-pkg")

    members = [
        _member(
            "src/zeta/pkg", "https://github.com/upstream/zeta.git", version="lyrical"
        )
    ]
    result = sync_workspace_members(settings, "ros2-workspace", members)

    ticket_id = result.filed_tickets["src-zeta-pkg"]
    svc = TicketService(settings, board_id="src-zeta-pkg")
    ticket = svc.get(ticket_id)
    assert ticket is not None
    assert ticket.source == SourceKind.AGENT
    assert "src-zeta-pkg" in ticket.title
    body = svc.workspace(ticket).read_description()
    assert ".robotsix-mill/config.yaml" in body
    assert "lyrical" in body  # references the pinned working branch
    _reset_repos_config()
    db.reset_engine()


def test_mill_repos_file_empty_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("MILL_REPOS_FILE", "")
    settings = _make_settings(tmp_path)
    _register_master(monkeypatch, _master_cfg())

    members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
    result = sync_workspace_members(settings, "ros2-workspace", members)
    assert result.added == []
    assert result.filed_tickets == {}


def test_writes_to_data_dir_overlay(tmp_path, monkeypatch):
    """When MILL_REPOS_FILE is unset and no repos_yaml_path is passed,
    sync_workspace_members writes to <data_dir>/registered_repos.yaml."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    settings = _make_settings(tmp_path)
    _register_master(monkeypatch, _master_cfg())

    members = [_member("src/alpha/pkg", "https://github.com/upstream/alpha.git")]
    result = sync_workspace_members(
        settings, "ros2-workspace", members, file_tickets=False
    )

    assert result.added == ["src-alpha-pkg"]
    overlay = tmp_path / "data" / "registered_repos.yaml"
    assert overlay.exists()
    repos = _read(overlay)["repos"]
    assert "src-alpha-pkg" in repos
    _reset_repos_config()

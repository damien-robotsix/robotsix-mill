"""Unit tests for ``FileOperationsMixin`` in isolation.

These exercise the clone/branch, repo-change, and gitignore-edit
detection methods that live in
``src/robotsix_mill/stages/implement/file_operations.py``.

git_ops, short_circuit_verify, and the forge auth helpers are mocked;
``_clone_and_branch`` tests use real ``TicketService`` (per-test SQLite)
and ``tmp_path`` workspaces, per repo convention.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from robotsix_mill.config import ConfigError, RepoConfig, Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.implement.file_operations import FileOperationsMixin


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path, **kw):
    return Settings(data_dir=str(tmp_path / "data"), **kw)


def _ctx(tmp_path, **kw):
    db.reset_engine()
    s = _settings(tmp_path, **kw)
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")
    return StageContext(
        settings=s,
        service=svc,
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )


def _ticket(ctx, **kw):
    t = ctx.service.create(
        title="Test ticket",
        description="desc",
        source="manual",
        board_id="test-board",
        **kw,
    )
    ctx.service.transition(t.id, State.READY)
    return ctx.service.get(t.id)


# ---------------------------------------------------------------------------
# _any_repo_has_changes
# ---------------------------------------------------------------------------


class TestAnyRepoHasChanges:
    def test_no_changes_single_repo(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: False,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=None, target_branch="main"
            )
            is False
        )

    def test_uncommitted_changes(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: True,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: False,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=None, target_branch="main"
            )
            is True
        )

    def test_ahead_of_main(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: True,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=None, target_branch="main"
            )
            is True
        )

    def test_multi_repo_both_clean(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        extra = tmp_path / "extra"
        repo.mkdir()
        extra.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: False,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=[extra], target_branch="main"
            )
            is False
        )

    def test_multi_repo_extra_has_changes(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        extra = tmp_path / "extra"
        repo.mkdir()
        extra.mkdir()
        calls = []

        def fake_has_changes(rd):
            calls.append(("has_changes", str(rd)))
            return rd == extra

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            fake_has_changes,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: False,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=[extra], target_branch="main"
            )
            is True
        )
        # Primary was checked first (and returned False), then extra.
        assert ("has_changes", str(repo)) in calls

    def test_multi_repo_skips_same_dir(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = []

        def fake_has_changes(rd):
            calls.append(rd)
            return False

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            fake_has_changes,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: False,
        )
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=[repo], target_branch="main"
            )
            is False
        )
        # repo_dir and extra_roots[0] are identical → skipped; only
        # the primary check ran.
        assert len(calls) == 1

    def test_per_repo_target_branch_with_settings(self, monkeypatch, tmp_path):
        """When settings is provided, each extra repo resolves its own
        target branch via get_repo_config + target_branch_for."""
        repo = tmp_path / "repo"
        extra = tmp_path / "extra"
        repo.mkdir()
        extra.mkdir()

        branch_calls = []

        def fake_branch_ahead(rd, target):
            branch_calls.append((str(rd), target))
            return target == "develop"  # only extra returns True

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            fake_branch_ahead,
        )

        def fake_get_repo_config(repo_id):
            if repo_id == "extra":
                return SimpleNamespace(working_branch="develop")
            raise ConfigError(f"unknown: {repo_id}")

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.get_repo_config",
            fake_get_repo_config,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.target_branch_for",
            lambda settings, rc: rc.working_branch if rc else "main",
        )

        s = _settings(tmp_path)
        assert (
            FileOperationsMixin._any_repo_has_changes(
                repo, extra_roots=[extra], target_branch="main", settings=s
            )
            is True
        )
        # Primary uses default target_branch "main", extra resolves "develop".
        assert branch_calls == [(str(repo), "main"), (str(extra), "develop")]

    def test_per_repo_config_error_falls_back(self, monkeypatch, tmp_path):
        """get_repo_config raises ConfigError → fall back to default target_branch."""
        repo = tmp_path / "repo"
        extra = tmp_path / "extra"
        repo.mkdir()
        extra.mkdir()

        branch_calls = []

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.has_changes",
            lambda rd: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_is_ahead_of_main",
            lambda rd, target: branch_calls.append((str(rd), target)),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.get_repo_config",
            lambda repo_id: (_ for _ in ()).throw(ConfigError("no such repo")),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.target_branch_for",
            lambda settings, rc: "main",
        )

        s = _settings(tmp_path)
        FileOperationsMixin._any_repo_has_changes(
            repo, extra_roots=[extra], target_branch="main", settings=s
        )
        # Both repos use the default "main".
        assert branch_calls == [(str(repo), "main"), (str(extra), "main")]


# ---------------------------------------------------------------------------
# _claimed_gitignored_edits
# ---------------------------------------------------------------------------


class TestClaimedGitignoredEdits:
    def test_relative_paths(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.short_circuit_verify"
            ".run_claimed_edited_rawpaths",
            lambda msgs: ["src/foo.py", "tests/bar.py"],
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops"
            ".ignored_existing_paths",
            lambda rd, rels: [r for r in rels if r == "src/foo.py"],
        )
        result = FileOperationsMixin._claimed_gitignored_edits(repo, b"messages")
        assert result == ["src/foo.py"]

    def test_absolute_paths_normalized(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.short_circuit_verify"
            ".run_claimed_edited_rawpaths",
            lambda msgs: [str(repo / "src/foo.py")],
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops"
            ".ignored_existing_paths",
            lambda rd, rels: rels,
        )
        result = FileOperationsMixin._claimed_gitignored_edits(repo, b"messages")
        assert result == ["src/foo.py"]

    def test_absolute_path_outside_clone_skipped(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.short_circuit_verify"
            ".run_claimed_edited_rawpaths",
            lambda msgs: [str(tmp_path / "other" / "file.py")],
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops"
            ".ignored_existing_paths",
            lambda rd, rels: rels,
        )
        result = FileOperationsMixin._claimed_gitignored_edits(repo, b"messages")
        assert result == []

    def test_deduplication_relative_and_absolute(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.short_circuit_verify"
            ".run_claimed_edited_rawpaths",
            lambda msgs: ["src/foo.py", str(repo / "src/foo.py")],
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops"
            ".ignored_existing_paths",
            lambda rd, rels: rels,
        )
        result = FileOperationsMixin._claimed_gitignored_edits(repo, b"messages")
        assert result == ["src/foo.py"]

    def test_fail_open_returns_empty(self, monkeypatch, tmp_path, caplog):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.short_circuit_verify"
            ".run_claimed_edited_rawpaths",
            lambda msgs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        import logging

        caplog.set_level(logging.WARNING)
        result = FileOperationsMixin._claimed_gitignored_edits(repo, b"messages")
        assert result == []
        assert "gitignored-edit detection failed" in caplog.text


# ---------------------------------------------------------------------------
# _clone_and_branch
# ---------------------------------------------------------------------------


class TestCloneAndBranch:
    def _mock_git_ops_clone_chain(self, monkeypatch, calls_tracker=None):
        """Mock the full git_ops chain for _clone_and_branch success paths.
        Each mock appends its name to *calls_tracker* if provided."""

        def _track(name):
            if calls_tracker is not None:
                calls_tracker.append(name)

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            lambda remote_url, repo_dir, target, token: _track("clone"),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.create_branch",
            lambda repo_dir, branch: _track("create_branch"),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.checkout",
            lambda repo_dir, branch: _track("checkout"),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.try_rebase_onto",
            lambda repo_dir, target, *, remote_url=None, token=None: (
                _track("try_rebase_onto") or True
            ),
        )

    def _mock_forge_auth(self, monkeypatch):
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations._resolve_remote_url",
            lambda settings, rc: "https://example.com/repo.git",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.github_token",
            lambda settings, repo_config=None: "fake-token",
        )

    def test_fresh_clone_success(self, monkeypatch, tmp_path):
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        self._mock_forge_auth(monkeypatch)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, tuple)
        repo_dir, branch, resuming = result
        assert repo_dir.name == "repo"
        assert branch.startswith("mill/")
        assert branch.endswith(ticket.id)
        assert resuming is False
        assert "clone" in calls
        assert "create_branch" in calls
        assert "try_rebase_onto" in calls
        assert "checkout" not in calls

    def test_resume_path(self, monkeypatch, tmp_path):
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        self._mock_forge_auth(monkeypatch)
        # Override branch_exists to True so we take the resume path.
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: True,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        repo_dir_out, branch, resuming = result
        assert resuming is True
        assert "checkout" in calls
        # Fresh clone steps should be skipped.
        assert "clone" not in calls
        assert "create_branch" not in calls
        # Rebase still runs.
        assert "try_rebase_onto" in calls

    def test_clone_failure_returns_blocked(self, monkeypatch, tmp_path):
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        self._mock_forge_auth(monkeypatch)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        def fake_clone(*a, **kw):
            raise subprocess.CalledProcessError(1, "git clone", stderr=b"auth failed")

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            fake_clone,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, Outcome)
        assert result.next_state == State.BLOCKED
        assert "clone failed" in (result.note or "")

    def test_rebase_failure_returns_rebasing(self, monkeypatch, tmp_path):
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_forge_auth(monkeypatch)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            lambda remote_url, repo_dir, target, token: calls.append("clone"),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.create_branch",
            lambda repo_dir, branch: calls.append("create_branch"),
        )
        # Rebase returns False.
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.try_rebase_onto",
            lambda repo_dir, target, *, remote_url=None, token=None: False,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, Outcome)
        assert result.next_state == State.REBASING
        assert "rebase" in (result.note or "").lower()

    def test_hard_invariant_clone_missing_reclones(self, monkeypatch, tmp_path):
        """When .git is missing before agent run, it re-clones."""
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_forge_auth(monkeypatch)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: False,
        )

        clone_count = [0]

        def fake_clone(*a, **kw):
            clone_count[0] += 1
            # Only create .git on the second attempt (simulates the
            # invariant fallback reclone).
            if clone_count[0] == 2:
                calls.append("clone2")

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            fake_clone,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.create_branch",
            lambda repo_dir, branch: calls.append("create_branch"),
        )
        # try_rebase_onto returns True but does NOT create .git.
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.try_rebase_onto",
            lambda repo_dir, target, *, remote_url=None, token=None: True,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        # The invariant reclone succeeded → we get a tuple.
        assert isinstance(result, tuple)
        assert "clone2" in calls
        assert "create_branch" in calls

    def test_hard_invariant_reclone_fails_returns_blocked(self, monkeypatch, tmp_path):
        """When .git is missing AND the reclone also fails → BLOCKED."""
        ctx = _ctx(tmp_path)
        ticket = _ticket(ctx)
        self._mock_forge_auth(monkeypatch)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.create_branch",
            lambda repo_dir, branch: None,
        )

        # First clone succeeds (doesn't raise), but .git remains missing
        # so the hard-invariant fallback fires.  The second clone (in the
        # invariant block) raises → BLOCKED with the invariant message.
        clone_count = [0]

        def fake_clone(*a, **kw):
            clone_count[0] += 1
            if clone_count[0] >= 2:
                raise subprocess.CalledProcessError(
                    1, "git clone", stderr=b"fatal on reclone"
                )

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            fake_clone,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.try_rebase_onto",
            lambda repo_dir, target, *, remote_url=None, token=None: True,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, Outcome)
        assert result.next_state == State.BLOCKED
        assert "clone missing" in (result.note or "")


# ---------------------------------------------------------------------------
# package_dag guard: the mixin must be importable from file_operations
# ---------------------------------------------------------------------------


def test_file_operations_mixin_is_importable():
    """Structural guard — the test file itself imports the mixin, but
    confirm the class is concrete enough to test."""
    assert hasattr(FileOperationsMixin, "_clone_and_branch")
    assert hasattr(FileOperationsMixin, "_any_repo_has_changes")
    assert hasattr(FileOperationsMixin, "_claimed_gitignored_edits")

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

from robotsix_mill.config import ConfigError, CrossRepoTarget, RepoConfig, Settings
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

    # -- cross_repo_target ------------------------------------------------

    def _ctx_cross_repo(self, tmp_path):
        """Return a StageContext with cross_repo_target set on the RepoConfig."""
        db.reset_engine()
        s = _settings(tmp_path)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        cross = CrossRepoTarget(
            upstream_remote_url="https://github.com/upstream/repo.git",
            fork_remote_url="https://github.com/fork/repo.git",
            base_branch="develop",
        )
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
                cross_repo_target=cross,
            ),
        )

    def test_cross_repo_clones_fork_not_managed(self, monkeypatch, tmp_path):
        """When cross_repo_target is set, _clone_and_branch clones the fork."""
        ctx = self._ctx_cross_repo(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        # Capture the remote_url passed to git_ops.clone.
        captured_remote = []

        def _capture_clone(remote_url, repo_dir, target, token):
            captured_remote.append(remote_url)
            # Create .git so the hard-invariant guard does not re-clone.
            (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
            calls.append("clone")

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            _capture_clone,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.github_token",
            lambda settings, repo_config=None: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, tuple)
        repo_dir, branch, resuming = result
        assert resuming is False
        assert len(captured_remote) == 1
        assert captured_remote[0] == "https://github.com/fork/repo.git"

    def test_cross_repo_uses_base_branch_as_target(self, monkeypatch, tmp_path):
        """cross_repo_target.base_branch is used as the clone target branch."""
        ctx = self._ctx_cross_repo(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        captured_target = []

        def _capture_clone(remote_url, repo_dir, target, token):
            captured_target.append(target)
            (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            _capture_clone,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.github_token",
            lambda settings, repo_config=None: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, tuple)
        assert len(captured_target) == 1
        assert captured_target[0] == "develop"

    def test_cross_repo_resume_path(self, monkeypatch, tmp_path):
        """Resume works when cross_repo_target is set."""
        ctx = self._ctx_cross_repo(tmp_path)
        ticket = _ticket(ctx)
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.github_token",
            lambda settings, repo_config=None: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.branch_exists",
            lambda rd, branch: True,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        repo_dir_out, branch, resuming = result
        assert resuming is True
        assert "clone" not in calls
        assert "checkout" in calls
        assert "try_rebase_onto" in calls

    def test_cross_repo_rebase_uses_fork_remote(self, monkeypatch, tmp_path):
        """The rebase also targets the fork remote, not the managed repo."""
        ctx = self._ctx_cross_repo(tmp_path)
        ticket = _ticket(ctx)
        calls = []
        self._mock_git_ops_clone_chain(monkeypatch, calls)
        # Override clone to create .git so the hard-invariant doesn't fire.
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.clone",
            lambda remote_url, repo_dir, target, token: (repo_dir / ".git").mkdir(
                parents=True, exist_ok=True
            ),
        )
        captured_rebase_remote = []

        def _capture_try_rebase(repo_dir, target, *, remote_url=None, token=None):
            captured_rebase_remote.append(remote_url)
            return True

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.git_ops.try_rebase_onto",
            _capture_try_rebase,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.github_token",
            lambda settings, repo_config=None: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.file_operations.shutil.rmtree",
            lambda path, ignore_errors=False: None,
        )

        result = FileOperationsMixin._clone_and_branch(ctx, ticket, ctx.settings)
        assert isinstance(result, tuple)
        assert len(captured_rebase_remote) == 1
        assert captured_rebase_remote[0] == "https://github.com/fork/repo.git"


# ---------------------------------------------------------------------------
# package_dag guard: the mixin must be importable from file_operations
# ---------------------------------------------------------------------------


def test_file_operations_mixin_is_importable():
    """Structural guard — the test file itself imports the mixin, but
    confirm the class is concrete enough to test."""
    assert hasattr(FileOperationsMixin, "_clone_and_branch")
    assert hasattr(FileOperationsMixin, "_any_repo_has_changes")
    assert hasattr(FileOperationsMixin, "_claimed_gitignored_edits")


# ---------------------------------------------------------------------------
# _edits_formatter_reverted — replay + format discriminator (real git + ruff)
# ---------------------------------------------------------------------------


def _init_git_repo(tmp_path, files: dict):
    from pathlib import Path

    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    import os

    runenv = {**os.environ, **env}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=runenv)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=runenv)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=runenv)
    return repo


def _edit_msgs(specs: list) -> bytes:
    import json

    parts = [
        {
            "part_kind": "tool-call",
            "tool_name": name,
            "args": args,
            "tool_call_id": f"c{i}",
        }
        for i, (name, args) in enumerate(specs)
    ]
    return json.dumps([{"parts": parts}]).encode()


# A repo whose ruff target is 3.14, so ruff format strips redundant
# multi-exception parentheses (the PEP-758 normalisation behind ticket c356).
_PY314_PYPROJECT = '[tool.ruff]\ntarget-version = "py314"\n'
_CLEAN_PEP758 = (
    "import json\n\n\n"
    "def f(p):\n"
    "    try:\n"
    "        return json.loads(p)\n"
    "    except json.JSONDecodeError, KeyError:\n"
    "        return None\n"
)


def _porcelain(repo) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class TestEditsFormatterReverted:
    def test_formatter_reverted_edit_is_noop_true(self, tmp_path):
        """The c356 case: parenthesising ``except A, B:`` is reverted by
        ruff format on a 3.14 target → no net change → True (safe no-op)."""
        repo = _init_git_repo(
            tmp_path, {"pyproject.toml": _PY314_PYPROJECT, "m.py": _CLEAN_PEP758}
        )
        msgs = _edit_msgs(
            [
                (
                    "edit_file",
                    {
                        "path": "m.py",
                        "old_string": "    except json.JSONDecodeError, KeyError:",
                        "new_string": "    except (json.JSONDecodeError, KeyError):",
                    },
                )
            ]
        )
        assert FileOperationsMixin._edits_formatter_reverted(repo, msgs) is True
        # Tree restored to pristine afterward.
        assert _porcelain(repo) == ""

    def test_surviving_change_is_lost_work_false(self, tmp_path):
        """A real semantic edit that ruff keeps → diff survives → False
        (work-loss case: must BLOCK)."""
        repo = _init_git_repo(
            tmp_path, {"pyproject.toml": _PY314_PYPROJECT, "m.py": _CLEAN_PEP758}
        )
        msgs = _edit_msgs(
            [
                (
                    "edit_file",
                    {
                        "path": "m.py",
                        "old_string": "        return None\n",
                        "new_string": "        return {}\n",
                    },
                )
            ]
        )
        assert FileOperationsMixin._edits_formatter_reverted(repo, msgs) is False
        assert _porcelain(repo) == ""

    def test_unreplayable_kind_fails_closed_none(self, tmp_path):
        repo = _init_git_repo(
            tmp_path, {"pyproject.toml": _PY314_PYPROJECT, "m.py": _CLEAN_PEP758}
        )
        msgs = _edit_msgs([("MultiEdit", {"file_path": "m.py", "edits": []})])
        assert FileOperationsMixin._edits_formatter_reverted(repo, msgs) is None
        assert _porcelain(repo) == ""

    def test_path_outside_clone_fails_closed_none(self, tmp_path):
        repo = _init_git_repo(
            tmp_path, {"pyproject.toml": _PY314_PYPROJECT, "m.py": _CLEAN_PEP758}
        )
        msgs = _edit_msgs(
            [
                (
                    "edit_file",
                    {"path": "../escape.py", "old_string": "a", "new_string": "b"},
                )
            ]
        )
        assert FileOperationsMixin._edits_formatter_reverted(repo, msgs) is None

    def test_no_replayable_edits_fails_closed_none(self, tmp_path):
        repo = _init_git_repo(
            tmp_path, {"pyproject.toml": _PY314_PYPROJECT, "m.py": _CLEAN_PEP758}
        )
        # only a read tool → extract returns [] → None (caller blocks)
        msgs = _edit_msgs([("read_file", {"path": "m.py"})])
        assert FileOperationsMixin._edits_formatter_reverted(repo, msgs) is None

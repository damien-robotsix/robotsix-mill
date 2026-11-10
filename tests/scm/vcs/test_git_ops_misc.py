import os
import subprocess
import time
from pathlib import Path

import pytest

import robotsix_mill.config as _cfg
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
    target_branch_for,
)
from robotsix_mill.scm.vcs import clone_all_repos, git_ops

from ._git_test_helpers import _git, make_bare_repo

# ===========================================================================
# redact_credentials + clone error sanitization
# ===========================================================================


class TestRedactCredentials:
    def test_strips_userinfo_from_embedded_url(self):
        text = (
            "CalledProcessError(128, ['git', 'clone', "
            "'https://oauth2:ghs_supersecret@github.com/x/y.git', '/tmp/d'])"
        )
        out = git_ops.redact_credentials(text)
        assert "ghs_supersecret" not in out
        assert "://***@github.com" in out

    def test_plain_url_unchanged(self):
        text = "git clone https://github.com/x/y.git failed"
        assert git_ops.redact_credentials(text) == text

    def test_non_url_text_unchanged(self):
        assert git_ops.redact_credentials("no urls here") == "no urls here"


class TestCloneErrorRedaction:
    def test_clone_failure_never_leaks_token(self, tmp_path):
        """A failed authed clone re-raises CalledProcessError with the
        token scrubbed from cmd/output/stderr — its repr lands in ticket
        notes and Langfuse traces (live leak: clone_repo)."""
        # Nothing listens on port 9 → fails fast, fully offline.
        with pytest.raises(subprocess.CalledProcessError) as ei:
            git_ops.clone(
                "https://127.0.0.1:9/none.git",
                tmp_path / "dest",
                "main",
                token="sekret-token-123",
            )
        exposed = repr(ei.value) + str(ei.value.cmd) + str(ei.value.stderr)
        assert "sekret-token-123" not in exposed
        assert "://***@" in str(ei.value.cmd)

    def test_clone_success_unaffected(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        assert (dest / "README.md").exists()


# ===========================================================================
# ignored_existing_paths — the "edits landed but git can't see them" detector
# ===========================================================================


class TestIgnoredExistingPaths:
    def _repo_with_ignored_subtree(self, tmp_path):
        """Mimics a manifest board: ``/src/*`` gitignored for vcs-imported
        sub-repos (the robotsix-mill-ros2 layout)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        (dest / ".gitignore").write_text("/src/*\n!/src/.gitkeep\n")
        git_ops.commit_all(dest, "add gitignore")
        return dest

    def test_detects_existing_ignored_write(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        target = dest / "src" / "pkg" / "msg"
        target.mkdir(parents=True)
        (target / "Status.msg").write_text("int32 code\n")
        hits = git_ops.ignored_existing_paths(dest, ["src/pkg/msg/Status.msg"])
        assert hits == ["src/pkg/msg/Status.msg"]

    def test_tracked_path_not_flagged(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        (dest / "tracked.txt").write_text("x\n")
        assert git_ops.ignored_existing_paths(dest, ["tracked.txt"]) == []

    def test_missing_file_not_flagged(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        assert git_ops.ignored_existing_paths(dest, ["src/pkg/gone.txt"]) == []

    def test_empty_input(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        assert git_ops.ignored_existing_paths(dest, []) == []


# ===========================================================================
# ignored_paths — existence-agnostic gitignore check (refine guard)
# ===========================================================================


class TestIgnoredPaths:
    def _repo_with_ignored_subtree(self, tmp_path):
        """Mimics a manifest board: ``/src/*`` gitignored for vcs-imported
        sub-repos (the robotsix-mill-ros2 layout)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        (dest / ".gitignore").write_text("/src/*\n!/src/.gitkeep\n")
        git_ops.commit_all(dest, "add gitignore")
        return dest

    def test_flags_nonexistent_ignored_path(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        # Status.msg was never written, yet a /src/* rule still ignores it.
        hits = git_ops.ignored_paths(dest, ["src/pkg/msg/Status.msg"])
        assert hits == ["src/pkg/msg/Status.msg"]

    def test_tracked_area_path_not_flagged(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        assert git_ops.ignored_paths(dest, ["robotsix_mill/foo.py"]) == []

    def test_empty_input(self, tmp_path):
        dest = self._repo_with_ignored_subtree(tmp_path)
        assert git_ops.ignored_paths(dest, []) == []


# ===========================================================================
# Regression test: repo with non-main default branch + working_branch config
# ===========================================================================


def _make_custom_branch_repo(tmp_path: Path, branch_name: str) -> str:
    """Create a bare repo with a non-main default branch.
    Branch name becomes the initial branch instead of 'main'."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", branch_name)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


class TestWorkingBranchRegression:
    def test_clone_baseline_deliver_with_custom_working_branch(self, tmp_path):
        """Regression test for per-repo working_branch: when a repo config
        specifies a non-main working_branch (e.g. 'lyrical'), clone/baseline/deliver
        operations target that branch instead of 'main' (which doesn't exist)."""
        # Create a repo with 'lyrical' as the default branch
        remote = _make_custom_branch_repo(tmp_path, "lyrical")

        # Test clone with the custom branch
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "lyrical")
        assert (dest / ".git").is_dir()
        assert (dest / "README.md").exists()

        # Test that branch comparison functions work with custom branch
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")

        # These should all work with target_branch="lyrical"
        assert git_ops.branch_is_ahead_of_main(dest, target_branch="lyrical") is True
        assert git_ops.branch_has_net_diff(dest, target_branch="lyrical") is True

        # Add another commit to the base branch and verify detection
        _git(dest, "checkout", "lyrical")
        (dest / "lyrical_added.txt").write_text("base branch commit")
        git_ops.commit_all(dest, "base advance")
        _git(dest, "push", "origin", "lyrical")
        _git(dest, "checkout", "feature")

        # After main (lyrical) advances, feature should be behind
        assert git_ops.branch_is_behind_main(dest, target_branch="lyrical") is True

    def test_changed_files_with_custom_branch(self, tmp_path):
        """Test that changed_files works correctly with a custom target_branch."""
        # Create a repo with 'rolling' as the default branch
        remote = _make_custom_branch_repo(tmp_path, "rolling")

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "rolling")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("new file")

        # changed_files with custom branch should detect the new file
        files = git_ops.changed_files(dest, "rolling")
        assert "new.txt" in files

    def test_changed_files_tolerates_missing_origin_ref(self, tmp_path, caplog):
        """changed_files does not raise when origin/<target> is unresolvable."""
        remote = _make_custom_branch_repo(tmp_path, "lyrical")
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "lyrical")
        (dest / "untracked.txt").write_text("untracked")

        # origin/main does not exist (clone was --single-branch lyrical).
        files = git_ops.changed_files(dest, "main")
        assert "untracked.txt" in files  # untracked files still collected

    def test_introduced_files_tolerates_missing_origin_ref(self, tmp_path, caplog):
        """introduced_files does not raise when origin/<target> is unresolvable."""
        remote = _make_custom_branch_repo(tmp_path, "lyrical")
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "lyrical")
        (dest / "untracked.txt").write_text("untracked")

        # origin/main does not exist (clone was --single-branch lyrical).
        files = git_ops.introduced_files(dest, "main")
        assert "untracked.txt" in files  # untracked files still collected

    def test_diff_base_with_custom_branch(self, tmp_path):
        """Test that diff_base works with a custom target_branch."""
        # Create a repo with 'develop' as the default branch
        remote = _make_custom_branch_repo(tmp_path, "develop")

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "develop")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature diff")
        git_ops.commit_all(dest, "feature commit")

        # diff_base with custom branch should show the diff
        diff = git_ops.diff_base(dest, "develop")
        assert "diff --git" in diff
        assert "feat.txt" in diff

    # -- Config-driven resolution: RepoConfig.working_branch must drive the
    # -- branch that clone/baseline/deliver target (instead of falling back
    # -- to 'main', which does not exist on these forks).

    @staticmethod
    def _repo_config(repo_id, *, working_branch, forge_remote_url=None):
        return RepoConfig(
            repo_id=repo_id,
            board_id="meta",
            langfuse_project_name=f"p-{repo_id}",
            langfuse_public_key=f"pk-{repo_id}",
            langfuse_secret_key=f"sk-{repo_id}",
            working_branch=working_branch,
            forge_remote_url=forge_remote_url,
        )

    def test_refine_clone_path_targets_working_branch(self, tmp_path):
        """End-to-end refine/meta clone path: a repo whose only branch is
        'lyrical' (no 'main') is cloned successfully precisely because its
        RepoConfig.working_branch is set. ``clone_all_repos`` runs the same
        ``git_ops.clone(url, dest, target_branch_for(s, rc), token)`` chain
        the refine clone (stages/refine/core.py) uses."""
        remote = _make_custom_branch_repo(tmp_path, "lyrical")
        settings = Settings(data_dir=str(tmp_path / "data"))

        # With working_branch set, resolution targets 'lyrical' and clone wins.
        rc = self._repo_config(
            "fork", working_branch="lyrical", forge_remote_url=remote
        )
        assert target_branch_for(settings, rc) == "lyrical"
        _reset_repos_config()
        _cfg._repos_config = ReposRegistry(repos={"fork": rc})
        try:
            result = clone_all_repos(settings)
        finally:
            _reset_repos_config()
        assert "fork" in result
        assert (result["fork"] / ".git").is_dir()

        # Negative control: without working_branch, resolution falls back to
        # 'main' (absent on the fork), so the clone fails — the motivating bug.
        rc_no_wb = self._repo_config(
            "fork", working_branch=None, forge_remote_url=remote
        )
        assert target_branch_for(settings, rc_no_wb) == "main"
        _reset_repos_config()
        _cfg._repos_config = ReposRegistry(repos={"fork": rc_no_wb})
        try:
            result_no_wb = clone_all_repos(settings)
        finally:
            _reset_repos_config()
        assert "fork" not in result_no_wb

    def test_implement_baseline_resolution_targets_working_branch(self, tmp_path):
        """Implement-baseline scope guardrail resolves the diff target via
        ``target_branch_for`` then calls ``git_ops.introduced_files`` against
        it (stages/implement.py). With working_branch='lyrical' that resolves
        to 'lyrical', and introduced files are detected against that base."""
        remote = _make_custom_branch_repo(tmp_path, "lyrical")
        settings = Settings(data_dir=str(tmp_path / "data"))
        rc = self._repo_config("fork", working_branch="lyrical")

        target = target_branch_for(settings, rc)
        assert target == "lyrical"

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, target)
        git_ops.create_branch(dest, "feature")
        (dest / "introduced.txt").write_text("baseline work")

        assert "introduced.txt" in git_ops.introduced_files(dest, target)

    def test_deliver_resolution_targets_working_branch(self, tmp_path):
        """Deliver resolves the PR target branch via ``target_branch_for``
        then guards on ahead-of-target (stages/deliver.py). With
        working_branch='lyrical' the branch is measured ahead of 'lyrical',
        not 'main'."""
        remote = _make_custom_branch_repo(tmp_path, "lyrical")
        settings = Settings(data_dir=str(tmp_path / "data"))
        rc = self._repo_config("fork", working_branch="lyrical")

        target = target_branch_for(settings, rc)
        assert target == "lyrical"

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, target)
        git_ops.create_branch(dest, "feature")
        (dest / "delivered.txt").write_text("deliver work")
        git_ops.commit_all(dest, "feature commit")

        assert git_ops.branch_is_ahead_of_main(dest, target_branch=target) is True


def test_git_never_prompts_for_credentials(tmp_path, monkeypatch):
    """Git must run with GIT_TERMINAL_PROMPT=0.

    A git process that decides it needs credentials can block on
    /dev/tty indefinitely, turning a bad or expired token into a hang
    instead of a clean failure.
    """
    seen: list[dict] = []

    def _fake_run(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", _fake_run)
    git_ops.fetch(tmp_path, remote_url="https://x/y.git", token="t", branch="main")

    assert seen
    assert seen[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_timeout_error_redacts_the_token(tmp_path, monkeypatch):
    """A TimeoutExpired must not leak the tokenized remote URL.

    Its ``cmd`` carries the same https://oauth2:<token>@... URL that
    CalledProcessError does, and these errors land in ticket notes and
    Langfuse traces.
    """

    def _fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(git_ops.subprocess, "run", _fake_run)

    with pytest.raises(subprocess.TimeoutExpired) as ei:
        git_ops.fetch(
            tmp_path, remote_url="https://x/y.git", token="sekrit", branch="main"
        )

    assert "sekrit" not in str(ei.value.cmd)
    assert "***" in str(ei.value.cmd)


# ===========================================================================
# clear_stale_index_lock
# ===========================================================================


def _make_lock(tmp_path: Path, *, age_seconds: float) -> Path:
    """Create ``<repo>/.git/index.lock`` and back-date its mtime."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    past = os.stat(lock).st_atime, time.time() - age_seconds
    os.utime(lock, past)
    return lock


def test_clear_stale_index_lock_removes_old_lock(tmp_path):
    """A lock older than the staleness threshold is deleted and reported."""
    lock = _make_lock(tmp_path, age_seconds=git_ops.STALE_INDEX_LOCK_SECONDS + 60)
    assert git_ops.clear_stale_index_lock(lock.parent.parent) is True
    assert not lock.exists()


def test_clear_stale_index_lock_keeps_fresh_lock(tmp_path):
    """A recently-created lock (a live git process) is left untouched."""
    lock = _make_lock(tmp_path, age_seconds=10)
    assert git_ops.clear_stale_index_lock(lock.parent.parent) is False
    assert lock.exists()


def test_clear_stale_index_lock_no_lock(tmp_path):
    """A missing lock is a silent no-op."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert git_ops.clear_stale_index_lock(repo) is False

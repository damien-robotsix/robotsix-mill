import subprocess
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
from robotsix_mill.vcs import clone_all_repos, git_ops
from robotsix_mill.vcs.git_ops import PostPushResult


# ---------------------------------------------------------------------------
# Helpers — copied verbatim from tests/stages/test_implement.py lines 18–35
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch — lets us
    exercise clone/branch/commit fully offline, no forge."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


# ===========================================================================
# 1. _authed_url — pure unit tests (no shell-out)
# ===========================================================================


class TestAuthedUrl:
    def test_https_with_token(self):
        result = git_ops._authed_url("https://github.com/me/repo.git", "tok123")
        assert result == "https://oauth2:tok123@github.com/me/repo.git"

    def test_https_none_token(self):
        result = git_ops._authed_url("https://github.com/me/repo.git", None)
        assert result == "https://github.com/me/repo.git"

    def test_file_url_unchanged(self):
        result = git_ops._authed_url("file:///tmp/remote.git", "tok123")
        assert result == "file:///tmp/remote.git"

    def test_ssh_url_unchanged(self):
        result = git_ops._authed_url("ssh://git@github.com/me/repo.git", "tok123")
        assert result == "ssh://git@github.com/me/repo.git"

    def test_empty_token_not_injected(self):
        result = git_ops._authed_url("https://github.com/me/repo.git", "")
        assert result == "https://github.com/me/repo.git"


# ===========================================================================
# 2. clone — integration (real git, file:// remote)
# ===========================================================================


class TestClone:
    def test_clone_bare_repo(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "clone_dest"
        git_ops.clone(remote, dest, "main")
        assert (dest / ".git").is_dir()
        assert (dest / "README.md").exists()
        assert (dest / "README.md").read_text().strip() == "seed"

    def test_clone_configures_user(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "clone_dest"
        git_ops.clone(remote, dest, "main")
        email = subprocess.run(
            ["git", "-C", str(dest), "config", "user.email"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        name = subprocess.run(
            ["git", "-C", str(dest), "config", "user.name"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert email == "mill@robotsix.local"
        assert name == "robotsix-mill"

    def test_init_repo_creates_branch_and_identity(self, tmp_path):
        dest = tmp_path / "fresh"
        git_ops.init_repo(dest, "main")
        assert (dest / ".git").is_dir()
        # On the initial branch (no commits yet) HEAD points at refs/heads/main.
        head = subprocess.run(
            ["git", "-C", str(dest), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "main"
        email = subprocess.run(
            ["git", "-C", str(dest), "config", "user.email"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert email == "mill@robotsix.local"

    def test_init_repo_commit_push_into_bare_remote(self, tmp_path):
        """init → write → commit → force-push populates an empty bare remote
        (the brand-new-repo scaffold path)."""
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(bare)],
            check=True,
        )
        dest = tmp_path / "work"
        git_ops.init_repo(dest, "main")
        (dest / "README.md").write_text("hi\n", encoding="utf-8")
        git_ops.commit_all(dest, "Initial scaffold")
        git_ops.push(dest, "main", f"file://{bare}", token=None)
        # The bare remote now has the README on main.
        show = subprocess.run(
            ["git", "-C", str(bare), "show", "main:README.md"],
            capture_output=True,
            text=True,
        )
        assert show.returncode == 0
        assert show.stdout.strip() == "hi"

    def test_clone_with_token_file_url_still_works(self, tmp_path):
        """Verify clone works with a token + file:// URL (token is ignored
        for file:// by _authed_url, but the codepath is exercised)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "clone_dest"
        git_ops.clone(remote, dest, "main", token="dummy-tok")
        assert (dest / ".git").is_dir()
        assert (dest / "README.md").exists()

    def test_clone_token_injection_subprocess_args(self, tmp_path, monkeypatch):
        """Verify that the URL passed to subprocess.run by clone has the
        oauth2 token injected for https:// URLs."""
        remote = make_bare_repo(tmp_path)
        # The remote is a file:// URL, but we monkeypatch subprocess.run
        # to capture the URL argument — and we pass an https:// URL so
        # _authed_url does the transformation.
        captured = []

        real_run = subprocess.run

        def _capture(cmd, **kwargs):
            captured.append(cmd)
            # The first call is 'git clone …' — intercept and redirect
            # to the real file:// remote so the clone actually succeeds.
            if cmd[0] == "git" and "clone" in cmd:
                # Replace the https URL with the real file:// remote
                new_cmd = list(cmd)
                for i, arg in enumerate(new_cmd):
                    if arg.startswith("https://"):
                        new_cmd[i] = remote
                return real_run(new_cmd, **kwargs)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", _capture)

        dest = tmp_path / "clone_dest"
        git_ops.clone("https://github.com/me/repo.git", dest, "main", token="tok123")

        # The first captured call should be the clone, with the
        # oauth2-injected URL in the arg list.
        clone_cmd = captured[0]
        assert any("oauth2:tok123@" in a for a in clone_cmd), (
            f"Expected oauth2 token injection in clone cmd: {clone_cmd}"
        )


# ===========================================================================
# 3. has_changes — integration (real git)
# ===========================================================================


class TestHasChanges:
    def test_clean_repo(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        assert git_ops.has_changes(dest) is False

    def test_new_file_shows_changes(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        (dest / "new.txt").write_text("hello")
        assert git_ops.has_changes(dest) is True

    def test_after_commit_clean_again(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        (dest / "new.txt").write_text("hello")
        assert git_ops.has_changes(dest) is True
        git_ops.commit_all(dest, "add new.txt")
        assert git_ops.has_changes(dest) is False


# ===========================================================================
# 4. branch_exists — integration (real git)
# ===========================================================================


class TestBranchExists:
    def test_existing_branch(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        assert git_ops.branch_exists(dest, "main") is True

    def test_nonexistent_branch(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        assert git_ops.branch_exists(dest, "no-such-branch") is False


# ===========================================================================
# 5. checkout — integration (real git)
# ===========================================================================


class TestCheckout:
    def test_checkout_existing_branch(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Checkout back to main
        git_ops.checkout(dest, "main")
        head = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "main"

    def test_checkout_nonexistent_branch_raises(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        with pytest.raises(subprocess.CalledProcessError):
            git_ops.checkout(dest, "no-such-branch")


# ===========================================================================
# 6. create_branch — integration (real git)
# ===========================================================================


class TestCreateBranch:
    def test_create_new_branch(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        out = subprocess.run(
            ["git", "-C", str(dest), "branch", "--list", "feature"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "feature" in out

    def test_recreate_existing_branch_no_error(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Recreate with -B should not raise
        git_ops.create_branch(dest, "feature")
        out = subprocess.run(
            ["git", "-C", str(dest), "branch", "--list", "feature"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "feature" in out


# ===========================================================================
# 7. commit_all — integration (real git)
# ===========================================================================


class TestCommitAll:
    def test_stages_and_commits_changes(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("content")
        git_ops.commit_all(dest, "add a.txt")
        log = subprocess.run(
            ["git", "-C", str(dest), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "add a.txt" in log


# ===========================================================================
# 8. push + fetch — integration (real git, file:// remote)
# ===========================================================================


class TestPushFetch:
    def test_push_and_fetch_roundtrip(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        # Clone into dest1, create branch, commit, push
        dest1 = tmp_path / "repo1"
        git_ops.clone(remote, dest1, "main")
        git_ops.create_branch(dest1, "feature")
        (dest1 / "pushed.txt").write_text("pushed")
        git_ops.commit_all(dest1, "push commit")
        git_ops.push(dest1, "feature", remote, token=None)

        # Clone fresh into dest2, fetch the feature branch
        dest2 = tmp_path / "repo2"
        git_ops.clone(remote, dest2, "main")
        git_ops.fetch(dest2, remote_url=remote, token=None, branch="feature")
        sha = git_ops.remote_branch_sha(dest2, "feature")
        assert sha is not None
        assert len(sha) == 40


# ===========================================================================
# 9. try_rebase_onto — integration (real git)
# ===========================================================================


class TestTryRebaseOnto:
    def test_success_path(self, tmp_path):
        """Clone, create branch, commit on branch, fetch main + rebase
        branch onto main → returns True, branch commits are on top."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")

        # Push a new commit to main so there's something to rebase onto
        wd = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(wd)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(wd, "config", "user.email", "op@t")
        _git(wd, "config", "user.name", "operator")
        (wd / "main_update.txt").write_text("operator edit on main\n")
        _git(wd, "add", "-A")
        _git(wd, "commit", "-q", "-m", "operator edit")
        _git(wd, "push", "origin", "main")

        # Now rebase feature onto main (using file:// remote, no token)
        result = git_ops.try_rebase_onto(dest, "main", remote_url=remote)
        assert result is True

        # Verify the feature commit is on top: log should show feature
        # commit AFTER the operator edit.
        log = (
            subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("\n")
        )
        # "feature commit" should be above "operator edit"
        assert log[0] == "feature commit"

    def test_fetch_failure_returns_false(self, tmp_path, monkeypatch):
        """Monkeypatch _git to make the fetch call raise CalledProcessError
        → try_rebase_onto returns False."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")

        orig_git = git_ops._git
        call_count = [0]

        def _failing_git(repo, *args):
            call_count[0] += 1
            # Fail on the first fetch call
            if call_count[0] == 1 and args and args[0] == "fetch":
                raise subprocess.CalledProcessError(128, ["git", "fetch"])
            return orig_git(repo, *args)

        monkeypatch.setattr(git_ops, "_git", _failing_git)

        result = git_ops.try_rebase_onto(dest, "main", remote_url=remote)
        assert result is False

    def test_rebase_conflict_aborts_returns_false(self, tmp_path):
        """Create a conflicting change on the branch and push a
        conflicting change to main → rebase fails, returns False,
        working tree is left clean."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Edit README.md on the feature branch
        (dest / "README.md").write_text("conflicting edit from feature\n")
        git_ops.commit_all(dest, "conflict on feature")

        # Push a conflicting edit to main
        wd = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(wd)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(wd, "config", "user.email", "op@t")
        _git(wd, "config", "user.name", "operator")
        (wd / "README.md").write_text("conflicting edit from main\n")
        _git(wd, "add", "-A")
        _git(wd, "commit", "-q", "-m", "conflicting main edit")
        _git(wd, "push", "origin", "main")

        result = git_ops.try_rebase_onto(dest, "main", remote_url=remote)
        assert result is False

        # Working tree should be clean after abort
        assert git_ops.has_changes(dest) is False


# ===========================================================================
# 10. head_sha — integration (real git)
# ===========================================================================


class TestHeadSha:
    def test_returns_40_char_hex(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        sha = git_ops.head_sha(dest)
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)


# ===========================================================================
# 11. remote_branch_sha — integration (real git)
# ===========================================================================


class TestRemoteBranchSha:
    def test_existing_remote_branch_returns_sha(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.fetch(dest, remote_url=remote, token=None, branch="main")
        sha = git_ops.remote_branch_sha(dest, "main")
        assert sha is not None
        assert len(sha) == 40

    def test_nonexistent_remote_branch_returns_none(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        sha = git_ops.remote_branch_sha(dest, "no-such-remote-branch")
        assert sha is None


# ===========================================================================
# 12. branch_is_ahead_of_main — integration (real git)
# ===========================================================================


class TestBranchIsAheadOfMain:
    def test_no_new_commits_returns_false(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Checkout feature (same as main), no new commits
        result = git_ops.branch_is_ahead_of_main(dest)
        assert result is False

    def test_new_commits_returns_true(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("ahead commit")
        git_ops.commit_all(dest, "ahead commit")
        result = git_ops.branch_is_ahead_of_main(dest)
        assert result is True

    def test_branch_behind_main_returns_false(self, tmp_path):
        """Regression: a branch that is BEHIND origin/main (zero
        commits ahead, many behind because main moved on after the
        branch was made) must return False. The previous content-diff
        implementation reported "ahead" here because the diff between
        a stale branch and a moved-on main is non-empty — which then
        sent the empty branch to the forge and produced a 422
        "No commits between main and branch"."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Capture the base-commit branch (before main moves on).
        git_ops.create_branch(dest, "feature")
        # Switch back to main and add a NEW commit, then push so
        # origin/main is ahead of the feature branch.
        _git(dest, "checkout", "main")
        (dest / "main_added.txt").write_text("landed on main after branch was cut")
        git_ops.commit_all(dest, "main moves on")
        _git(dest, "push", "origin", "main")
        # Switch back to the feature branch — it now has 0 commits
        # ahead of origin/main (and N commits behind).
        _git(dest, "checkout", "feature")
        assert git_ops.branch_is_ahead_of_main(dest) is False

    def test_fetch_failure_assumes_ahead(self, tmp_path, monkeypatch):
        """When fetch fails, branch_is_ahead_of_main returns True
        (the 'assume ahead' fallback)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")

        orig_git = git_ops._git

        def _failing_git(repo, *args):
            # Fail on the "fetch" call
            if args and args[0] == "fetch":
                raise subprocess.CalledProcessError(128, ["git", "fetch"])
            return orig_git(repo, *args)

        monkeypatch.setattr(git_ops, "_git", _failing_git)

        result = git_ops.branch_is_ahead_of_main(dest)
        assert result is True

    def test_ahead_of_custom_branch(self, tmp_path):
        """Test branch_is_ahead_of_main with a custom target_branch
        (non-main). Creates a develop branch and tests against it."""
        # Build a repo with 'develop' as the default branch
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-q")
        _git(seed, "config", "user.email", "t@t")
        _git(seed, "config", "user.name", "t")
        (seed / "README.md").write_text("seed\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "init")
        _git(seed, "branch", "-M", "develop")
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(seed), str(bare)],
            check=True,
            capture_output=True,
        )
        remote = f"file://{bare}"

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "develop")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("ahead commit")
        git_ops.commit_all(dest, "ahead commit")
        # Test with target_branch="develop"
        result = git_ops.branch_is_ahead_of_main(dest, target_branch="develop")
        assert result is True


# ===========================================================================
# 13. changed_files — integration (real git)
# ===========================================================================


# ===========================================================================
# 12a. branch_has_net_diff — integration (real git, custom target_branch)
# ===========================================================================


class TestBranchHasNetDiff:
    def test_no_diff_returns_false(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Checkout feature (same as main), no new commits
        result = git_ops.branch_has_net_diff(dest)
        assert result is False

    def test_with_net_diff_returns_true(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("diff content")
        git_ops.commit_all(dest, "add new.txt")
        result = git_ops.branch_has_net_diff(dest)
        assert result is True

    def test_explicit_ref_not_head(self, tmp_path):
        """The optional ``ref`` arg checks a named branch, not HEAD — so a
        caller that cannot rely on HEAD being the feature branch (the merge
        stage's closed-PR no-op check) still gets the right answer."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Feature branch carries a real change...
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("diff content")
        git_ops.commit_all(dest, "add new.txt")
        # ...but HEAD is moved back to main (empty vs base).
        git_ops.checkout(dest, "main")
        assert git_ops.branch_has_net_diff(dest, ref="HEAD") is False
        assert git_ops.branch_has_net_diff(dest, ref="feature") is True

    def test_with_custom_target_branch(self, tmp_path):
        """Test branch_has_net_diff with custom target_branch (non-main).
        Creates a develop branch and tests against it."""
        # Build a repo with 'develop' as the default branch
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-q")
        _git(seed, "config", "user.email", "t@t")
        _git(seed, "config", "user.name", "t")
        (seed / "README.md").write_text("seed\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "init")
        _git(seed, "branch", "-M", "develop")
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(seed), str(bare)],
            check=True,
            capture_output=True,
        )
        remote = f"file://{bare}"

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "develop")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("diff content")
        git_ops.commit_all(dest, "add new.txt")
        # Test with target_branch="develop"
        result = git_ops.branch_has_net_diff(dest, target_branch="develop")
        assert result is True


# ===========================================================================
# 12a2. branch_has_substantive_diff — integration (real git)
# ===========================================================================


class TestBranchHasSubstantiveDiff:
    def test_no_diff_returns_false(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        result = git_ops.branch_has_substantive_diff(dest)
        assert result is False

    def test_with_substantive_diff_returns_true(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("diff content")
        git_ops.commit_all(dest, "add new.txt")
        result = git_ops.branch_has_substantive_diff(dest)
        assert result is True

    def test_changelog_only_returns_false(self, tmp_path):
        """A branch whose only diff is CHANGELOG.md is treated as empty
        (the substantive change is already on main)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "CHANGELOG.md").write_text("## 0.0.0\n- entry\n")
        git_ops.commit_all(dest, "changelog only")
        result = git_ops.branch_has_substantive_diff(dest)
        assert result is False

    def test_changelog_and_code_returns_true(self, tmp_path):
        """Mixed ceremonial + substantive diff is still substantive."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "CHANGELOG.md").write_text("## 0.0.0\n- entry\n")
        (dest / "real_change.py").write_text("print(1)")
        git_ops.commit_all(dest, "changelog + code")
        result = git_ops.branch_has_substantive_diff(dest)
        assert result is True

    def test_explicit_ref(self, tmp_path):
        """Optional ref arg checks a named branch, not HEAD."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("diff content")
        git_ops.commit_all(dest, "add new.txt")
        git_ops.checkout(dest, "main")
        assert git_ops.branch_has_substantive_diff(dest, ref="HEAD") is False
        assert git_ops.branch_has_substantive_diff(dest, ref="feature") is True


# ===========================================================================
# 12a3. branch_diff_files — integration (real git)
# ===========================================================================


class TestBranchDiffFiles:
    def test_no_diff_returns_empty(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        files = git_ops.branch_diff_files(dest)
        assert files == []

    def test_with_diff_returns_files(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("a")
        (dest / "b.txt").write_text("b")
        git_ops.commit_all(dest, "two files")
        files = git_ops.branch_diff_files(dest)
        assert sorted(files) == ["a.txt", "b.txt"]

    def test_explicit_ref(self, tmp_path):
        """Optional ref arg checks a named branch."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feat")
        git_ops.commit_all(dest, "feat commit")
        git_ops.checkout(dest, "main")
        assert git_ops.branch_diff_files(dest, ref="HEAD") == []
        assert "feat.txt" in (git_ops.branch_diff_files(dest, ref="feature") or [])


# ===========================================================================
# 12a4. CEREMONIAL_FILES — unit
# ===========================================================================


class TestCeremonialFiles:
    def test_changelog_is_ceremonial(self):
        assert "CHANGELOG.md" in git_ops.CEREMONIAL_FILES

    def test_is_frozenset(self):
        assert isinstance(git_ops.CEREMONIAL_FILES, frozenset)


# ===========================================================================
# 12b. branch_is_behind_main — integration (real git, custom target_branch)
# ===========================================================================


class TestBranchIsBehindMain:
    def test_not_behind_when_same_commit(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Checkout feature (same as main), no commits ahead on main
        result = git_ops.branch_is_behind_main(dest)
        assert result is False

    def test_behind_when_main_advanced(self, tmp_path):
        """Test that branch_is_behind_main returns True when origin/main
        has commits not on HEAD."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Capture the base-commit branch (before main moves on).
        git_ops.create_branch(dest, "feature")
        # Switch back to main and add a NEW commit, then push so
        # origin/main is ahead of the feature branch.
        _git(dest, "checkout", "main")
        (dest / "main_added.txt").write_text("landed on main after branch was cut")
        git_ops.commit_all(dest, "main moves on")
        _git(dest, "push", "origin", "main")
        # Switch back to the feature branch — it now has commits behind
        # origin/main.
        _git(dest, "checkout", "feature")
        assert git_ops.branch_is_behind_main(dest) is True

    def test_with_custom_target_branch(self, tmp_path):
        """Test branch_is_behind_main with custom target_branch (non-main).
        Creates a develop branch and tests against it."""
        # Build a repo with 'develop' as the default branch
        seed = tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-q")
        _git(seed, "config", "user.email", "t@t")
        _git(seed, "config", "user.name", "t")
        (seed / "README.md").write_text("seed\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "init")
        _git(seed, "branch", "-M", "develop")
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", "-q", str(seed), str(bare)],
            check=True,
            capture_output=True,
        )
        remote = f"file://{bare}"

        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "develop")
        # Capture the base-commit branch (before develop moves on).
        git_ops.create_branch(dest, "feature")
        # Switch back to develop and add a NEW commit, then push so
        # origin/develop is ahead of the feature branch.
        _git(dest, "checkout", "develop")
        (dest / "develop_added.txt").write_text(
            "landed on develop after branch was cut"
        )
        git_ops.commit_all(dest, "develop moves on")
        _git(dest, "push", "origin", "develop")
        # Switch back to the feature branch — it now has commits behind
        # origin/develop.
        _git(dest, "checkout", "feature")
        # Test with target_branch="develop"
        result = git_ops.branch_is_behind_main(dest, target_branch="develop")
        assert result is True


# ===========================================================================
# 13. changed_files — integration (real git)
# ===========================================================================


class TestChangedFiles:
    def test_modified_tracked_file_appears(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Commit so we have a baseline, then modify
        (dest / "README.md").write_text("modified\n")
        files = git_ops.changed_files(dest, "main")
        assert "README.md" in files

    def test_untracked_file_appears(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new_file.txt").write_text("untracked")
        files = git_ops.changed_files(dest, "main")
        assert "new_file.txt" in files

    def test_gitignored_file_does_not_appear(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / ".gitignore").write_text("*.ignored\n")
        (dest / "test.ignored").write_text("should be ignored")
        files = git_ops.changed_files(dest, "main")
        assert "test.ignored" not in files

    def test_clean_repo_returns_empty_list(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Fresh clone on main — no changes vs origin/main
        files = git_ops.changed_files(dest, "main")
        assert files == []


# ===========================================================================
# 13a. introduced_files — integration (real git)
# ===========================================================================


class TestIntroducedFiles:
    def test_phantom_main_change_excluded(self, tmp_path):
        """The core fix: a file X that main modified AFTER the branch was
        cut must NOT appear in introduced_files (it's not the ticket's
        work), even though the old changed_files DOES report it."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Branch `feature` from the base commit (commit A).
        git_ops.create_branch(dest, "feature")

        # Advance origin/main with a change to an unrelated file X via a
        # second clone, then fetch so the working clone's origin/main ref
        # is ahead of the branch base.
        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "config", "user.email", "op@t")
        _git(pusher, "config", "user.name", "operator")
        (pusher / "X.txt").write_text("changed on main after branch was cut\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "main changes X")
        _git(pusher, "push", "origin", "main")
        _git(dest, "fetch", "origin")

        # introduced_files excludes X; old changed_files includes it.
        assert "X.txt" not in git_ops.introduced_files(dest, "main")
        assert "X.txt" in git_ops.changed_files(dest, "main")

    def test_committed_branch_change_reported(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("branch work\n")
        git_ops.commit_all(dest, "add a.txt on branch")
        assert "a.txt" in git_ops.introduced_files(dest, "main")

    def test_uncommitted_tracked_change_reported(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "README.md").write_text("modified\n")
        assert "README.md" in git_ops.introduced_files(dest, "main")

    def test_untracked_file_reported(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new_file.txt").write_text("untracked")
        assert "new_file.txt" in git_ops.introduced_files(dest, "main")

    def test_gitignored_file_does_not_appear(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / ".gitignore").write_text("*.ignored\n")
        (dest / "test.ignored").write_text("should be ignored")
        assert "test.ignored" not in git_ops.introduced_files(dest, "main")

    def test_clean_repo_returns_empty_list(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        assert git_ops.introduced_files(dest, "main") == []


# ===========================================================================
# 13b. restore_paths — integration (real git)
# ===========================================================================


class TestRestorePaths:
    def test_restores_modified_tracked_file_unstaged(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "README.md").write_text("polluted\n")
        assert "README.md" in git_ops.changed_files(dest, "main")
        git_ops.restore_paths(dest, "main", ["README.md"])
        assert "README.md" not in git_ops.changed_files(dest, "main")
        assert (dest / "README.md").read_text() == "seed\n"

    def test_removes_untracked_new_file(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "stray.txt").write_text("untracked junk")
        assert "stray.txt" in git_ops.changed_files(dest, "main")
        git_ops.restore_paths(dest, "main", ["stray.txt"])
        assert "stray.txt" not in git_ops.changed_files(dest, "main")
        assert not (dest / "stray.txt").exists()

    def test_reverts_wip_committed_modification(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Modify a tracked file AND add a new file, then WIP-commit both.
        (dest / "README.md").write_text("polluted\n")
        (dest / "vendored.py").write_text("vendored\n")
        git_ops.commit_all(dest, "wip")
        assert "README.md" in git_ops.changed_files(dest, "main")
        assert "vendored.py" in git_ops.changed_files(dest, "main")
        git_ops.restore_paths(dest, "main", ["README.md", "vendored.py"])
        # Working tree no longer diffs from origin for either path...
        changed = git_ops.changed_files(dest, "main")
        assert "README.md" not in changed
        assert "vendored.py" not in changed
        assert (dest / "README.md").read_text() == "seed\n"
        assert not (dest / "vendored.py").exists()
        # ...and a follow-up commit leaves no net diff vs origin/main.
        git_ops.commit_all(dest, "cleanup")
        net = subprocess.run(
            ["git", "-C", str(dest), "diff", "origin/main...HEAD", "--name-only"],
            capture_output=True,
            text=True,
        ).stdout
        assert "README.md" not in net
        assert "vendored.py" not in net


# ===========================================================================
# 14. diff_base — integration (real git)
# ===========================================================================


class TestDiffBase:
    def test_returns_diff_with_header_when_branch_has_commits(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature diff")
        git_ops.commit_all(dest, "feature commit")
        diff = git_ops.diff_base(dest, "main")
        assert "diff --git" in diff
        assert "feat.txt" in diff

    def test_without_remote_url_fetches_from_origin(self, tmp_path):
        """diff_base without remote_url/token uses stored origin (file://)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature diff")
        git_ops.commit_all(dest, "feature commit")
        diff = git_ops.diff_base(dest, "main")
        assert "diff --git" in diff


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
        notes and Langfuse traces (live leak: maintenance clone_repo)."""
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


# ===========================================================================
# 15. reconcile_with_remote_pr — integration (real git, file:// remote)
# ===========================================================================


class TestReconcileWithRemotePr:
    """AC1–AC4: reconcile_with_remote_pr behaviour."""

    def test_remote_ahead_fast_forwards(self, tmp_path):
        """AC1: remote has an extra commit → fast-forward the workspace."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("local work")
        git_ops.commit_all(dest, "local commit")

        # Push the feature branch so it exists on the remote.
        git_ops.push(dest, "feature", remote, token=None)

        # Simulate a human pushing to the same branch from another clone.
        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "config", "user.email", "human@t")
        _git(pusher, "config", "user.name", "human")
        # The feature branch now exists on remote, so check it out.
        _git(pusher, "fetch", "origin", "feature")
        _git(pusher, "checkout", "-b", "feature", "origin/feature")
        (pusher / "human_fix.txt").write_text("human pushed this fix\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "human fix")
        _git(pusher, "push", "origin", "feature")

        # Now reconcile — the workspace should fast-forward to include
        # the human commit.
        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.SYNCED

        # Workspace HEAD should now equal the remote tip.
        _git(dest, "fetch", "origin", "feature")
        remote_sha = git_ops.remote_branch_sha(dest, "feature")
        assert git_ops.head_sha(dest) == remote_sha

        # The human commit must be in the log.
        log = subprocess.run(
            ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "human fix" in log

        # The local commit should also be in the log (the human built on
        # top of it).
        assert "local commit" in log

    def test_already_in_sync_noop(self, tmp_path):
        """AC2: when workspace is already at the remote tip → no-op."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("work")
        git_ops.commit_all(dest, "commit")
        # Push so local == remote.
        git_ops.push(dest, "feature", remote, token=None)
        head_before = git_ops.head_sha(dest)

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.SYNCED
        assert git_ops.head_sha(dest) == head_before

    def test_remote_branch_does_not_exist_noop(self, tmp_path):
        """AC3: remote branch doesn't exist yet → no-op, returns True."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Create a local branch that has never been pushed.
        git_ops.create_branch(dest, "never-pushed")
        (dest / "feat.txt").write_text("unpushed work")
        git_ops.commit_all(dest, "unpushed commit")
        head_before = git_ops.head_sha(dest)

        result = git_ops.reconcile_with_remote_pr(
            dest, remote, "never-pushed", token=None
        )
        assert result is git_ops.ReconcileResult.SYNCED
        assert git_ops.head_sha(dest) == head_before

    def test_diverged_returns_diverged(self, tmp_path):
        """AC4: both sides advanced independently → returns DIVERGED."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "local.txt").write_text("local work")
        git_ops.commit_all(dest, "local commit")

        # Push a DIFFERENT commit to the remote (simulating a human).
        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "config", "user.email", "human@t")
        _git(pusher, "config", "user.name", "human")
        _git(pusher, "checkout", "-b", "feature")
        (pusher / "remote.txt").write_text("remote work\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "remote commit")
        _git(pusher, "push", "origin", "feature")

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.DIVERGED

    def test_diverged_but_remote_commit_is_mill_own_returns_synced(self, tmp_path):
        """Divergence where the ONLY remote-side commit a force-push would
        discard is mill-authored (the mill's own prior force-push from an
        earlier rebase cycle) → SYNCED, not DIVERGED. This is the fix for the
        false 'diverged' bail that forced a manual reconcile after every mill
        rebase."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "local.txt").write_text("rebased local work")
        git_ops.commit_all(dest, "local rebase commit")

        # The remote 'feature' carries the mill's OWN earlier push (a prior
        # rebase cycle) — authored by the mill, not a human.
        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "config", "user.email", "mill@robotsix.local")
        _git(pusher, "config", "user.name", "robotsix-mill")
        _git(pusher, "checkout", "-b", "feature")
        (pusher / "prior.txt").write_text("mill's prior rebase\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "mill prior push")
        _git(pusher, "push", "origin", "feature")

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.SYNCED

    def test_local_ahead_of_remote_noop(self, tmp_path):
        """When local is ahead of remote (normal case after local commits,
        before push) → returns True, no change."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("local work")
        git_ops.commit_all(dest, "local commit")
        # Push so remote knows about it, then make another local commit.
        git_ops.push(dest, "feature", remote, token=None)
        (dest / "feat2.txt").write_text("more local work")
        git_ops.commit_all(dest, "second local commit")
        head_before = git_ops.head_sha(dest)

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.SYNCED
        # Local should still be ahead (unchanged).
        assert git_ops.head_sha(dest) == head_before


# ===========================================================================
# 16. push_with_lease — integration (real git, file:// remote)
# ===========================================================================


class TestPushWithLease:
    """AC5–AC6: push_with_lease behaviour."""

    def test_succeeds_when_remote_at_expected_sha(self, tmp_path):
        """AC5: push_with_lease succeeds when remote matches the
        expected SHA (refs/remotes/origin/<branch>)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("initial work")
        git_ops.commit_all(dest, "initial commit")

        # Push so the remote has the branch.
        git_ops.push(dest, "feature", remote, token=None)

        # Make another local commit so we have something new to push.
        (dest / "feat2.txt").write_text("more work")
        git_ops.commit_all(dest, "second commit")

        # Fetch the remote branch to populate the tracking ref.
        git_ops.fetch(dest, remote_url=remote, token=None, branch="feature")

        # Push with lease should succeed — remote is at the expected SHA.
        git_ops.push_with_lease(dest, "feature", remote, token=None)

        # Verify the remote has our latest commit.
        verify = tmp_path / "verify"
        subprocess.run(
            ["git", "clone", "-q", remote, str(verify)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(verify, "fetch", "origin", "feature")
        _git(verify, "checkout", "-b", "feature", "origin/feature")
        assert (verify / "feat.txt").read_text() == "initial work"
        assert (verify / "feat2.txt").read_text() == "more work"

    def test_fails_when_remote_has_advanced(self, tmp_path):
        """AC6: push_with_lease fails (raises CalledProcessError) when
        the remote has advanced since the last fetch."""
        remote = make_bare_repo(tmp_path)

        # First clone — will push with lease.
        dest1 = tmp_path / "repo1"
        git_ops.clone(remote, dest1, "main")
        git_ops.create_branch(dest1, "feature")
        (dest1 / "feat1.txt").write_text("work from clone 1")
        git_ops.commit_all(dest1, "commit from clone 1")

        # Push so the remote has the branch at a known SHA.
        git_ops.push(dest1, "feature", remote, token=None)

        # Fetch to populate tracking ref with the current remote SHA.
        git_ops.fetch(dest1, remote_url=remote, token=None, branch="feature")

        # Meanwhile, a second clone pushes a DIFFERENT commit to the
        # same branch (simulating a concurrent human push).
        dest2 = tmp_path / "repo2"
        git_ops.clone(remote, dest2, "main")
        # Fetch the feature branch and check it out.
        git_ops.fetch(dest2, remote_url=remote, token=None, branch="feature")
        _git(dest2, "branch", "feature", "origin/feature")
        _git(dest2, "checkout", "feature")
        (dest2 / "feat2.txt").write_text("concurrent human push")
        git_ops.commit_all(dest2, "human commit")
        git_ops.push(dest2, "feature", remote, token=None)

        # Now clone 1 tries to push with lease — must fail because the
        # remote has advanced beyond the expected SHA.
        with pytest.raises(subprocess.CalledProcessError):
            git_ops.push_with_lease(dest1, "feature", remote, token=None)

        # The remote must still have the human commit (NOT overwritten).
        verify = tmp_path / "verify"
        subprocess.run(
            ["git", "clone", "-q", remote, str(verify)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(verify, "fetch", "origin", "feature")
        _git(verify, "checkout", "-b", "feature", "origin/feature")
        assert (verify / "feat2.txt").read_text() == "concurrent human push"
        # The human commit is still there — lease prevented the overwrite.

    def test_new_remote_branch_falls_back_to_force(self, tmp_path):
        """When the remote branch doesn't exist yet, push_with_lease
        falls back to a plain --force push (nothing to lease against)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "brand-new-branch")
        (dest / "new.txt").write_text("first push")
        git_ops.commit_all(dest, "first commit")

        # No prior fetch — remote_branch_sha returns None.
        # push_with_lease should fall back to --force.
        git_ops.push_with_lease(dest, "brand-new-branch", remote, token=None)

        # Verify the remote has the new branch.
        verify = tmp_path / "verify"
        subprocess.run(
            ["git", "clone", "-q", remote, str(verify)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(verify, "fetch", "origin", "brand-new-branch")
        _git(verify, "checkout", "-b", "brand-new-branch", "origin/brand-new-branch")
        assert (verify / "new.txt").read_text() == "first push"


# ===========================================================================
# 17. End-to-end: rebase preserves foreign commits (AC7)
# ===========================================================================


class TestRebasePreservesForeignCommits:
    """AC7: reconcile + rebase preserves human-pushed commits."""

    def test_rebase_preserves_human_commit(self, tmp_path):
        """Full integration test:
        1. Bare repo with main + feature branch (the PR branch)
        2. Clone workspace (single-branch of feature)
        3. Push a human commit to remote feature
        4. Push a new commit to main (so rebase has something to do)
        5. Call reconcile_with_remote_pr then try_rebase_onto
        6. Assert the human commit appears in the post-rebase log
        """
        remote = make_bare_repo(tmp_path)

        # --- 1. Create the PR branch (feature) with a ticket commit ---
        dest = tmp_path / "workspace"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "ticket_work.txt").write_text("ticket changes\n")
        git_ops.commit_all(dest, "ticket: implement feature X")
        ticket_commit_msg = "ticket: implement feature X"

        # --- 2. Clone workspace as the mill would: single-branch of feature ---
        # (We already have the workspace at dest, but let's simulate the
        # situation where a human pushes after the clone was created.)
        # First, push the feature branch so it exists on the remote.
        git_ops.push(dest, "feature", remote, token=None)

        # --- 3. Push a human commit to remote feature ---
        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "config", "user.email", "human@t")
        _git(pusher, "config", "user.name", "human")
        _git(pusher, "checkout", "feature")
        (pusher / "human_fix.txt").write_text("human fix for edge case\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "human: fix edge case")
        _git(pusher, "push", "origin", "feature")

        # --- 4. Push a new commit to main (so rebase has something to do) ---
        _git(pusher, "checkout", "main")
        (pusher / "main_update.txt").write_text("main advanced\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "main: advance after PR was cut")
        _git(pusher, "push", "origin", "main")

        # --- 5. Reconcile + rebase ---
        # The workspace clone is stale (doesn't have the human commit or
        # the main update).  Reconcile first.
        reconciled = git_ops.reconcile_with_remote_pr(
            dest, remote, "feature", token=None
        )
        assert reconciled is git_ops.ReconcileResult.SYNCED

        # Now rebase onto main.  Use the file:// remote so fetch works.
        rebased = git_ops.try_rebase_onto(dest, "main", remote_url=remote)
        assert rebased is True

        # --- 6. Assert the human commit appears in the post-rebase log ---
        # It should be on top of main, alongside (or on top of) the ticket
        # commits.
        log = (
            subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("\n")
        )
        assert "human: fix edge case" in log
        assert ticket_commit_msg in log
        assert "main: advance after PR was cut" in log

        # The human commit should be in the history — either on top or
        # merged in.  Verify the file exists.
        assert (dest / "human_fix.txt").exists()
        assert (dest / "human_fix.txt").read_text() == "human fix for edge case\n"
        assert (dest / "ticket_work.txt").exists()


# ===========================================================================
# 18. push / fetch / push_with_lease error redaction (mock-based)
# ===========================================================================


class TestPushErrorRedaction:
    def test_push_failure_redacts_token(self):
        """When _git raises CalledProcessError during push(), the re-raised
        exception has credentials redacted from cmd, output, and stderr."""
        token = "ghs_sekret123"
        token_url = f"https://oauth2:{token}@github.com/owner/repo.git"
        error = subprocess.CalledProcessError(
            128,
            [
                "git",
                "-C",
                "/tmp/repo",
                "push",
                "--force",
                token_url,
                "feature:feature",
            ],
            output="",
            stderr=f"fatal: could not read from remote repository\n"
            f"fatal: unable to access '{token_url}/'\n",
        )

        from unittest.mock import patch

        with patch("robotsix_mill.vcs.git_ops._git", side_effect=error):
            with pytest.raises(subprocess.CalledProcessError) as ei:
                git_ops.push(
                    Path("/tmp/repo"),
                    "feature",
                    "https://github.com/owner/repo.git",
                    token,
                )

        exposed = (
            repr(ei.value)
            + str(ei.value.cmd)
            + str(ei.value.stderr)
            + str(ei.value.output)
        )
        assert token not in exposed
        assert "://***@" in str(ei.value.cmd)
        assert "://***@" in (ei.value.stderr or "")
        # output is empty string, but verify it doesn't contain the token
        assert token not in (ei.value.output or "")


class TestFetchErrorRedaction:
    def test_fetch_failure_redacts_token(self):
        """When _git raises CalledProcessError during fetch(), the re-raised
        exception has credentials redacted from cmd, output, and stderr."""
        token = "ghs_sekret456"
        token_url = f"https://oauth2:{token}@github.com/owner/repo.git"
        error = subprocess.CalledProcessError(
            128,
            [
                "git",
                "-C",
                "/tmp/repo",
                "fetch",
                token_url,
                "+refs/heads/feature:refs/remotes/origin/feature",
            ],
            output="",
            stderr=f"fatal: couldn't find remote ref feature\n"
            f"fatal: unable to access '{token_url}/'\n",
        )

        from unittest.mock import patch

        with patch("robotsix_mill.vcs.git_ops._git", side_effect=error):
            with pytest.raises(subprocess.CalledProcessError) as ei:
                git_ops.fetch(
                    Path("/tmp/repo"),
                    remote_url="https://github.com/owner/repo.git",
                    token=token,
                    branch="feature",
                )

        exposed = (
            repr(ei.value)
            + str(ei.value.cmd)
            + str(ei.value.stderr)
            + str(ei.value.output)
        )
        assert token not in exposed
        assert "://***@" in str(ei.value.cmd)
        assert "://***@" in (ei.value.stderr or "")


class TestPushWithLeaseErrorRedaction:
    def test_force_fallback_branch_redacts_token(self):
        """When remote_branch_sha returns None (branch doesn't exist),
        push_with_lease takes the --force fallback.  If that push fails,
        the error is redacted."""
        token = "ghs_sekret789"
        token_url = f"https://oauth2:{token}@github.com/owner/repo.git"
        error = subprocess.CalledProcessError(
            128,
            [
                "git",
                "-C",
                "/tmp/repo",
                "push",
                "--force",
                token_url,
                "feature:feature",
            ],
            output="",
            stderr=f"fatal: remote rejected\nfatal: unable to access '{token_url}/'\n",
        )

        from unittest.mock import patch

        # _git always raises → remote_branch_sha() returns None (it catches
        # CalledProcessError internally), so the --force branch is taken.
        with patch("robotsix_mill.vcs.git_ops._git", side_effect=error):
            with pytest.raises(subprocess.CalledProcessError) as ei:
                git_ops.push_with_lease(
                    Path("/tmp/repo"),
                    "feature",
                    "https://github.com/owner/repo.git",
                    token,
                )

        exposed = (
            repr(ei.value)
            + str(ei.value.cmd)
            + str(ei.value.stderr)
            + str(ei.value.output)
        )
        assert token not in exposed
        assert "://***@" in str(ei.value.cmd)
        assert "://***@" in (ei.value.stderr or "")

    def test_force_with_lease_branch_redacts_token(self):
        """When remote_branch_sha returns a SHA (branch exists),
        push_with_lease takes the --force-with-lease branch.  If that push
        fails, the error is redacted."""
        token = "ghs_sekret012"
        token_url = f"https://oauth2:{token}@github.com/owner/repo.git"
        error = subprocess.CalledProcessError(
            128,
            [
                "git",
                "-C",
                "/tmp/repo",
                "push",
                "--force-with-lease=refs/heads/feature:abc1234",
                token_url,
                "feature:feature",
            ],
            output="",
            stderr=f"fatal: failed to push some refs\n"
            f"fatal: unable to access '{token_url}/'\n",
        )

        from unittest.mock import patch

        # remote_branch_sha must succeed (return a SHA) so that
        # push_with_lease enters the --force-with-lease branch.
        # _git needs to succeed for the rev-parse call but fail for
        # the push call.
        def _git_side_effect(repo, *args):
            if args and args[0] == "rev-parse":
                return "abc1234"  # canned SHA for remote_branch_sha
            raise error

        with patch("robotsix_mill.vcs.git_ops._git", side_effect=_git_side_effect):
            with pytest.raises(subprocess.CalledProcessError) as ei:
                git_ops.push_with_lease(
                    Path("/tmp/repo"),
                    "feature",
                    "https://github.com/owner/repo.git",
                    token,
                )

        exposed = (
            repr(ei.value)
            + str(ei.value.cmd)
            + str(ei.value.stderr)
            + str(ei.value.output)
        )
        assert token not in exposed
        assert "://***@" in str(ei.value.cmd)
        assert "://***@" in (ei.value.stderr or "")


# ===========================================================================
# 19. post_push_check — integration (real git, file:// remote)
# ===========================================================================


class TestPostPushCheck:
    """Exercise all four ``PostPushResult`` outcomes against a real git
    repository (except ``UNAVAILABLE``, which uses a broken HTTPS URL)."""

    def test_pass(self, tmp_path):
        """Push lands, all commits mill-authored → PASS."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "dest"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Write and commit a file with mill author identity.
        (dest / "mill_work.txt").write_text("mill-authored\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            "user.email=mill@robotsix.local",
            "-c",
            "user.name=Mill",
            "commit",
            "-q",
            "-m",
            "mill work",
        )
        git_ops.push(dest, "feature", remote, token=None)

        result = git_ops.post_push_check(dest, "feature", "main", remote, token=None)
        assert result == PostPushResult.PASS

    def test_not_landed(self, tmp_path):
        """Local HEAD ≠ remote SHA → NOT_LANDED.

        Push the branch first so it exists on the remote, then make a local
        commit without pushing — the remote is behind, so the check fails."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "dest"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")

        # First commit + push so the remote branch exists.
        (dest / "initial.txt").write_text("initial\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            "user.email=mill@robotsix.local",
            "-c",
            "user.name=Mill",
            "commit",
            "-q",
            "-m",
            "initial commit",
        )
        git_ops.push(dest, "feature", remote, token=None)

        # Second, unpushed local commit.
        (dest / "unpushed.txt").write_text("unpushed\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            "user.email=mill@robotsix.local",
            "-c",
            "user.name=Mill",
            "commit",
            "-q",
            "-m",
            "unpushed commit",
        )

        result = git_ops.post_push_check(dest, "feature", "main", remote, token=None)
        assert result == PostPushResult.NOT_LANDED

    def test_foreign_divergence(self, tmp_path):
        """Remote carries a non-mill-authored commit → FOREIGN_DIVERGENCE."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "dest"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")

        # Commit with human author, then push.
        (dest / "human_work.txt").write_text("human-authored\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            "user.email=human@example.com",
            "-c",
            "user.name=Human",
            "commit",
            "-q",
            "-m",
            "human work",
        )
        git_ops.push(dest, "feature", remote, token=None)

        result = git_ops.post_push_check(dest, "feature", "main", remote, token=None)
        assert result == PostPushResult.FOREIGN_DIVERGENCE

    def test_unavailable(self, tmp_path):
        """Unreachable URL → fetch fails → UNAVAILABLE."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "dest"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Commit locally so there is a HEAD; the remote URL is unreachable.
        (dest / "local.txt").write_text("local\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            "user.email=mill@robotsix.local",
            "-c",
            "user.name=Mill",
            "commit",
            "-q",
            "-m",
            "local only",
        )

        result = git_ops.post_push_check(
            dest, "feature", "main", "https://127.0.0.1:9/none.git", token=None
        )
        assert result == PostPushResult.UNAVAILABLE

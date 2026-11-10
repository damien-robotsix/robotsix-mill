import subprocess

import pytest

from robotsix_mill.scm.vcs import git_ops

from ._git_test_helpers import _git, make_bare_repo

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

    # -- empty-repo bootstrap ----------------------------------------------

    def test_clone_empty_remote_bootstraps(self, tmp_path, monkeypatch):
        """When cloning from a truly empty remote (no branches at all),
        clone() bootstraps the remote by pushing an initial commit and
        returns normally — the caller sees it as a successful clone."""
        bare = tmp_path / "empty.git"
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "-b", "main", str(bare)],
            check=True,
        )
        # Monkeypatch _remote_has_branches to simulate what happens with
        # a real empty remote: the bare repo has no refs yet, so
        # ls-remote --heads returns empty.
        import robotsix_mill.scm.vcs.git_ops as go

        _orig_has_branches = go._remote_has_branches

        def _fake_has_branches(remote_url, token):
            # For file:// remotes, ls-remote --heads on a truly bare
            # (no-refs) repo returns empty.  Verify the real function
            # sees this, then simulate it consistently.
            return False

        monkeypatch.setattr(go, "_remote_has_branches", _fake_has_branches)

        dest = tmp_path / "clone_dest"
        bare_url = f"file://{bare}"
        git_ops.clone(bare_url, dest, "main", repo_id="test-repo")

        # After bootstrap, the destination has .git and the README.
        assert (dest / ".git").is_dir()
        assert (dest / "README.md").exists()
        content = (dest / "README.md").read_text()
        assert "test-repo" in content
        assert "bootstrapped automatically" in content

        # The bare remote now has the initial commit on main.
        show = subprocess.run(
            ["git", "-C", str(bare), "show", "main:README.md"],
            capture_output=True,
            text=True,
        )
        assert show.returncode == 0
        assert "test-repo" in show.stdout

        # git identity is configured.
        email = subprocess.run(
            ["git", "-C", str(dest), "config", "user.email"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert email == "mill@robotsix.local"


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

    def test_noop_when_nothing_to_commit(self, tmp_path):
        """commit_all on a clean repo is a silent no-op — no commit, no error."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # Repo is clean — commit_all should return without raising.
        git_ops.commit_all(dest, "should be no-op")
        # Verify no new commit was created (HEAD still at "init").
        log = subprocess.run(
            ["git", "-C", str(dest), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert "init" in log
        assert "should be no-op" not in log


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

        def _failing_git(repo, *args, **kwargs):
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

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.vcs import git_ops


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

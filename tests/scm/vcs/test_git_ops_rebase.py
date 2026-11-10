import subprocess

from robotsix_mill.scm.vcs import git_ops

from ._git_test_helpers import _git, make_bare_repo

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

        def _failing_git(repo, *args, **kwargs):
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
# 9b. try_mechanical_rebase — integration (real git)
# ===========================================================================


class TestTryMechanicalRebase:
    def test_clean_rebase_returns_true(self, tmp_path):
        """Branch with a single commit rebases cleanly onto updated main."""
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

        result = git_ops.try_mechanical_rebase(dest, "main")
        assert result is True

        # Verify the feature commit is on top
        log = (
            subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("\n")
        )
        assert log[0] == "feature commit"

    def test_conflict_aborts_returns_false(self, tmp_path):
        """Create a conflicting change on the branch and push a
        conflicting change to main → rebase fails, returns False,
        working tree is left clean, no ongoing rebase."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
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

        result = git_ops.try_mechanical_rebase(dest, "main")
        assert result is False

        # Working tree should be clean after abort
        assert git_ops.has_changes(dest) is False
        # No ongoing rebase: git should report we are on the feature branch
        branch = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "feature"


# ===========================================================================
# 9c. try_rebase_onto_branch — integration (real git)
# ===========================================================================


class TestTryRebaseOntoBranch:
    def test_rebase_onto_remote_branch_tip(self, tmp_path):
        """Local carries an extra fix commit while the remote branch tip moved
        forward concurrently (e.g. a mill rebase) — the fix is replayed on top
        of the current remote tip."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")
        git_ops.push(dest, "feature", remote, token=None)

        # Local-only fix commit on top of the pushed feature branch.
        (dest / "feat.txt").write_text("the agent's fix\n")
        git_ops.commit_all(dest, "agent fix")

        # Concurrent remote move: a second clone pushes to feature.
        wd = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(wd)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(wd, "config", "user.email", "op@t")
        _git(wd, "config", "user.name", "operator")
        _git(wd, "checkout", "-q", "feature")
        (wd / "concurrent.txt").write_text("concurrent remote work\n")
        _git(wd, "add", "-A")
        _git(wd, "commit", "-q", "-m", "concurrent remote work")
        _git(wd, "push", "origin", "feature")

        result = git_ops.try_rebase_onto_branch(
            dest, "feature", remote_url=remote, token=None
        )
        assert result is True

        log = (
            subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("\n")
        )
        # The agent's fix lands on top of the concurrent remote work.
        assert log[0] == "agent fix"
        assert log[1] == "concurrent remote work"

    def test_noop_when_remote_is_ancestor(self, tmp_path):
        """Remote tip is already an ancestor of local (the fix exists only
        locally, remote never moved) — rebase is a no-op, returns True, and
        the local fix commit is preserved."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")
        git_ops.push(dest, "feature", remote, token=None)

        before = git_ops.head_sha(dest)
        (dest / "feat.txt").write_text("the agent's fix\n")
        git_ops.commit_all(dest, "agent fix")

        result = git_ops.try_rebase_onto_branch(
            dest, "feature", remote_url=remote, token=None
        )
        assert result is True
        assert git_ops.head_sha(dest) != before  # fix commit still present
        log = (
            subprocess.run(
                ["git", "-C", str(dest), "log", "--oneline", "--format=%s"],
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("\n")
        )
        assert log[0] == "agent fix"

    def test_fetch_failure_returns_false(self, tmp_path, monkeypatch):
        """A failing fetch (network error) → returns False so the caller stops
        retrying instead of pushing blind."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "feat.txt").write_text("feature work")
        git_ops.commit_all(dest, "feature commit")
        git_ops.push(dest, "feature", remote, token=None)
        (dest / "feat.txt").write_text("the agent's fix\n")
        git_ops.commit_all(dest, "agent fix")

        orig_git = git_ops._git
        call_count = [0]

        def _failing_git(repo, *args, **kwargs):
            call_count[0] += 1
            # Fail the first fetch call.
            if call_count[0] == 1 and args and args[0] == "fetch":
                raise subprocess.CalledProcessError(128, ["git", "fetch"])
            return orig_git(repo, *args)

        monkeypatch.setattr(git_ops, "_git", _failing_git)

        result = git_ops.try_rebase_onto_branch(
            dest, "feature", remote_url=remote, token=None
        )
        assert result is False

    def test_rebase_conflict_aborts_returns_false(self, tmp_path):
        """Local fix conflicts with the current remote tip — rebase fails,
        returns False, and the working tree is left clean with no ongoing
        rebase."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "README.md").write_text("conflicting edit from feature\n")
        git_ops.commit_all(dest, "conflict on feature")
        git_ops.push(dest, "feature", remote, token=None)

        # Local second commit editing the same line.
        (dest / "README.md").write_text("agent's conflicting fix\n")
        git_ops.commit_all(dest, "agent fix")

        # Concurrent remote move editing the same file line.
        wd = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(wd)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(wd, "config", "user.email", "op@t")
        _git(wd, "config", "user.name", "operator")
        _git(wd, "checkout", "-q", "feature")
        (wd / "README.md").write_text("concurrent remote edit\n")
        _git(wd, "add", "-A")
        _git(wd, "commit", "-q", "-m", "concurrent remote edit")
        _git(wd, "push", "origin", "feature")

        result = git_ops.try_rebase_onto_branch(
            dest, "feature", remote_url=remote, token=None
        )
        assert result is False
        assert git_ops.has_changes(dest) is False
        branch = subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "feature"


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
# 13a. changed_source_files — integration (real git)
# ===========================================================================


class TestChangedSourceFiles:
    def test_committed_file_appears(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "new.txt").write_text("branch work")
        git_ops.commit_all(dest, "add new.txt")
        files = git_ops.changed_source_files(dest, "main")
        assert "new.txt" in files

    def test_clean_branch_returns_empty(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        # No changes — same as origin/main
        files = git_ops.changed_source_files(dest, "main")
        assert files == []

    def test_uncommitted_changes_not_reported(self, tmp_path):
        """Only committed (--diff-filter=AM) files appear; working-tree
        dirt is invisible — this is the contract the rebase integrity
        check relies on."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "dirty.txt").write_text("unstaged")
        files = git_ops.changed_source_files(dest, "main")
        assert "dirty.txt" not in files

    def test_deleted_file_not_reported(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "README.md").unlink()
        git_ops.commit_all(dest, "remove readme")
        files = git_ops.changed_source_files(dest, "main")
        assert "README.md" not in files

    def test_changelog_excluded_by_check_not_by_collector(self, tmp_path):
        """changed_source_files does NOT filter CHANGELOG.md — the
        exclusion is the caller's responsibility."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "CHANGELOG.md").write_text("updated")
        git_ops.commit_all(dest, "changelog")
        files = git_ops.changed_source_files(dest, "main")
        assert "CHANGELOG.md" in files


# ===========================================================================
# 13b. check_rebase_diff_integrity — unit
# ===========================================================================


class TestCheckRebaseDiffIntegrity:
    def test_no_drops_returns_ok(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("branch work")
        (dest / "b.txt").write_text("more work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", ["a.txt", "b.txt"]
        )
        assert ok is True
        assert dropped == []

    def test_dropped_file_detected(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("branch work")
        git_ops.commit_all(dest, "implement")

        # Pre-rebase files include b.txt, but b.txt is not in the diff
        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", ["a.txt", "b.txt"]
        )
        assert ok is False
        assert dropped == ["b.txt"]

    def test_changelog_excluded_from_comparison(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "src").mkdir(parents=True, exist_ok=True)
        (dest / "src" / "mod.py").write_text("real work")
        git_ops.commit_all(dest, "implement")

        # CHANGELOG.md in pre-rebase list but not in post-rebase diff
        # should NOT trigger a drop.
        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", ["src/mod.py", "CHANGELOG.md"]
        )
        assert ok is True
        assert dropped == []

    def test_modules_yaml_registry_excluded(self, tmp_path):
        """``docs/modules.yaml`` is a registry CI re-derives, not
        implement-stage content.

        Its content is a function of the whole repo, so a rebase settles it on
        a version matching neither the branch nor the target's previous one —
        which the blob-equality excuse cannot clear. Live on 2026-08-07 that
        blocked six auto-mail tickets in twelve minutes.
        """
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "src").mkdir(parents=True, exist_ok=True)
        (dest / "src" / "mod.py").write_text("real work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", ["src/mod.py", "docs/modules.yaml"]
        )
        assert ok is True
        assert dropped == []

    def test_exempt_paths_override_replaces_defaults(self, tmp_path):
        """An explicit *exempt_paths* replaces the default list rather than
        extending it — so a caller can narrow the guard's blind spot."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "src").mkdir(parents=True, exist_ok=True)
        (dest / "src" / "mod.py").write_text("real work")
        git_ops.commit_all(dest, "implement")

        # Only docs/ is exempt now, so CHANGELOG.md is back in scope.
        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest,
            "main",
            ["src/mod.py", "CHANGELOG.md", "docs/modules.yaml"],
            exempt_paths=["docs/"],
        )
        assert ok is False
        assert dropped == ["CHANGELOG.md"]

    def test_empty_exempt_paths_disables_exemptions(self, tmp_path):
        """An empty list is honoured as "exempt nothing", not treated as
        unset — otherwise an operator could not turn the exemptions off."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "src").mkdir(parents=True, exist_ok=True)
        (dest / "src" / "mod.py").write_text("real work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", ["src/mod.py", "CHANGELOG.md"], exempt_paths=[]
        )
        assert ok is False
        assert dropped == ["CHANGELOG.md"]

    def test_exempt_paths_do_not_mask_real_source_drops(self, tmp_path):
        """The exemption must not swallow a genuine implement-stage drop that
        happens to sit alongside exempt registry files."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "src").mkdir(parents=True, exist_ok=True)
        (dest / "src" / "kept.py").write_text("real work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest,
            "main",
            ["src/kept.py", "src/discarded.py", "docs/modules.yaml", "CHANGELOG.md"],
        )
        assert ok is False
        assert dropped == ["src/discarded.py"]

    def test_empty_pre_list_returns_ok(self, tmp_path):
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "a.txt").write_text("work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(dest, "main", [])
        assert ok is True
        assert dropped == []

    def test_custom_target_branch(self, tmp_path):
        """Integrity check works with a non-main target branch."""
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
        (dest / "mod.py").write_text("work")
        git_ops.commit_all(dest, "implement")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "develop", ["mod.py"]
        )
        assert ok is True
        assert dropped == []

    def test_dropped_when_base_deleted_file_pr_modified(self, tmp_path):
        """When the base branch deleted a file the PR modified, and the
        rebase agent accepts the deletion, the integrity check must flag
        the file as dropped (the pre-rebase blob differs from the
        non-existent target blob)."""
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        # Simulate a PR branch that modified mod.py
        git_ops.create_branch(dest, "feature")
        (dest / "mod.py").write_text("branch work")
        git_ops.commit_all(dest, "implement: modify mod.py")
        pre_files = git_ops.changed_source_files(dest, "main")
        assert "mod.py" in pre_files
        pre_blobs = git_ops.file_blobs(dest, pre_files)

        # Simulate: the rebase agent erroneously accepted the target's
        # deletion of mod.py. After rebase, mod.py is gone.
        (dest / "mod.py").unlink()
        git_ops.commit_all(dest, "rebase: incorrectly dropped mod.py")

        ok, dropped, _s = git_ops.check_rebase_diff_integrity(
            dest, "main", pre_files, pre_blobs
        )
        assert ok is False, "integrity check must flag the dropped file"
        assert "mod.py" in dropped

    def test_sibling_modified_target_produces_sibling_likely_not_dropped(
        self, tmp_path
    ):
        """When the target branch changed a file during the rebase window
        (a sibling PR landed), the file must appear in *sibling_likely*,
        not *dropped*."""
        repo, run = _init_repo_with_target(tmp_path)
        # Branch modifies Dockerfile to its own version.
        (repo / "Dockerfile").write_text("FROM base:1.2.3\n")
        run("add", "-A")
        run("commit", "-qm", "pin base image")
        pre_files = git_ops.changed_source_files(repo, "main")
        pre_blobs = git_ops.file_blobs(repo, pre_files)

        # Snapshot target's pre-rebase content (the original Dockerfile).
        target_pre_blobs = git_ops.file_blobs(repo, pre_files, ref="origin/main")

        # Simulate: a sibling PR landed on main, changing Dockerfile
        # DIFFERENTLY during our rebase window.  The agent correctly
        # took the sibling's version — our delta is superseded.
        run("branch", "feature")
        run("checkout", "main")
        (repo / "Dockerfile").write_text("FROM base:2.0\n")
        run("add", "-A")
        run("commit", "-qm", "sibling PR: bump to 2.0")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        # Return to feature and rebase onto the sibling's main.
        run("checkout", "feature")
        run("reset", "--hard", "origin/main")
        (repo / "keep.py").write_text("x = 1\n")
        run("add", "-A")
        run("commit", "-qm", "branch work after rebase")

        ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
            repo,
            "main",
            pre_files,
            pre_blobs,
            target_pre_blobs=target_pre_blobs,
        )
        assert ok is False, "sibling-likely files must cause not-ok"
        assert dropped == [], "sibling-modified file must not be in dropped"
        assert "Dockerfile" in sibling

    def test_genuine_drop_target_unchanged(self, tmp_path):
        """When the target branch did NOT change a file during the rebase
        window, the file must appear in *dropped*, not *sibling_likely*."""
        repo, run = _init_repo_with_target(tmp_path)
        (repo / "Dockerfile").write_text("FROM base:1.2.3\n")
        run("add", "-A")
        run("commit", "-qm", "pin base image")
        pre_files = git_ops.changed_source_files(repo, "main")
        pre_blobs = git_ops.file_blobs(repo, pre_files)

        # Target's pre-rebase and post-rebase content match (no sibling).
        # Both capture the same origin/main blob — Dockerfile unchanged.
        target_pre_blobs = git_ops.file_blobs(repo, pre_files, ref="origin/main")
        target_post_blobs = git_ops.file_blobs(repo, pre_files, ref="origin/main")
        assert target_pre_blobs == target_post_blobs

        # Simulate rebase that erroneously dropped Dockerfile.
        (repo / "Dockerfile").unlink()
        run("add", "-A")
        run("commit", "-qm", "rebase: incorrectly dropped Dockerfile")

        ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
            repo,
            "main",
            pre_files,
            pre_blobs,
            target_pre_blobs=target_pre_blobs,
        )
        assert ok is False, "dropped files must cause not-ok"
        assert "Dockerfile" in dropped
        assert "Dockerfile" not in sibling


# ===========================================================================
# 13c. introduced_files — integration (real git)
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


# --- rebase integrity: "already upstream" is not a silent drop -----------


def _init_repo_with_target(tmp_path):
    """Build a repo with an origin/main ref and a feature branch on top."""
    repo = tmp_path / "r"
    repo.mkdir()

    def run(*a):
        return subprocess.run(
            ["git", "-C", str(repo), *a], check=True, capture_output=True, text=True
        )

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "Dockerfile").write_text("FROM base\n")
    (repo / "keep.py").write_text("x = 0\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, run


def test_change_already_landed_upstream_is_not_a_drop(tmp_path):
    """A sibling PR landing the same change first must not block this one.

    The rebase correctly collapses the now-redundant delta to nothing.
    Live, this dead-ended ten tickets on the identical `Dockerfile`
    immediately after the canonical fix for it merged from another PR.
    """
    repo, run = _init_repo_with_target(tmp_path)
    # What THIS branch wanted to deliver for Dockerfile.
    (repo / "Dockerfile").write_text("FROM base:1.2.3\n")
    run("add", "-A")
    run("commit", "-qm", "pin base image")
    pre_files = ["Dockerfile"]
    pre_blobs = git_ops.file_blobs(repo, pre_files)

    # A sibling PR lands byte-identical content on main, so after the
    # rebase this branch carries no Dockerfile delta any more.
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "the branch's real work")

    ok, dropped, _s = git_ops.check_rebase_diff_integrity(
        repo, "main", pre_files, pre_blobs
    )

    assert ok is True
    assert dropped == []


def test_added_file_landed_upstream_is_excused_without_pre_rebase_blobs(tmp_path):
    """The identical-blob excuse must not hinge on the pre-rebase snapshot.

    Live (robotsix-browser bd94, 2026-08-29): the snapshot reached the guard
    empty, a sibling PR had already landed the branch's new
    ``deploy/docker-compose.yml`` byte-for-byte, and the guard blocked the
    ticket as a silent drop. git's ``ORIG_HEAD`` still holds the pre-rebase
    content, so the guard recovers the blob from there.
    """
    repo, run = _init_repo_with_target(tmp_path)
    branch = run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    (repo / "deploy").mkdir()
    (repo / "deploy" / "docker-compose.yml").write_text("services: {}\n")
    (repo / "README.md").write_text("docs\n")
    run("add", "-A")
    run("commit", "-qm", "add compose + docs")
    # A sibling PR lands the identical compose file on main, on its own
    # lineage (absent at the merge-base), before this branch is rebased.
    run("checkout", "-q", "-b", "sibling", "origin/main")
    (repo / "deploy").mkdir(exist_ok=True)
    (repo / "deploy" / "docker-compose.yml").write_text("services: {}\n")
    run("add", "-A")
    run("commit", "-qm", "sibling adds the same compose")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "-q", branch)
    # The rebase collapses the now-redundant compose delta; README survives.
    run("rebase", "-q", "origin/main")

    ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
        repo, "main", ["deploy/docker-compose.yml", "README.md"], None
    )

    assert (ok, dropped, sibling) == (True, [], [])


def test_modified_file_without_pre_rebase_blobs_stays_conservative(tmp_path):
    """Without a snapshot and without an ``ORIG_HEAD`` rebase marker, a file
    that ends up agreeing with the target gets no excuse — that is exactly
    what a discarded modification looks like."""
    repo, run = _init_repo_with_target(tmp_path)
    # Dockerfile exists at the merge-base (created by _init_repo_with_target
    # fixtures? ensure it exists on main first).
    (repo / "Dockerfile").write_text("FROM base:1.0\n")
    run("add", "-A")
    run("commit", "-qm", "baseline Dockerfile")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "Dockerfile").write_text("FROM base:1.2.3\n")
    run("add", "-A")
    run("commit", "-qm", "pin base image")
    # Rebase "discarded" the change: HEAD back on the target's content.
    run("checkout", "-q", "origin/main", "--", "Dockerfile")
    (repo / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "real work, Dockerfile change lost")

    ok, dropped, _s = git_ops.check_rebase_diff_integrity(
        repo, "main", ["Dockerfile", "keep.py"], None
    )

    assert ok is False
    assert dropped == ["Dockerfile"]


def test_added_file_superseded_by_a_different_target_version_is_excused(tmp_path):
    """robotsix-browser bd94 (2026-08-29): the branch added
    ``deploy/docker-compose.yml``, a sibling PR had landed a DIFFERENT compose
    file on main first, and the rebase agent kept main's. The branch's intent
    (the file exists on the target) is met, the rest of the diff is
    mergeable — blocking as a silent drop dead-ended the ticket."""
    repo, run = _init_repo_with_target(tmp_path)
    branch = run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    (repo / "deploy").mkdir()
    (repo / "deploy" / "docker-compose.yml").write_text("services: {ours: 1}\n")
    (repo / "README.md").write_text("docs\n")
    run("add", "-A")
    run("commit", "-qm", "add compose + docs")
    pre_files = ["deploy/docker-compose.yml", "README.md"]
    pre_blobs = git_ops.file_blobs(repo, pre_files)

    run("checkout", "-q", "-b", "sibling", "origin/main")
    (repo / "deploy").mkdir(exist_ok=True)
    (repo / "deploy" / "docker-compose.yml").write_text("services: {theirs: 2}\n")
    run("add", "-A")
    run("commit", "-qm", "sibling adds its own compose")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "-q", branch)
    # The agent resolves the add/add conflict by taking the target's version
    # (during a rebase the target is the "ours" side).
    import subprocess as _sp

    try:
        run("rebase", "-q", "origin/main")
    except _sp.CalledProcessError:
        run("checkout", "--ours", "deploy/docker-compose.yml")
        run("add", "deploy/docker-compose.yml")
        run("-c", "core.editor=true", "rebase", "--continue")
    assert (
        repo / "deploy" / "docker-compose.yml"
    ).read_text() == "services: {theirs: 2}\n"

    ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
        repo, "main", pre_files, pre_blobs
    )

    assert (ok, dropped, sibling) == (True, [], [])


def test_discarded_change_is_still_reported_as_a_drop(tmp_path):
    """A rebase that threw the branch's work away must still block.

    This is the bug the guard exists to catch, and it produces the same
    "HEAD agrees with target" end state as the healthy case above — only
    the pre-rebase blob distinguishes them. Without this the exemption
    would silently swallow real data loss.
    """
    repo, run = _init_repo_with_target(tmp_path)
    # The branch's intended Dockerfile content, captured pre-rebase.
    (repo / "Dockerfile").write_text("FROM base:1.2.3\n")
    run("add", "-A")
    run("commit", "-qm", "pin base image")
    pre_files = ["Dockerfile"]
    pre_blobs = git_ops.file_blobs(repo, pre_files)

    # The rebase agent resolves a conflict by discarding that change:
    # HEAD reverts to main's ORIGINAL Dockerfile.
    run("reset", "-q", "--hard", "origin/main")
    (repo / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "only the other file survived")

    ok, dropped, _s = git_ops.check_rebase_diff_integrity(
        repo, "main", pre_files, pre_blobs
    )

    assert ok is False
    assert dropped == ["Dockerfile"]


def test_without_blob_snapshot_behaviour_is_unchanged(tmp_path):
    """Omitting the snapshot keeps the original conservative reporting."""
    repo, run = _init_repo_with_target(tmp_path)
    (repo / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "other")

    ok, dropped, _s = git_ops.check_rebase_diff_integrity(repo, "main", ["Dockerfile"])

    assert ok is False
    assert dropped == ["Dockerfile"]


def test_renamed_file_is_not_reported_as_dropped(tmp_path):
    """A file git resolves as a RENAME is still on the branch.

    Rename detection is on by default, so an extract/split refactor whose
    new file resembles a sibling the target has since gained flips from
    `A` to `R` after the rebase. Filtering `R` out of the post-rebase file
    list made the guard report a file the branch plainly carries as
    silently dropped — live on auto-mail 945c, whose
    `_archive_action_mixin.py` scored R096 against the file it was
    extracted from.
    """
    repo, run = _init_repo_with_target(tmp_path)
    body = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(40))
    (repo / "old_mixin.py").write_text(body)
    run("add", "-A")
    run("commit", "-qm", "add the mixin")
    run("update-ref", "refs/remotes/origin/main", "HEAD")

    # The branch renames it (with a small edit, so similarity < 100%).
    run("mv", "old_mixin.py", "new_mixin.py")
    (repo / "new_mixin.py").write_text(body + "\ndef extra():\n    return 99\n")
    run("add", "-A")
    run("commit", "-qm", "extract into its own module")

    assert git_ops._git(repo, "diff", "--name-status", "origin/main...HEAD").startswith(
        "R"
    ), "precondition: git must classify this as a rename"

    post = git_ops.changed_source_files(repo, "main")
    assert "new_mixin.py" in post

    ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
        repo, "main", ["new_mixin.py"], git_ops.file_blobs(repo, ["new_mixin.py"])
    )
    assert ok is True
    assert dropped == []
    assert sibling == []


def test_uv_lock_is_exempt_from_the_drop_guard(tmp_path):
    """uv.lock is re-derived from the whole dependency graph.

    A dependency a sibling PR already landed collapses this branch's lock
    delta to nothing, which the blob-equality excuse cannot clear — mill
    0020 blocked on exactly that, as part 2/3 of a migration whose
    dependency had merged in part 1/3.
    """
    repo, run = _init_repo_with_target(tmp_path)
    (repo / "uv.lock").write_text("version = 1\n")
    run("add", "-A")
    run("commit", "-qm", "lock")

    ok, dropped, sibling = git_ops.check_rebase_diff_integrity(
        repo, "main", ["uv.lock"], {}
    )
    assert ok is True
    assert dropped == []
    assert sibling == []
    assert "uv.lock" in git_ops.DEFAULT_REBASE_DROP_EXEMPT_PATHS

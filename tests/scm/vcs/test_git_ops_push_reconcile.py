import subprocess
from pathlib import Path

import pytest

from robotsix_mill.scm.vcs import git_ops
from robotsix_mill.scm.vcs.git_ops import PostPushResult

from ._git_test_helpers import _git, make_bare_repo

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

    def test_diverged_but_remote_commit_is_mills_github_app_returns_synced(
        self, tmp_path
    ):
        """The mill pushes under TWO identities and both must count as its own.

        Local git commits are ``mill@robotsix.local``, but everything pushed
        through the GitHub App — every ci_fix push — is authored by
        ``<id>+robotsix-mill[bot]@users.noreply.github.com``. Matching only the
        former made the mill hard-block on its own App commits with "a human
        likely pushed" (live on central-deploy #801 / ticket …f472).
        """
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "local.txt").write_text("rebased local work")
        git_ops.commit_all(dest, "local rebase commit")

        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(
            pusher,
            "config",
            "user.email",
            "285582353+robotsix-mill[bot]@users.noreply.github.com",
        )
        _git(pusher, "config", "user.name", "robotsix-mill[bot]")
        _git(pusher, "checkout", "-b", "feature")
        (pusher / "cifix.txt").write_text("mill's own ci_fix push\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "ci fix push via the GitHub App")
        _git(pusher, "push", "origin", "feature")

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.SYNCED

    def test_diverged_mixed_human_and_bot_still_diverged(self, tmp_path):
        """One human commit among automation commits must still bail.

        Broadening to automation identities must not weaken the guard the
        check exists for.
        """
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "repo"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")
        (dest / "local.txt").write_text("rebased local work")
        git_ops.commit_all(dest, "local rebase commit")

        pusher = tmp_path / "pusher"
        subprocess.run(
            ["git", "clone", "-q", remote, str(pusher)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(pusher, "checkout", "-b", "feature")
        _git(
            pusher,
            "config",
            "user.email",
            "285582353+robotsix-mill[bot]@users.noreply.github.com",
        )
        _git(pusher, "config", "user.name", "robotsix-mill[bot]")
        (pusher / "cifix.txt").write_text("bot work\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "bot commit")
        # ...then a real person pushes on top.
        _git(pusher, "config", "user.email", "damien@example.com")
        _git(pusher, "config", "user.name", "Damien")
        (pusher / "human.txt").write_text("hand-written fix\n")
        _git(pusher, "add", "-A")
        _git(pusher, "commit", "-q", "-m", "human commit")
        _git(pusher, "push", "origin", "feature")

        result = git_ops.reconcile_with_remote_pr(dest, remote, "feature", token=None)
        assert result is git_ops.ReconcileResult.DIVERGED

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

        with patch("robotsix_mill.scm.vcs.git_ops._git", side_effect=error):
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

        with patch("robotsix_mill.scm.vcs.git_ops._git", side_effect=error):
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
        with patch("robotsix_mill.scm.vcs.git_ops._git", side_effect=error):
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
        def _git_side_effect(repo, *args, **kwargs):
            if args and args[0] == "rev-parse":
                return "abc1234"  # canned SHA for remote_branch_sha
            raise error

        with patch("robotsix_mill.scm.vcs.git_ops._git", side_effect=_git_side_effect):
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

    @pytest.mark.parametrize(
        ("author_email", "committer_email", "label"),
        [
            (
                "github-actions[bot]@users.noreply.github.com",
                "mill@robotsix.local",
                "repo CI auto-format commit",
            ),
            (
                "285582353+robotsix-mill[bot]@users.noreply.github.com",
                "noreply@github.com",
                "mill's own GitHub App via the API",
            ),
        ],
    )
    def test_automation_authors_are_not_foreign(
        self, tmp_path, author_email, committer_email, label
    ):
        """Bot-authored commits must not read as "a human pushed".

        Regression (2026-07-31): ``_MILL_EMAILS`` held only
        ``mill@robotsix.local`` and required BOTH author and committer to
        match it, so the two identities below — mill's own CI auto-format
        commit and mill's own GitHub App — were reported as foreign. Three
        live tickets were blocked with "a human likely pushed to the PR
        branch. Manual reconciliation required" when no human was involved.
        """
        remote = make_bare_repo(tmp_path)
        dest = tmp_path / "dest"
        git_ops.clone(remote, dest, "main")
        git_ops.create_branch(dest, "feature")

        (dest / "automated.txt").write_text(f"{label}\n")
        _git(dest, "add", ".")
        _git(
            dest,
            "-c",
            f"user.email={author_email}",
            "-c",
            "user.name=Automation",
            "commit",
            "-q",
            "-m",
            label,
            env={
                "GIT_COMMITTER_EMAIL": committer_email,
                "GIT_COMMITTER_NAME": "Automation",
            },
        )
        git_ops.push(dest, "feature", remote, token=None)

        result = git_ops.post_push_check(dest, "feature", "main", remote, token=None)
        assert result == PostPushResult.PASS

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


# ---------------------------------------------------------------------------
# classify_push_error — auth vs generic failure classification
# ---------------------------------------------------------------------------


class TestClassifyPushError:
    """``classify_push_error`` distinguishes auth failures from generic errors."""

    def test_auth_invalid_username_or_token(self):
        assert (
            git_ops.classify_push_error(
                "remote: Invalid username or token.\n"
                "fatal: Authentication failed for 'https://github.com/o/r.git/'"
            )
            == "auth"
        )

    def test_auth_password_not_supported(self):
        assert (
            git_ops.classify_push_error(
                "remote: Password authentication is not supported for Git operations.\n"
                "fatal: Authentication failed"
            )
            == "auth"
        )

    def test_auth_401(self):
        assert (
            git_ops.classify_push_error("The requested URL returned error: 401")
            == "auth"
        )

    def test_auth_403(self):
        assert (
            git_ops.classify_push_error("The requested URL returned error: 403")
            == "auth"
        )

    def test_auth_repository_not_found(self):
        assert (
            git_ops.classify_push_error(
                "remote: Repository not found.\nfatal: repository 'https://...' not found"
            )
            == "auth"
        )

    def test_auth_fatal_authentication_failed(self):
        assert (
            git_ops.classify_push_error(
                "fatal: Authentication failed for 'https://github.com/o/r.git/'"
            )
            == "auth"
        )

    def test_auth_could_not_read_from_remote(self):
        assert (
            git_ops.classify_push_error(
                "fatal: Could not read from remote repository.\n"
                "Please make sure you have the correct access rights"
            )
            == "auth"
        )

    def test_generic_network_error(self):
        assert (
            git_ops.classify_push_error(
                "fatal: unable to access 'https://github.com/o/r.git/': "
                "Could not resolve host: github.com"
            )
            == "generic"
        )

    def test_generic_empty_stderr(self):
        assert git_ops.classify_push_error("") == "generic"

    def test_case_insensitive(self):
        assert (
            git_ops.classify_push_error("REMOTE: INVALID USERNAME OR TOKEN.") == "auth"
        )


# ---------------------------------------------------------------------------
# check_push_access — health-endpoint probe
# ---------------------------------------------------------------------------


class TestCheckPushAccess:
    """``check_push_access`` runs ``git ls-remote`` and returns a structured dict."""

    def test_ok_with_bare_repo(self, tmp_path):
        """Against a local bare repo (no auth), the probe succeeds."""
        from ._git_test_helpers import make_bare_repo

        url = make_bare_repo(tmp_path)
        result = git_ops.check_push_access(url, token=None)
        assert result["ok"] is True
        assert result["status"] == "ok"
        assert isinstance(result["latency_ms"], int)
        assert result["detail"] is not None  # SHA string

    def test_unreachable_remote(self):
        """An unreachable host returns error, not auth_error."""
        result = git_ops.check_push_access("https://127.0.0.1:9/none.git", token=None)
        assert result["ok"] is False
        assert result["status"] == "error"

    def test_auth_error_classification(self, monkeypatch):
        """When git ls-remote exits non-zero with an auth message,
        the result status is ``auth_error``."""
        import subprocess as sp_mod

        fake = sp_mod.CompletedProcess(
            args=["git", "ls-remote", "https://oauth2:bad@github.com/o/r.git", "HEAD"],
            returncode=128,
            stdout="",
            stderr="remote: Invalid username or token.\n"
            "fatal: Authentication failed for 'https://github.com/o/r.git/'\n",
        )

        monkeypatch.setattr(sp_mod, "run", lambda *a, **kw: fake)
        result = git_ops.check_push_access("https://github.com/o/r.git", token="bad")
        assert result["ok"] is False
        assert result["status"] == "auth_error"

    def test_with_token_in_remote_url(self, tmp_path):
        """Token passed alongside a bare repo URL still works."""
        from ._git_test_helpers import make_bare_repo

        url = make_bare_repo(tmp_path)
        result = git_ops.check_push_access(url, token="ghs_fake")
        assert result["ok"] is True
        assert result["status"] == "ok"

    def test_subprocess_exception_returns_error(self, monkeypatch):
        """When subprocess.run itself raises (not just non-zero exit),
        the result is ``error``."""
        import subprocess as sp_mod

        def _raise(*a, **kw):
            raise OSError("no git binary")

        monkeypatch.setattr(sp_mod, "run", _raise)
        result = git_ops.check_push_access("https://github.com/o/r.git")
        assert result["ok"] is False
        assert result["status"] == "error"
        assert "no git binary" in str(result["detail"])


# --- network git calls must be bounded and non-interactive ---------------


def test_network_git_calls_pass_a_timeout(tmp_path, monkeypatch):
    """fetch/push must hand subprocess.run a timeout.

    Without one a stalled connection hangs the calling thread forever.
    Because every stage offloads blocking work to a shared, small thread
    pool, each hung git permanently removes one worker from that pool —
    observed live as the entire pool wedged in the periodic repo refresh
    while merge stages sat "active" for 10+ minutes doing nothing.
    """
    seen: list[dict] = []

    def _fake_run(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", _fake_run)

    git_ops.fetch(tmp_path, remote_url="https://x/y.git", token="t", branch="main")
    git_ops.push(tmp_path, "main", "https://x/y.git", "t")

    assert seen, "no git subprocess was invoked"
    for kw in seen:
        assert kw.get("timeout") == git_ops.NETWORK_GIT_TIMEOUT

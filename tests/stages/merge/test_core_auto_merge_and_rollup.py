"""Merge-stage core tests: git merge verification, stale-artifact and
auto-merge paths, green-but-unpromotable ceiling, no-PR spin guard and
empty-rollup self-heal."""

import json
import subprocess

from robotsix_mill.vcs.git_ops import PostPushResult, ReconcileResult

from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage
from tests.stages.merge.conftest import (
    _gh,
    _human_mr_approval,
    _implement_complete,
    _in_rebasing,
    _write_review_artifact,
)

# ============================================================
# G. Git merge verification tests (new)
# ============================================================


def _git(repo, *args):
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_repo_with_origin(tmp_path):
    """Build a work repo with an ``origin/main`` remote-tracking ref.

    Creates a bare repo used as ``origin``, a work repo with an initial
    commit on ``main`` pushed to it, and fetches so ``origin/main``
    resolves locally.  Returns the work-repo ``Path``.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit on main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    return repo


def _waiting_auto_merge_ticket(ctx, *, sha="abc1234"):
    """Create a ticket in WAITING_AUTO_MERGE with auto-merge eligibility.

    Returns the ticket and its branch name.
    """
    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)
    branch = f"mill/{t.id}"
    return t, branch


def test_waiting_auto_merge_verify_ancestor_confirmed_goes_to_done(
    tmp_path, monkeypatch
):
    """Feature branch tip is an ancestor of origin/main → verify passes → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    # Set up a real git repo with a feature branch merged into main.
    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "push", "origin", branch)
    # Merge the feature into main and push.
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", branch, "-m", "merge feature")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Monkeypatch _workspace_repo_dir to return our real repo.
    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    # Monkeypatch the forge: PR already merged.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_verify_squash_merge_goes_to_done(tmp_path, monkeypatch):
    """Feature tip NOT an ancestor of main, but a commit on main references
    the ticket ID → squash-merge fallback → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    # Create the ticket first so we have its id for the commit message.
    t = _human_mr_approval(ctx)

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    # Do NOT merge the branch into main (so it's not an ancestor).
    # Instead, create a squash-style commit on main that references the ticket.
    _git(repo, "checkout", "main")
    (repo / "other.txt").write_text("squash of feature\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", f"squash merge of {t.id}")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_verify_fails_goes_to_implement_complete(
    tmp_path, monkeypatch
):
    """Feature branch tip is NOT an ancestor of main AND no squash-merge
    evidence → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("unmerged work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    # Do NOT merge into main.
    _git(repo, "checkout", "main")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "merge not confirmed" in out.note


def test_waiting_auto_merge_merge_pr_success_verify_fails_goes_to_implement_complete(
    tmp_path, monkeypatch
):
    """merge_pr returns {'merged': True} but the feature-tip is not an
    ancestor of main and no squash-merge evidence → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("unmerged work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "checkout", "main")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    # Path B: CI is green, eligibility holds, merge_pr returns success.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": feature_tip,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "merge not confirmed" in out.note


def test_cross_repo_merge_routes_to_upstream_pr(tmp_path, monkeypatch):
    """A repo with a cross_repo_target merges/polls the UPSTREAM PR:
    the forge resolved for merge_pr targets the upstream owner/repo,
    not the clone remote."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    # Replace the ctx repo_config with one carrying a cross_repo_target.
    rc = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        forge_remote_url="https://github.com/fork-owner/r.git",
        auto_merge_enabled=True,
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork-owner/r.git",
        ),
    )
    ctx = StageContext(settings=ctx.settings, service=ctx.service, repo_config=rc)

    seen = {}
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://github.com/up/r/pull/1",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    def fake_merge(self, *, source_branch):
        seen["owner_repo"] = self._owner_repo
        return {"merged": True, "reason": "merged"}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", fake_merge)

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    # The merge targeted the UPSTREAM repo, not the fork clone remote.
    assert seen["owner_repo"] == ("up", "r")


def test_waiting_auto_merge_no_repo_proceeds_to_done(tmp_path, monkeypatch):
    """No git repo in workspace → _verify_merge_ancestor returns True
    (best-effort) → DONE as before."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "abc1234",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


# ============================================================
# Stale review artifact (head_sha mismatch) — regression tests
# for merge-gate-replays-stale-request-changes
# ============================================================


def test_auto_merge_not_blocked_by_stale_artifact_head_sha(tmp_path, monkeypatch):
    """When review.md has a different head_sha than the current PR head,
    the stale verdict must not block auto-merge (eligible=True)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "current-head-abc123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    # Write an artifact with a DIFFERENT head_sha than the PR reports.
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="old-stale-head-xyz789",
    )

    out = MergeStage().run(t, ctx)
    # Despite auto_merge_eligible: false in artifact, the stale head_sha
    # mismatch makes it eligible → auto-merge to DONE.
    assert out.next_state is State.DONE


def test_auto_merge_not_blocked_by_current_artifact_same_head_sha(
    tmp_path,
    monkeypatch,
):
    """When review.md has the SAME head_sha as the current PR, the
    verdict is current but no longer blocks — auto-merge fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "same-head-123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="same-head-123",
    )

    out = MergeStage().run(t, ctx)
    # Artifact check removed — auto-merge proceeds.
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_without_head_sha_in_artifact_is_backward_compat(
    tmp_path,
    monkeypatch,
):
    """When review.md has NO head_sha line (legacy pre-d42c artifact),
    the missing SHA is treated as stale — the verdict cannot be trusted
    and the PR auto-merges through to DONE instead of blocking."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "any-head",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # No head_sha line — legacy artifact, treated as stale.
    _write_review_artifact(ctx, t, verdict="REQUEST_CHANGES", eligible=False)

    out = MergeStage().run(t, ctx)
    # Legacy artifacts without head_sha are stale → auto-merge to DONE.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_stale_artifact_does_not_bounce(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll with a stale artifact (different head_sha)
    must not bounce back to HUMAN_MR_APPROVAL — the ticket proceeds to
    auto-merge when CI is green."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "rebased-head-def456",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # Artifact from BEFORE the rebase — different head_sha.
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="pre-rebase-head-abc111",
    )
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Should not bounce — stale artifact is ignored, proceeds to auto-merge.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_legacy_artifact_no_head_sha_does_not_bounce(
    tmp_path,
    monkeypatch,
):
    """WAITING_AUTO_MERGE poll with a legacy artifact that has NO head_sha
    line (pre-d42c cache) must not bounce back to HUMAN_MR_APPROVAL —
    the missing SHA means the verdict is untrusted and the ticket
    proceeds to auto-merge when CI is green."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "post-push-head-456",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # Legacy artifact — no head_sha line at all.
    _write_review_artifact(ctx, t, verdict="REQUEST_CHANGES", eligible=False)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Should not bounce — legacy artifact treated as stale, proceeds to auto-merge.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_current_artifact_no_longer_bounces(
    tmp_path,
    monkeypatch,
):
    """WAITING_AUTO_MERGE poll with a CURRENT artifact (same head_sha)
    that is not eligible — artifact check removed, stays WAITING_AUTO_MERGE
    when CI is pending."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "current-head-999",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="current-head-999",
    )
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Artifact check removed — stays in WAITING_AUTO_MERGE (CI pending).
    assert out.next_state is State.WAITING_AUTO_MERGE


def test_stale_artifact_no_longer_blocks_auto_merge(tmp_path, monkeypatch):
    """When a stale review artifact exists (head_sha mismatch), auto-merge
    still fires — the artifact check has been removed entirely."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "rebased-head-abc",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    ws = ctx.service.workspace(t)

    # Pre-populate a review stage cache entry — simulates a cached
    # REQUEST_CHANGES outcome from before the rebase.

    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    cache_path = ws.artifacts_dir / "stage_cache.json"
    cache_path.write_text(
        json.dumps(
            {"review": {"input_hash": "old-hash", "next_state": "ready", "note": ""}}
        ),
        encoding="utf-8",
    )

    # Write a review artifact with a DIFFERENT head_sha (stale).
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="old-stale-sha-xyz",
    )

    out = MergeStage().run(t, ctx)
    # Artifact check removed — auto-merge to DONE.
    assert out.next_state is State.DONE


def test_stale_changes_requested_dismissed_regardless_of_feedback_flag(
    tmp_path, monkeypatch
):
    """Regression: a stale CHANGES_REQUESTED forge review against an old
    commit is dismissed on the forge and does NOT prevent auto-merge,
    even when review_feedback_enabled is False.

    Scenario (from ticket b3fb / PR #2446):
    - MR is approved (review.md with auto_merge_eligible: true)
    - A new commit is pushed, superseding an earlier CHANGES_REQUESTED review
    - CI is green on the new head
    - The merge gate must not bounce back to human_mr_approval

    This test runs with review_feedback_enabled=False to prove the
    stale-review dismissal path works independently of the feedback gate.
    """
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    # review_feedback_enabled is NOT set — defaults to False.

    dismissed_ids: list[int] = []

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
            "sha": "new-head-abc222",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "please fix", "path": "a.py", "line": 1}],
            "files": ["a.py"],
            "commit_id": "old-head-abc111",
            "review_id": 42,
        },
    )

    def _dismiss(self, *, source_branch, review_id):
        dismissed_ids.append(review_id)
        return True

    monkeypatch.setattr(github.GitHubForge, "dismiss_review", _dismiss)
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Must auto-merge → DONE; must NOT bounce back to human_mr_approval.
    assert out.next_state is State.DONE
    # The stale review must have been dismissed.
    assert dismissed_ids == [42]


def test_stale_changes_requested_dismissed_with_feedback_enabled(tmp_path, monkeypatch):
    """Same scenario as above but with review_feedback_enabled=True.
    The stale review is still dismissed and auto-merge proceeds."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )

    dismissed_ids: list[int] = []

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
            "sha": "new-head-abc222",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "please fix", "path": "a.py", "line": 1}],
            "files": ["a.py"],
            "commit_id": "old-head-abc111",
            "review_id": 42,
        },
    )

    def _dismiss(self, *, source_branch, review_id):
        dismissed_ids.append(review_id)
        return True

    monkeypatch.setattr(github.GitHubForge, "dismiss_review", _dismiss)
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Must auto-merge → DONE.
    assert out.next_state is State.DONE
    # The stale review must have been dismissed.
    assert dismissed_ids == [42]


# ============================================================
# Green-but-unpromotable ceiling (renamed required check)
# ============================================================


def _green_unpromotable_ctx(tmp_path, monkeypatch, **extra):
    """Forge stubs for "every reported check passed, forge still won't promote"."""
    ctx = _gh(tmp_path, **extra)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://forge/pr/1",
            "mergeable": True,
            # Protection is waiting on a context nothing reports, so the PR
            # never leaves "blocked" no matter how long the mill polls.
            "mergeable_state": "blocked",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [{"name": "Lint", "conclusion": "success"}],
        },
    )
    return ctx


def test_green_unpromotable_re_polls_below_the_ceiling(tmp_path, monkeypatch):
    """Settling is normal: the first polls must keep waiting, not block."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_green_unpromotable_blocks_at_the_ceiling_naming_the_missing_context(
    tmp_path, monkeypatch
):
    """Green CI + a permanently unpromotable PR escalates with an actionable note."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(
        github.GitHubForge,
        "required_status_contexts",
        lambda self, *, target_branch: ["lint", "typecheck"],
    )
    t = _implement_complete(ctx)
    stage = MergeStage()
    outs = [stage.run(t, ctx) for _ in range(ctx.settings.green_unpromotable_max_polls)]

    assert [o.next_state for o in outs[:-1]] == [State.IMPLEMENT_COMPLETE] * (
        len(outs) - 1
    )
    final = outs[-1]
    assert final.next_state is State.BLOCKED
    # The two required contexts the PR never reports must be named; the one
    # it does report ("Lint") must not be.
    assert "'lint'" in final.note
    assert "'typecheck'" in final.note
    assert "https://forge/pr/1" in final.note


def test_green_unpromotable_falls_back_when_protection_is_unreadable(
    tmp_path, monkeypatch
):
    """No protection data (403/unprotected) still blocks, with a vaguer note."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(
        github.GitHubForge,
        "required_status_contexts",
        lambda self, *, target_branch: [],
    )
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        stage.run(t, ctx)
    final = stage.run(t, ctx)
    assert final.next_state is State.BLOCKED
    assert "mergeable_state='blocked'" in final.note


def test_green_unpromotable_counter_resets_while_a_check_is_pending(
    tmp_path, monkeypatch
):
    """A pending check is genuine settling and must not consume the budget."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE

    # One poll where CI is still running clears the accumulated count...
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
            "pending": ["Test"],
            "jobs": [],
        },
    )
    assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE

    # ...so the next green-but-stuck poll starts the budget over.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [{"name": "Lint", "conclusion": "success"}],
        },
    )
    assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE


def test_green_unpromotable_guard_disabled_by_zero(tmp_path, monkeypatch):
    """green_unpromotable_max_polls=0 restores the old unbounded re-poll."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch, green_unpromotable_max_polls=0)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(12):
        assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE


# ============================================================
# Consecutive no-PR spin guard (merge_pr_missing_max_polls)
# ============================================================


def _pr_missing_ctx(tmp_path, monkeypatch, **extra):
    """Forge stubs for 'no PR found for the ticket's branch'."""
    ctx = _gh(tmp_path, **extra)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )
    return ctx


def test_pr_missing_re_polls_below_the_ceiling(tmp_path, monkeypatch):
    """A missing PR is normally transient (CI still provisioning) — the
    first polls must keep re-polling, not block."""
    ctx = _pr_missing_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.merge_pr_missing_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_pr_missing_blocks_at_the_ceiling_with_specific_note(tmp_path, monkeypatch):
    """A permanently-missing PR (e.g. cross-repo deliver polled against the
    board's own repo) escalates to BLOCKED with an actionable note instead
    of silently spinning on the same dead lookup."""
    ctx = _pr_missing_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    outs = [stage.run(t, ctx) for _ in range(ctx.settings.merge_pr_missing_max_polls)]
    assert [o.next_state for o in outs[:-1]] == [State.IMPLEMENT_COMPLETE] * (
        len(outs) - 1
    )
    final = outs[-1]
    assert final.next_state is State.BLOCKED
    assert "no PR found" in final.note
    assert "manual intervention" in final.note


def test_pr_missing_counter_resets_when_pr_appears(tmp_path, monkeypatch):
    """A PR that appears resets the streak — the next missing run starts the
    budget over from scratch."""
    ctx = _pr_missing_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.merge_pr_missing_max_polls - 1):
        stage.run(t, ctx)

    # PR appears (green + mergeable) → promotes and resets the counter.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    assert stage.run(t, ctx).next_state is State.HUMAN_MR_APPROVAL
    # run() only returns the outcome; the worker applies the transition.
    # Simulate it so the next poll dispatches from HUMAN_MR_APPROVAL.
    ctx.service.transition(t.id, State.HUMAN_MR_APPROVAL, note="gates passed")
    t = ctx.service.get(t.id)

    # PR disappears again — budget restarted, so still not blocked yet.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )
    for _ in range(ctx.settings.merge_pr_missing_max_polls - 1):
        assert stage.run(t, ctx).next_state is State.HUMAN_MR_APPROVAL
    assert stage.run(t, ctx).next_state is State.BLOCKED


def test_pr_missing_guard_disabled_by_zero(tmp_path, monkeypatch):
    """merge_pr_missing_max_polls=0 restores the old unbounded re-poll."""
    ctx = _pr_missing_ctx(tmp_path, monkeypatch, merge_pr_missing_max_polls=0)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(12):
        assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE


# ============================================================
# Empty-rollup self-heal (close/reopen PR)
# ============================================================


def _empty_rollup_ctx(tmp_path, monkeypatch, **extra):
    """Forge stubs for 'CI success with zero check runs, mergeable_state blocked'."""
    ctx = _gh(tmp_path, **extra)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://forge/pr/1",
            "mergeable": True,
            "mergeable_state": "blocked",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [],  # empty rollup — no CI triggered
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "required_status_contexts",
        lambda self, *, target_branch: [],
    )
    return ctx


def test_empty_rollup_re_polls_below_threshold(tmp_path, monkeypatch):
    """Empty rollup keeps re-polling until the threshold is reached."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_empty_rollup_self_heal_closes_and_reopens_pr(tmp_path, monkeypatch):
    """At the threshold, the mill closes and reopens the PR once."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Poll up to threshold - 1.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        stage.run(t, ctx)

    # Track forge calls.
    closed_branches: list[str] = []
    reopened_branches: list[str] = []
    comments: list[tuple[str, str]] = []

    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: closed_branches.append(source_branch) or True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: reopened_branches.append(source_branch) or True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "post_pr_comment",
        lambda self, *, source_branch, body: (
            comments.append((source_branch, body)) or True
        ),
    )

    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert (
        "close/reopen" in out.note.lower() or "closed and reopened" in out.note.lower()
    )
    assert len(closed_branches) == 1
    assert len(reopened_branches) == 1
    assert len(comments) == 1
    assert "pull_request" in comments[0][1] or "CI" in comments[0][1]
    notes = [e.note or "" for e in ctx.service.history(t.id)]
    assert any("empty CI rollup" in n and "closed and reopened" in n for n in notes)


def test_empty_rollup_parks_after_failed_self_heal(tmp_path, monkeypatch):
    """If the rollup is still empty after self-heal, park BLOCKED with a specific note."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Poll up to threshold - 1.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        stage.run(t, ctx)

    # Self-heal succeeds (close + reopen).
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "post_pr_comment",
        lambda self, *, source_branch, body: True,
    )
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # Now poll again up to threshold — the rollup is still empty.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE

    # Next poll should park BLOCKED with the self-heal note.
    out = stage.run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert (
        "close/reopen" in out.note.lower() or "closed and reopened" in out.note.lower()
    )
    assert "already attempted" in out.note.lower()


def test_empty_rollup_self_heal_disabled_by_zero(tmp_path, monkeypatch):
    """empty_rollup_max_polls=0 disables the self-heal entirely."""
    ctx = _empty_rollup_ctx(
        tmp_path, monkeypatch, empty_rollup_max_polls=0, green_unpromotable_max_polls=0
    )
    t = _implement_complete(ctx)
    stage = MergeStage()
    # Should keep re-polling without attempting self-heal.
    for _ in range(12):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_empty_rollup_resets_when_jobs_appear(tmp_path, monkeypatch):
    """If jobs appear (CI triggered), the empty-rollup counter resets."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Poll up to threshold - 1.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        stage.run(t, ctx)

    # Now CI triggers — jobs appear.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
            "pending": ["ci / tests"],
            "jobs": [{"name": "ci / tests", "conclusion": None}],
        },
    )
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # Switch back to empty rollup — counter should have reset, so we get
    # a full budget of polls before self-heal.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [],
        },
    )
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_empty_rollup_self_heal_suppressed_within_rebase_cooldown(
    tmp_path, monkeypatch
):
    """A fresh rebase push shows a transient empty rollup (its CI has not fired
    yet).  The close/reopen self-heal must NOT fire in the rebase-cooldown
    window — closing the ticket's own PR (and failing to reopen it) is how
    rebased PRs were orphaned as "PR closed without merge"."""
    import datetime

    from robotsix_mill.stages.merge._shared import _REBASE_LAST_TS

    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)

    # Simulate what _handle_rebase_success records right after a rebase
    # force-push: a recent rebase timestamp.
    artifacts_dir = ctx.service.workspace(t).artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / _REBASE_LAST_TS).write_text(
        datetime.datetime.now(datetime.UTC).isoformat(), encoding="utf-8"
    )

    closed_branches: list[str] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: closed_branches.append(source_branch) or True,
    )

    stage = MergeStage()
    # Poll well past the self-heal threshold — the PR must stay open.
    for _ in range(ctx.settings.empty_rollup_max_polls + 3):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
    assert closed_branches == []


def test_rebase_on_conflict_does_not_close_own_pr(tmp_path, monkeypatch):
    """Acceptance: a conflicting PR goes rebase → IMPLEMENT_COMPLETE and the
    merge stage never closes the ticket's own PR, even once the freshly-pushed
    head reports a transient empty CI rollup."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **kwargs: RebaseResult(
            status="DONE", summary="resolved docs/modules.yaml"
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.file_blobs", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.try_mechanical_rebase",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    close_calls: list[str] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: close_calls.append(source_branch) or True,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    stage = MergeStage()
    # Rebase poll: conflict resolved → back to IMPLEMENT_COMPLETE, same PR.
    # The success path records the rebase timestamp (_REBASE_LAST_TS).
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # Apply the transition the runtime would make and keep polling.
    ctx.service.transition(t.id, State.IMPLEMENT_COMPLETE)
    t = ctx.service.get(t.id)

    # Subsequent IMPLEMENT_COMPLETE polls see the fresh head's empty rollup —
    # the self-heal stays suppressed; the PR is never closed.
    for _ in range(ctx.settings.empty_rollup_max_polls + 3):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
    assert close_calls == []


def test_empty_rollup_close_fails_parks_with_explicit_note(tmp_path, monkeypatch):
    """close_pr failing parks BLOCKED with a note naming the failed attempt."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Poll up to threshold - 1.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        stage.run(t, ctx)

    # close_pr fails.
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: False,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "post_pr_comment",
        lambda self, *, source_branch, body: True,
    )

    # The marker is written before the forge calls, so the "at most one
    # self-heal per PR" guard holds even when the attempt fails: park BLOCKED
    # now with the attempt details instead of retrying on later polls.
    out = stage.run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "closed=False" in out.note
    assert "reopened=False" in out.note


def test_empty_rollup_reopen_fails_parks_with_explicit_note(tmp_path, monkeypatch):
    """close_pr succeeding but reopen_pr failing parks BLOCKED and warns the PR
    may have been left closed."""
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Poll up to threshold - 1.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        stage.run(t, ctx)

    # close succeeds, reopen fails.
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: False,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "post_pr_comment",
        lambda self, *, source_branch, body: True,
    )

    out = stage.run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "closed=True" in out.note
    assert "reopened=False" in out.note
    assert "left closed" in out.note.lower()


def test_empty_rollup_polls_do_not_consume_green_unpromotable_budget(
    tmp_path, monkeypatch
):
    """Empty-rollup polls below the self-heal threshold must not consume the
    green-unpromotable budget — otherwise a small
    green_unpromotable_max_polls parks the ticket with the generic 'no check
    outstanding' note before the self-heal can trigger."""
    ctx = _empty_rollup_ctx(
        tmp_path,
        monkeypatch,
        empty_rollup_max_polls=3,
        green_unpromotable_max_polls=1,
    )
    t = _implement_complete(ctx)
    stage = MergeStage()

    # Track forge calls: the self-heal must fire at the empty-rollup
    # threshold, not be preempted by the green-unpromotable ceiling.
    closed_branches: list[str] = []
    reopened_branches: list[str] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: closed_branches.append(source_branch) or True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: reopened_branches.append(source_branch) or True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "post_pr_comment",
        lambda self, *, source_branch, body: True,
    )

    # Poll below the threshold: each must re-poll (IMPLEMENT_COMPLETE), never
    # trip the green-unpromotable ceiling with the generic note.
    for _ in range(ctx.settings.empty_rollup_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE

    # At the threshold, the self-heal fires and re-polls.
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert len(closed_branches) == 1
    assert len(reopened_branches) == 1
    assert (
        "close/reopen" in out.note.lower() or "closed and reopened" in out.note.lower()
    )


# ============================================================
# Empty rollup: action_required workflow-run approval
# ============================================================


def _action_required_ctx(tmp_path, monkeypatch, *, runs, approve=None, **extra):
    """Empty-rollup blocked ctx whose head carries action_required workflow runs.

    ``check_status`` reports a resolvable ``_sha`` (empty rollup, zero jobs)
    and ``list_workflow_runs`` returns *runs* — some or all parked with
    ``conclusion='action_required'``.  *approve* stubs
    ``forge.approve_workflow`` when given.
    """
    ctx = _empty_rollup_ctx(tmp_path, monkeypatch, **extra)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [],  # empty rollup — no check runs registered
            "_sha": "abcdef1234567890",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: runs,
    )
    if approve is not None:
        monkeypatch.setattr(github.GitHubForge, "approve_workflow", approve)
    return ctx


def test_empty_rollup_action_required_runs_get_approved(tmp_path, monkeypatch):
    """Empty rollup + mergeable_state=blocked whose head runs carry
    conclusion='action_required' → approve each and re-poll, instead of
    concluding the pull_request event never fired / closing the PR."""
    runs = [
        {
            "id": 11,
            "name": "ci / tests",
            "conclusion": "action_required",
            "html_url": "https://github.com/o/r/actions/runs/11",
        },
        {
            "id": 12,
            "name": "ci / lint",
            "conclusion": "action_required",
            "html_url": "https://github.com/o/r/actions/runs/12",
        },
    ]
    approved: list[int] = []
    ctx = _action_required_ctx(
        tmp_path,
        monkeypatch,
        runs=runs,
        approve=lambda self, *, run_id: approved.append(run_id) or {"approved": True},
    )
    t = _implement_complete(ctx)
    stage = MergeStage()

    closed_branches: list[str] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "close_pr",
        lambda self, *, source_branch: closed_branches.append(source_branch) or True,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "reopen_pr",
        lambda self, *, source_branch: True,
    )

    # Approve on the FIRST poll — no need to wait for the self-heal threshold.
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert sorted(approved) == [11, 12], "every action_required run is approved"
    assert closed_branches == [], (
        "action_required approval must precede any close/reopen self-heal"
    )
    assert "action_required" in out.note.lower()


def test_empty_rollup_action_required_approval_refused_403_blocks(
    tmp_path, monkeypatch
):
    """A refused (403) approval parks BLOCKED naming action_required and the URLs."""
    runs = [
        {
            "id": 11,
            "name": "ci / tests",
            "conclusion": "action_required",
            "html_url": "https://github.com/o/r/actions/runs/11",
        },
    ]
    ctx = _action_required_ctx(
        tmp_path,
        monkeypatch,
        runs=runs,
        approve=lambda self, *, run_id: {
            "approved": False,
            "forbidden": True,
            "reason": "Client error '403 Forbidden' for url "
            "'.../actions/runs/11/approve'",
        },
    )
    t = _implement_complete(ctx)
    stage = MergeStage()

    out = stage.run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "action_required" in out.note
    assert "https://github.com/o/r/actions/runs/11" in out.note
    assert "403" in out.note

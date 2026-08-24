from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    repo_auto_merge_enabled = env.pop("repo_auto_merge_enabled", False)
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        import robotsix_mill.config as _cfg
        from robotsix_mill.config import Secrets, _reset_secrets

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    db.init_db(s, board_id="test-board")
    from robotsix_mill.config import RepoConfig

    return StageContext(
        settings=s,
        service=TicketService(s, board_id="test-board"),
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            auto_merge_enabled=repo_auto_merge_enabled,
        ),
    )


def _human_mr_approval(ctx):
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _implement_complete(ctx):
    """Create a ticket in IMPLEMENT_COMPLETE state (PR open, gates not verified)."""
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _in_rebasing(ctx):
    """Create a ticket already in REBASING state."""
    t = _implement_complete(ctx)
    ctx.service.transition(t.id, State.REBASING, note="PR conflicting")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    # When auto_merge_enabled is set to "true", also opt in the repo.
    if (
        extra.get("auto_merge_enabled") == "true"
        and "repo_auto_merge_enabled" not in extra
    ):
        extra["repo_auto_merge_enabled"] = True
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
    )


# ============================================================
# E. Auto-merge gate (new)
# ============================================================


def _write_review_artifact(
    ctx, ticket, *, verdict="APPROVE", eligible=True, comment="", head_sha=None
):
    """Helper: write a review.md artifact for auto-merge tests."""
    art_dir = ctx.service.workspace(ticket).artifacts_dir
    art_dir.mkdir(parents=True, exist_ok=True)
    text = f"verdict: {verdict}\nauto_merge_eligible: {str(eligible).lower()}\n"
    if head_sha is not None:
        text += f"head_sha: {head_sha}\n"
    if comment:
        text += f"comment: {comment}\n"
    (art_dir / "review.md").write_text(text, encoding="utf-8")


def test_auto_merge_fires_when_all_conditions_met(tmp_path, monkeypatch):
    """Mergeable + CI success + auto_merge_enabled + review_enabled +
    artifact auto_merge_eligible: true + merge_pr returns merged → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
            "author": "mill-bot",
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
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "auto-merged" in out.note


def test_auto_merge_skipped_when_flag_disabled(tmp_path, monkeypatch):
    """auto_merge_enabled=False → HUMAN_MR_APPROVAL (standard no-op)."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "author": "mill-bot",
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
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


def test_auto_merge_skipped_when_review_disabled(tmp_path, monkeypatch):
    """review_enabled=False → HUMAN_MR_APPROVAL even when auto_merge_enabled=True
    and artifact says eligible."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="false")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


def test_auto_merge_skipped_when_no_review_artifact(tmp_path, monkeypatch):
    """No review artifact — auto-merge still fires (artifact gate removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    # NO review artifact written

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_fires_regardless_of_artifact_verdict(tmp_path, monkeypatch):
    """Artifact auto_merge_eligible: false no longer blocks — the upstream
    human_mr_approval gate is the authoritative review decision."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_skipped_when_ci_pending(tmp_path, monkeypatch):
    """CI conclusion is 'pending', not 'success' → WAITING_AUTO_MERGE
    (auto-merge gate entered, waiting for CI to go green)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert merge_called == []  # merge_pr not called for pending CI


def test_auto_merge_skipped_when_ci_failure(tmp_path, monkeypatch):
    """CI conclusion is 'failure' → IMPLEMENT_COMPLETE (silent fallback)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
        lambda self, *, source_branch: {"conclusion": "failure", "failing": []},
    )

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert merge_called == []


def test_auto_merge_skipped_when_not_mergeable(tmp_path, monkeypatch):
    """mergeable=False + update_branch fails → REBASING (merge_pr never called)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert merge_called == []


def test_merge_pr_failure_blocks_on_forge_rejection(tmp_path, monkeypatch):
    """merge_pr returns {'merged': False} → BLOCKED with forge error in history."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {
            "merged": False,
            "reason": "branch protection",
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


def test_auto_merge_writes_merge_artifact(tmp_path, monkeypatch):
    """On success, merge.md is written with the PR URL."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/42",
            "mergeable": True,
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

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    merge_artifact = ctx.service.workspace(t).artifacts_dir / "merge.md"
    assert merge_artifact.exists()
    content = merge_artifact.read_text(encoding="utf-8")
    assert "auto-merged: https://gh/o/r/pull/42" in content


# ============================================================
# F. WAITING_AUTO_MERGE — updated for IMPLEMENT_COMPLETE fallback
# ============================================================


def test_eligible_pending_ci_goes_to_waiting_auto_merge(tmp_path, monkeypatch):
    """Eligible + CI pending → WAITING_AUTO_MERGE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert merge_called == []  # merge_pr never called


def test_eligible_success_auto_merges_to_done(tmp_path, monkeypatch):
    """Eligible + CI success → DONE (already covered, ensure it still passes)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_eligible_forge_merge_failed_blocks_with_comment(tmp_path, monkeypatch):
    """Eligible + CI success + forge rejects → BLOCKED with forge error in history."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {
            "merged": False,
            "reason": "branch protection",
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "forge merge rejected: branch protection" in (merge_events[0].note or "")


def test_not_eligible_disabled_flag_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """auto_merge_enabled=false → HUMAN_MR_APPROVAL + comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "author": "mill-bot",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "auto-merge disabled in global config" in (merge_events[0].note or "")


def test_not_eligible_review_disabled_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """review_enabled=false → HUMAN_MR_APPROVAL + comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="false")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "review gate disabled" in (merge_events[0].note or "")


def test_no_review_artifact_auto_merges_when_eligible(tmp_path, monkeypatch):
    """No review artifact — auto-merge still fires (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True},
    )

    t = _human_mr_approval(ctx)
    # NO review artifact

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_not_eligible_flagged_false_auto_merges_anyway(tmp_path, monkeypatch):
    """auto_merge_eligible: false no longer blocks — auto-merge fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "author": "mill-bot",
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
        lambda self, *, source_branch: {"merged": True},
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
    _write_review_artifact(ctx, t, eligible=False, comment="risky migration")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_not_eligible_no_comment_line_auto_merges_anyway(tmp_path, monkeypatch):
    """Review artifact with eligible=False and no comment — auto-merge
    still fires (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "author": "mill-bot",
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
        lambda self, *, source_branch: {"merged": True},
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
    _write_review_artifact(ctx, t, eligible=False)  # no comment

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_comment_dedup_same_reason_no_duplicate(tmp_path, monkeypatch):
    """Two polls with the same reason → exactly 1 comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    # First poll → writes comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1

    # Second poll — same conditions, same reason → no new comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1


def test_comment_dedup_different_reason_new_comment(tmp_path, monkeypatch):
    """Reason changes → new comment fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    # First poll → disabled flag comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1

    # Hack: change the stored reason to simulate a prior different
    # reason (e.g., was CI pending, now CI succeeded but still not
    # eligible). Then re-run — the new reason text differs.
    reason_path = ctx.service.workspace(t).artifacts_dir / "merge_reason.txt"
    reason_path.write_text("old different reason", encoding="utf-8")

    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 2


def test_waiting_auto_merge_becomes_implement_complete_on_ci_failure(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE poll where CI now fails → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
        lambda self, *, source_branch: {"conclusion": "failure", "failing": []},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    # Transition to WAITING_AUTO_MERGE manually (simulate previous poll).
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_waiting_auto_merge_stays_waiting_when_ci_pending(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll with CI still pending → stays WAITING_AUTO_MERGE
    (artifact verdict no longer affects eligibility)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )

    t = _human_mr_approval(ctx)
    # Write the artifact as eligible so the WAITING_AUTO_MERGE
    # transition is plausible.
    _write_review_artifact(ctx, t, eligible=True)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    # Now change the artifact to not eligible — this no longer matters.
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE


def test_waiting_auto_merge_to_done_on_ci_success(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll where CI is now green → DONE."""
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
# WAITING_AUTO_MERGE → IMPLEMENT_COMPLETE on conflict
# ============================================================


def test_waiting_auto_merge_conflicting_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + mergeable=False + update_branch fails → REBASING (autonomous rebase enabled by default)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert "rebasing automatically" in out.note


def test_waiting_auto_merge_conflicting_kill_switch_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + mergeable=False + autonomous_rebase_enabled=False → IMPLEMENT_COMPLETE."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        autonomous_rebase_enabled="false",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


def test_waiting_auto_merge_conflicting_update_branch_succeeds_stays_in_waiting_auto_merge(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + mergeable=False + update_branch succeeds → WAITING_AUTO_MERGE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {
            "updated": True,
            "reason": "update-branch accepted",
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE


def _green_behind_forge(monkeypatch, *, update_branch_result=None):
    """Patch the forge: PR open+mergeable but behind target, CI green.

    *update_branch_result* — dict return value for ``update_branch``.
    Defaults to ``{"updated": True, "reason": "update-branch accepted"}``.
    """
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
        },
    )
    if update_branch_result is None:
        update_branch_result = {"updated": True, "reason": "update-branch accepted"}
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: update_branch_result,
    )


def test_waiting_auto_merge_green_behind_auto_updates_and_retries(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + green CI + branch behind target →
    update-branch API called, returns WAITING_AUTO_MERGE to retry
    after CI re-runs."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    update_calls = []
    _green_behind_forge(monkeypatch)
    # Track update_branch invocations.
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: (
            update_calls.append(source_branch)
            or {"updated": True, "reason": "update-branch accepted"}
        ),
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert update_calls, "update_branch should have been called"
    assert f"mill/{t.id}" in update_calls


def test_human_mr_approval_green_behind_auto_updates_and_retries(tmp_path, monkeypatch):
    """HUMAN_MR_APPROVAL + green CI + branch behind target →
    update-branch API called, returns WAITING_AUTO_MERGE to retry
    after CI re-runs."""
    ctx = _gh(tmp_path)
    update_calls = []
    _green_behind_forge(monkeypatch)
    # Track update_branch invocations.
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: (
            update_calls.append(source_branch)
            or {"updated": True, "reason": "update-branch accepted"}
        ),
    )

    t = _human_mr_approval(ctx)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert update_calls, "update_branch should have been called"
    assert f"mill/{t.id}" in update_calls


# Branch cleanup on DONE-via-merge (delete_branch_on_merge)
# ============================================================


def _merged_pr_status(monkeypatch):
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://gh/o/r/pull/3",
        },
    )


def test_done_via_merge_deletes_branch_when_flag_enabled(tmp_path, monkeypatch):
    """delete_branch_on_merge=True → delete_branch called once with the branch."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    _merged_pr_status(monkeypatch)
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert calls == [f"mill/{t.id}"]


def test_done_via_merge_skips_delete_when_flag_disabled(tmp_path, monkeypatch):
    """delete_branch_on_merge=False → delete_branch never called."""
    ctx = _gh(tmp_path, delete_branch_on_merge=False)
    _merged_pr_status(monkeypatch)
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert calls == []


def test_done_via_merge_cleanup_failure_does_not_block_done(tmp_path, monkeypatch):
    """A delete_branch that raises/returns False must not prevent DONE."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    _merged_pr_status(monkeypatch)

    def boom(self, *, branch):
        raise RuntimeError("forge down")

    monkeypatch.setattr(github.GitHubForge, "delete_branch", boom)
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_blocked_closed_unmerged_does_not_delete_branch(tmp_path, monkeypatch):
    """A BLOCKED/PR-closed transition must not trigger branch deletion."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert calls == []


# ============================================================

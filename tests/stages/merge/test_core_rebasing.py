import json
import subprocess

from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage, _read_counter, _write_counter
from robotsix_mill.vcs.git_ops import PostPushResult


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


# --- REBASING path: clean rebase → IMPLEMENT_COMPLETE ---


def test_rebasing_clean_rebase_returns_to_implement_complete(tmp_path, monkeypatch):
    """Ticket in REBASING → rebase agent succeeds → post-check passes → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, target=target, remote_url=remote_url)
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    # Post-rebase routing checks whether a PR exists; mock so the
    # forge reports a PR → route stays IMPLEMENT_COMPLETE (regression).
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

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls["branch"] == f"mill/{t.id}"


def test_rebasing_success_routes_to_waiting_auto_merge_when_from_human_mr_approval(
    tmp_path, monkeypatch
):
    """REBASING with _REBASE_FROM_STATE=human_mr_approval + auto_merge eligible → WAITING_AUTO_MERGE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    def fake_rebase(**kwargs):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    # Two-phase pr_status: first call (entry check in _run_rebase) returns
    # "behind" so the rebase proceeds; second call (post-rebase routing in
    # _handle_rebase_success) returns "clean" so it enters the from_state
    # routing logic.
    pr_call_count = []

    def staged_pr(self, *, source_branch):
        pr_call_count.append(1)
        if len(pr_call_count) == 1:
            # Entry check — not clean, must proceed to _handle_conflict.
            return {
                "merged": False,
                "state": "open",
                "url": "u",
                "mergeable": True,
                "mergeable_state": "behind",
            }
        # Post-rebase routing — PR is now clean after rebase.
        return {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "clean",
        }

    monkeypatch.setattr(github.GitHubForge, "pr_status", staged_pr)

    monkeypatch.setattr(
        MergeStage,
        "_auto_merge_eligible",
        lambda self, ticket, ctx, pr_head_sha=None, forge=None, pr=None: (True, ""),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    from_state_path = ctx.service.workspace(t).artifacts_dir / "rebase_from_state.txt"
    from_state_path.write_text(State.HUMAN_MR_APPROVAL.value, encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert "resuming autonomous merge monitoring" in out.note


def test_rebasing_success_routes_to_human_mr_approval_when_auto_merge_not_eligible(
    tmp_path, monkeypatch
):
    """REBASING with _REBASE_FROM_STATE=human_mr_approval + auto_merge NOT eligible → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    def fake_rebase(**kwargs):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    # Two-phase pr_status: first call returns "behind" so the rebase
    # proceeds; second call returns "clean" for the from_state routing.
    pr_call_count = []

    def staged_pr(self, *, source_branch):
        pr_call_count.append(1)
        if len(pr_call_count) == 1:
            return {
                "merged": False,
                "state": "open",
                "url": "u",
                "mergeable": True,
                "mergeable_state": "behind",
            }
        return {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "clean",
        }

    monkeypatch.setattr(github.GitHubForge, "pr_status", staged_pr)

    monkeypatch.setattr(
        MergeStage,
        "_auto_merge_eligible",
        lambda self, ticket, ctx, pr_head_sha=None, forge=None, pr=None: (
            False,
            "review required",
        ),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    from_state_path = ctx.service.workspace(t).artifacts_dir / "rebase_from_state.txt"
    from_state_path.write_text(State.HUMAN_MR_APPROVAL.value, encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert "rebase succeeded but review required" in out.note


def test_mechanical_rebase_skips_agent_when_clean(tmp_path, monkeypatch):
    """When try_mechanical_rebase succeeds and push_with_lease works,
    the rebase agent is never spawned."""
    ctx = _gh(tmp_path)

    agent_called = []

    def fake_rebase(**kwargs):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.try_mechanical_rebase",
        lambda repo, target_branch: True,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push_with_lease",
        lambda repo, branch, remote_url, token: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda repo, target: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.file_blobs",
        lambda repo, files: {},
    )
    # Post-rebase routing checks whether a PR exists; mock so the
    # forge reports a PR → route stays IMPLEMENT_COMPLETE.
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

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_called == [], "rebase agent should not have been called"


def test_mechanical_rebase_falls_through_on_conflict(tmp_path, monkeypatch):
    """When try_mechanical_rebase returns False (conflicts), the rebase
    agent IS spawned."""
    ctx = _gh(tmp_path)

    agent_called = []

    def fake_rebase(**kwargs):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.try_mechanical_rebase",
        lambda repo, target_branch: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert agent_called == [1], "rebase agent should have been called"


def test_mechanical_push_failure_falls_through_to_agent(tmp_path, monkeypatch):
    """When try_mechanical_rebase succeeds but push_with_lease fails,
    the rebase agent IS spawned (fallthrough)."""

    ctx = _gh(tmp_path)

    agent_called = []

    def fake_rebase(**kwargs):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.try_mechanical_rebase",
        lambda repo, target_branch: True,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push_with_lease",
        lambda repo, branch, remote_url, token: (
            # Simulate lease violation or network error
            (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["git", "push"]))
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert agent_called == [1], "rebase agent should have been called on push failure"


def test_rebase_clears_stale_review_artifact_and_cache(tmp_path, monkeypatch):
    """After a successful rebase, the review.md artifact and the review
    stage-outcome cache must be cleared so a subsequent review pass
    evaluates the current diff rather than replaying a stale verdict."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
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

    t = _in_rebasing(ctx)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Pre-populate a review.md artifact and a stage cache entry to
    # simulate a pre-rebase REQUEST_CHANGES verdict.
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "review.md").write_text(
        "verdict: REQUEST_CHANGES\n"
        "auto_merge_eligible: false\n"
        "head_sha: old-stale-sha\n"
        "comment: build artifacts in diff\n",
        encoding="utf-8",
    )
    # Write a stage_cache.json with a "review" entry.

    cache_path = ws.artifacts_dir / "stage_cache.json"
    cache_path.write_text(
        json.dumps(
            {"review": {"input_hash": "abc123", "next_state": "ready", "note": ""}}
        ),
        encoding="utf-8",
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # After rebase, review.md must be removed.
    assert not (ws.artifacts_dir / "review.md").exists(), (
        "review.md must be deleted after successful rebase"
    )

    # After rebase, the review stage cache entry must be cleared.
    cache = (
        json.loads(cache_path.read_text(encoding="utf-8"))
        if cache_path.exists()
        else {}
    )
    assert "review" not in cache, (
        "review stage cache entry must be removed after successful rebase"
    )


def test_rebase_success_blocks_when_implement_files_silently_dropped(
    tmp_path, monkeypatch
):
    """After rebase succeeds, if implement-stage source files are no longer
    in the branch diff vs merge-base, the ticket must BLOCK with a diagnostic
    listing the dropped files."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
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

    # Simulate: pre-rebase the branch had 2 source files, but after
    # rebase only 1 remains (the other was silently dropped).
    pre_files = ["src/mod.py", "src/dropped.py"]
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda repo, target_branch="main", ref="HEAD": pre_files,
    )
    seen_exempt: list[object] = []

    def fake_integrity(
        repo,
        target_branch,
        pre_rebase_files,
        pre_rebase_blobs=None,
        exempt_paths=None,
        target_pre_blobs=None,
    ):
        seen_exempt.append(exempt_paths)
        return (False, ["src/dropped.py"], [])

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.check_rebase_diff_integrity",
        fake_integrity,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "src/dropped.py" in (out.note or "")
    # The configured exemption list must actually reach the guard — a
    # default-only path would silently ignore an operator's override.
    assert seen_exempt == [ctx.settings.rebase_drop_exempt_paths]
    assert "docs/modules.yaml" in ctx.settings.rebase_drop_exempt_paths


def test_rebase_sibling_modified_produces_targeted_blocked_message(
    tmp_path, monkeypatch
):
    """When pre-rebase files are superseded by a sibling PR (target
    changed the same files during the rebase window), the BLOCKED message
    must mention "sibling PR" / "sibling's version" rather than the
    generic "silently dropped" wording."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
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

    pre_files = ["src/keep.py", "src/sibling_modified.py"]
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda repo, target_branch="main", ref="HEAD": pre_files,
    )

    def fake_integrity(
        repo,
        target_branch,
        pre_rebase_files,
        pre_rebase_blobs=None,
        exempt_paths=None,
        target_pre_blobs=None,
    ):
        return (False, [], ["src/sibling_modified.py"])

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.check_rebase_diff_integrity",
        fake_integrity,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    note = out.note or ""
    assert "src/sibling_modified.py" in note
    assert "sibling" in note.lower(), (
        "sibling-modified BLOCKED message must mention 'sibling'"
    )


def test_rebase_drop_messages_do_not_advise_a_retry(tmp_path, monkeypatch):
    """Neither drop message may tell the operator to resume-and-retry.

    A retry re-runs the same rebase against the same target and reproduces
    the same result. Measured on the live board: tickets blocked this way
    accumulated 5, 6 and 7 identical BLOCKED events before anyone looked.
    Of four such blocks investigated, three were files the target already
    carried (superseded) and one was a genuine drop — a retry resolves
    neither. The message must send the operator to a decision instead.
    """
    for dropped, sibling in (
        (["src/gone.py"], []),
        ([], ["src/superseded.py"]),
    ):
        ctx = _gh(tmp_path / f"case{len(dropped)}")

        def fake_rebase(
            *,
            settings,
            repo_dir,
            branch,
            target,
            memory="",
            remote_url=None,
            token=None,
            pre_rebase_files=None,
            previously_dropped_files=None,
        ):
            return RebaseResult(status="DONE", summary="ok")

        monkeypatch.setattr("robotsix_mill.stages.merge.run_rebase_agent", fake_rebase)
        monkeypatch.setattr(
            "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.merge.git_ops.post_push_check",
            lambda *a, **k: PostPushResult.PASS,
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
            "robotsix_mill.stages.merge.git_ops.changed_source_files",
            lambda repo, target_branch="main", ref="HEAD": ["src/keep.py"],
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.merge.git_ops.check_rebase_diff_integrity",
            lambda *a, _d=dropped, _s=sibling, **k: (False, _d, _s),
        )

        t_ = _in_rebasing(ctx)
        repo_dir = ctx.service.workspace(t_).dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / ".git").mkdir(exist_ok=True)

        out = MergeStage().run(t_, ctx)
        note = (out.note or "").lower()
        assert out.next_state is State.BLOCKED
        assert "resume-blocked to retry" not in note, (
            f"rebase-drop message still advises a retry: {out.note!r}"
        )
        # It must instead point at how to decide.
        assert "supersed" in note or "close the ticket" in note, (
            f"message gives no way to decide the case: {out.note!r}"
        )


def test_rebase_rerun_receives_previously_dropped_files(tmp_path, monkeypatch):
    """When a prior rebase dropped files (recorded in rebase_dropped_files.txt),
    the next run_rebase_agent call receives them as previously_dropped_files."""
    ctx = _gh(tmp_path)

    rebase_calls = []

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        rebase_calls.append(
            {
                "pre_rebase_files": pre_rebase_files,
                "previously_dropped_files": previously_dropped_files,
            }
        )
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
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
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda repo, target_branch="main", ref="HEAD": ["src/mod.py"],
    )
    monkeypatch.setattr(
        "robotsix_mill.vcs.git_diff.changed_source_files",
        lambda repo, target_branch="main", ref="HEAD": ["src/mod.py"],
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Pre-populate the dropped-files artifact from a prior failed attempt.
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    dropped_path = artifacts / "rebase_dropped_files.txt"
    dropped_path.write_text("docs/modules.yaml\nsrc/search.py\n", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    # The rebase should succeed (post-check passes, integrity clean).
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # Verify the agent received the previously-dropped file list.
    assert len(rebase_calls) == 1
    assert rebase_calls[0]["previously_dropped_files"] == [
        "docs/modules.yaml",
        "src/search.py",
    ]
    assert rebase_calls[0]["pre_rebase_files"] == ["src/mod.py"]


def test_rebase_success_no_pre_rebase_files_passes_integrity(tmp_path, monkeypatch):
    """When pre_rebase_files is empty (e.g. git plumbing failure), the
    integrity check is skipped and the rebase succeeds normally."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
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
    # Pre-rebase file list is empty — integrity check is skipped.
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.changed_source_files",
        lambda repo, target_branch="main", ref="HEAD": [],
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_rebasing_push_targets_per_repo_remote(tmp_path, monkeypatch):
    """Regression: the post-rebase force-push must target the ticket's
    *per-repo* remote, not the global FORGE_REMOTE_URL.

    A ticket on a non-mill board whose rebased commit was pushed to the
    global (mill) remote left the real PR branch untouched → GitHub kept
    reporting the PR conflicting → endless REBASING → BLOCKED.
    """
    from robotsix_mill.config import RepoConfig

    base = _gh(tmp_path)  # global FORGE_REMOTE_URL = https://github.com/o/r.git
    per_repo_url = "https://github.com/o/other-repo.git"
    ctx = StageContext(
        settings=base.settings,
        service=base.service,
        repo_config=RepoConfig(
            repo_id="other-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            forge_remote_url=per_repo_url,
        ),
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None, pre_rebase_files=None, previously_dropped_files=None: (
            RebaseResult(status="DONE", summary="ok")
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check", fake_post_check
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

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # The push must go to the per-repo remote, not the global one.
    assert post_check_calls["remote_url"] == per_repo_url


def test_rebasing_success_no_pr_routes_to_ready(tmp_path, monkeypatch):
    """Rebase agent succeeds, post-check passes, but no PR exists for the
    branch → route to READY so the ticket re-enters implement."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, target=target, remote_url=remote_url)
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    # pr_status returns None → no PR exists → route to READY.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.READY
    assert post_check_calls["branch"] == f"mill/{t.id}"


def test_rebasing_noop_skips_force_push(tmp_path, monkeypatch):
    """Rebase agent succeeds; post_push_check is always called to verify
    the agent-driven push actually landed. The old deterministic no-op
    skip (local==remote → skip push) is gone — the agent pushes, the
    stage only verifies."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.head_sha",
        lambda repo: sha,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.remote_branch_sha",
        lambda repo, branch: sha,
    )
    post_check_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: post_check_calls.append(1) or PostPushResult.PASS,
    )
    # Need a PR status so the stage routes to IMPLEMENT_COMPLETE, not READY.
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

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls == [1]  # post_push_check IS called


def test_rebasing_noop_blocks_after_max_attempts(tmp_path, monkeypatch):
    """A rebase that never resolves the conflict is bounded: once
    the attempt budget is spent the ticket goes BLOCKED (once), instead
    of ping-ponging forever. The post-check still passes."""
    ctx = _gh(tmp_path, rebase_max_attempts="2")
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: PostPushResult.PASS,
    )
    # PR still reports conflicting (not mergeable) so attempts are counted
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

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # attempt 1 → IMPLEMENT_COMPLETE (re-poll), attempt 2 (== max) → BLOCKED
    o1 = MergeStage().run(t, ctx)
    assert o1.next_state is State.IMPLEMENT_COMPLETE
    o2 = MergeStage().run(ctx.service.get(t.id), ctx)
    assert o2.next_state is State.BLOCKED


# --- REBASING: retry stays REBASING ---


def test_rebasing_retry_stays_rebasing(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt < max → Outcome(REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="3")
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

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING  # retry, not IMPLEMENT_COMPLETE

    counter_path = ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    assert _read_counter(counter_path) == 1


# --- REBASING: exhausted → BLOCKED ---


def test_rebasing_exhausted_blocks(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt == max → Outcome(BLOCKED)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "rebase failed after 1 attempt" in out.note
    assert push_called == []  # never force-pushed on failure


def test_rebase_failure_note_surfaces_conflicts_and_agent_detail(tmp_path, monkeypatch):
    """The BLOCKED note names the conflicting file(s) and the rebase agent's
    own explanation instead of a generic 'manual conflict resolution
    required' (better operator feedback)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(
            status="FAILED",
            summary="both sides rewrote tests/test_reconcile.py; a human must "
            "decide which assertions to keep",
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.conflicted_files",
        lambda repo: ["tests/test_reconcile.py"],
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "tests/test_reconcile.py" in out.note
    assert "both sides rewrote" in out.note
    assert "rebase failed after 1 attempt" in out.note


# --- Full cycle: IMPLEMENT_COMPLETE → REBASING → IMPLEMENT_COMPLETE ---


def test_implement_complete_to_rebasing_and_back(tmp_path, monkeypatch):
    """Full cycle: IMPLEMENT_COMPLETE + mergeable=False → REBASING → then rebase success → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
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
    calls = {}

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        calls.update(repo_dir=repo_dir, branch=branch, target=target)
        return RebaseResult(status="DONE", summary="ok")  # success

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _implement_complete(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Step 1: IMPLEMENT_COMPLETE + conflicting → REBASING.
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert calls == {}  # agent not called yet

    # Actually transition the ticket to REBASING.
    ctx.service.transition(t.id, State.REBASING, note="conflicting")
    t = ctx.service.get(t.id)

    # Switch pr_status to report a PR exists (mergeable=True) so the
    # post-rebase routing stays IMPLEMENT_COMPLETE.
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

    # Step 2: REBASING → rebase agent runs, succeeds → IMPLEMENT_COMPLETE.
    out2 = MergeStage().run(t, ctx)
    assert calls["branch"] == f"mill/{t.id}"
    assert calls["target"] == "main"
    assert str(repo_dir) in calls["repo_dir"]
    assert post_check_calls["branch"] == f"mill/{t.id}"
    assert out2.next_state is State.IMPLEMENT_COMPLETE


def test_rebasing_no_workspace_clone_blocks(tmp_path, monkeypatch):
    """If the workspace clone is missing in REBASING, cannot rebase → BLOCKED."""
    ctx = _gh(tmp_path)
    # No repo dir created — workspace is empty.
    t = _in_rebasing(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note


def test_rebase_failure_exhausts_attempts_then_blocks(tmp_path, monkeypatch):
    """Agent returns False for every attempt → BLOCKED after max (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="2")

    agent_calls = []

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        agent_calls.append(1)
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Attempt 1: agent returns False → stays REBASING (retry next poll)
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert len(agent_calls) == 1

    # Attempt 2: agent returns False again → exhausted → BLOCKED
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert "rebase failed after 2 attempt" in out2.note
    assert len(agent_calls) == 2


def test_rebase_agent_crash_is_treated_as_failure(tmp_path, monkeypatch):
    """If the agent raises, treat as False — failure path (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def boom(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        raise RuntimeError("LLM timeout")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        boom,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "rebase failed after 1 attempt" in out.note


def test_no_force_push_on_rebase_failure(tmp_path, monkeypatch):
    """When agent returns False, no force-push is made (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert push_called == []  # never called


def test_push_failure_after_rebase_success_blocks(tmp_path, monkeypatch):
    """Rebase succeeds but post_push_check reports NOT_LANDED → BLOCKED."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: PostPushResult.NOT_LANDED,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "push did not land" in out.note


def test_rebase_counter_resets_only_when_pr_becomes_mergeable(tmp_path, monkeypatch):
    """A push is NOT proof the conflict is resolved (git rebase rewrites
    SHAs every run). The attempt counter must persist across rebase+push
    cycles and only reset to 0 when the IMPLEMENT_COMPLETE poll sees a
    mergeable PR — otherwise the loop is unbounded."""
    ctx = _gh(tmp_path, rebase_max_attempts="3")

    call_count = [0]

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        call_count[0] += 1
        # First call fails, second succeeds.
        if call_count[0] == 2:
            return RebaseResult(status="DONE", summary="ok")
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    # The PR is conflicting (mergeable=False) WHILE rebasing, then becomes
    # mergeable once the conflict is truly resolved. A mutable holder lets
    # the final IMPLEMENT_COMPLETE poll see mergeable=True.
    mergeable = [False]
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": mergeable[0],
        },
    )

    def fake_post_check(repo, branch, target, remote_url, token):
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    counter_path = ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"

    # Attempt 1 fails → counter=1, stays REBASING
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert _read_counter(counter_path) == 1

    # Attempt 2 succeeds+pushes → back to IMPLEMENT_COMPLETE, but counter is
    # PERSISTED (==2), NOT reset — a push doesn't prove resolution.
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(counter_path) == 2

    # Now the IMPLEMENT_COMPLETE poll sees a genuinely mergeable + CI green PR
    # → the conflict is really gone → counter resets to 0 AND ticket
    # promotes to HUMAN_MR_APPROVAL (gates passed).
    mergeable[0] = True
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success"},
    )
    ctx.service.transition(t.id, State.IMPLEMENT_COMPLETE, note="rebased")
    out3 = MergeStage().run(ctx.service.get(t.id), ctx)
    assert out3.next_state is State.HUMAN_MR_APPROVAL  # promoted
    assert _read_counter(counter_path) == 0  # counter reset during poll


def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    """The force-push must reference only the ticket's own branch."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_args = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        push_args.update(branch=branch, remote_url=remote_url, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    # Branch pushed is the ticket's branch, not the target.
    assert push_args["branch"] == f"mill/{t.id}"
    assert push_args["branch"] != "main"  # never push target branch


def test_counter_read_write(tmp_path):
    """Unit tests for the attempt counter helpers."""
    p = tmp_path / "counter.txt"
    assert _read_counter(p) == 0  # missing file
    p.write_text("garbage")
    assert _read_counter(p) == 0  # unparseable
    _write_counter(p, 5)
    assert _read_counter(p) == 5
    _write_counter(p, 0)
    assert _read_counter(p) == 0


def test_rebase_force_push_uses_minted_token_not_raw_forge_token(tmp_path, monkeypatch):
    """Regression: the post-rebase force-push must use github_push_token()
    (the minted App/PAT token) — not the raw s.forge_token, which is
    empty under GitHub App auth -> unauthenticated push -> git exit 128
    -> ticket BLOCKED. The rebase+push moved to the REBASING-state path
    (#26), so drive the ticket through REBASING here."""
    ctx = _gh(tmp_path)  # FORGE_TOKEN="t" (raw); minted token differs
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.github_push_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.github_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    seen = {}
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: seen.update(token=token),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    assert seen.get("token") == "MINTED-APP-TOK"  # not the raw "t"


# ============================================================

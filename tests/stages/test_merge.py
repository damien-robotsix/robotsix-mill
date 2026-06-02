from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage, _read_counter, _write_counter


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg

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
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
    )


# ============================================================
# IMPLEMENT_COMPLETE gate-check poll path (new)
# ============================================================


def test_implement_complete_ci_green_mergeable_promotes_to_human_mr_approval(
    tmp_path, monkeypatch
):
    """CI green + PR mergeable → HUMAN_MR_APPROVAL (gates passed)."""
    ctx = _gh(tmp_path)
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
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_implement_complete_ci_failing_transitions_to_fixing_ci(tmp_path, monkeypatch):
    """CI failing → FIXING_CI."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.FIXING_CI


def _ci_failing_mergeable(monkeypatch):
    """Patch the forge so the PR is open+mergeable with failing CI."""
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


def test_implement_complete_ci_failing_behind_main_rebases_before_ci_fix(
    tmp_path, monkeypatch
):
    """CI failing + branch behind main → REBASING (not FIXING_CI).

    A repo-wide gate often fails on code that isn't the ticket's because the
    branch was cut from an older main. Rebase onto current main first; ci_fix
    can't fix non-ticket code."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch)
    # Workspace clone present + behind main.
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(merge_mod.git_ops, "branch_is_behind_main", lambda repo: True)
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.REBASING


def test_implement_complete_ci_failing_up_to_date_goes_to_ci_fix(tmp_path, monkeypatch):
    """CI failing + branch NOT behind main → FIXING_CI (genuine failure;
    a rebase would be a no-op, so don't loop)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(merge_mod.git_ops, "branch_is_behind_main", lambda repo: False)
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_conflicting_transitions_to_rebasing(tmp_path, monkeypatch):
    """PR conflicting → REBASING."""
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
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING


def test_implement_complete_ci_pending_stays_same_state(tmp_path, monkeypatch):
    """CI pending → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
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
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_no_check_status_stays_same_state(tmp_path, monkeypatch):
    """check_status returns None → same-state IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: None,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_merged_transitions_to_done(tmp_path, monkeypatch):
    """PR merged while polling → DONE."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://gh/o/r/pull/3",
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_implement_complete_closed_unmerged_blocks(tmp_path, monkeypatch):
    """PR closed unmerged → BLOCKED."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


def test_implement_complete_pr_status_none_stays_same_state(tmp_path, monkeypatch):
    """pr_status returns None → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_transient_error_stays_same_state(tmp_path, monkeypatch):
    """pr_status raises → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_check_status_transient_error_stays_same_state(
    tmp_path, monkeypatch
):
    """check_status raises → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


# --- existing paths (updated for IMPLEMENT_COMPLETE) ---


def test_blocked_when_forge_unconfigured(tmp_path):
    ctx = _ctx(tmp_path)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED and "forge not configured" in out.note


def test_merged_to_done(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://github.com/o/r/pull/3",
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE and "pull/3" in out.note
    assert (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_closed_unmerged_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "closed without merge" in out.note


def test_open_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL  # same state = worker no-op


def test_transient_error_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)

    def boom(self, *, source_branch):
        raise RuntimeError("api down")

    monkeypatch.setattr(github.GitHubForge, "pr_status", boom)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL  # retry next poll, not blocked


# --- mergeable flag: explicit True/None treated as mergeable (no rebase) ---


def test_open_mergeable_true_is_noop(tmp_path, monkeypatch):
    """PR open with mergeable=True → standard no-op, no rebase."""
    ctx = _gh(tmp_path)
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
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_open_mergeable_none_is_noop(tmp_path, monkeypatch):
    """mergeable=None (unchecked) → treat as mergeable, no rebase."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": None,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


# --- New: mergeable PR never enters REBASING ---


def test_mergeable_pr_never_enters_rebasing(tmp_path, monkeypatch):
    """mergeable=True/None → OUTCOME(HUMAN_MR_APPROVAL), never REBASING."""
    ctx = _gh(tmp_path)
    for mergeable in (True, None):
        monkeypatch.setattr(
            github.GitHubForge,
            "pr_status",
            lambda self, *, source_branch, m=mergeable: {
                "merged": False,
                "state": "open",
                "url": "u",
                "mergeable": m,
            },
        )
        out = MergeStage().run(_human_mr_approval(ctx), ctx)
        assert out.next_state is State.HUMAN_MR_APPROVAL
        assert "REBASING" not in str(out.next_state.value)


# --- HUMAN_MR_APPROVAL silent fallback: conflicting → IMPLEMENT_COMPLETE ---


def test_human_mr_approval_conflicting_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=False → IMPLEMENT_COMPLETE (silent fallback)."""
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
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


def test_human_mr_approval_ci_failing_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=True + CI failure → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


# --- REBASING path: clean rebase → IMPLEMENT_COMPLETE ---


def test_rebasing_clean_rebase_returns_to_implement_complete(tmp_path, monkeypatch):
    """Ticket in REBASING → rebase agent succeeds → force-push → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
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
            "mergeable": True,
        },
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert push_calls["branch"] == f"mill/{t.id}"


def test_rebasing_success_no_pr_routes_to_ready(tmp_path, monkeypatch):
    """Rebase agent succeeds, force-pushes, but no PR exists for the
    branch → route to READY so the ticket re-enters implement."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
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
    assert push_calls["branch"] == f"mill/{t.id}"


def test_rebasing_noop_skips_force_push(tmp_path, monkeypatch):
    """Rebase agent succeeds but the remote already has this exact
    commit (no-op). We must NOT force-push (that re-triggers CI + a
    mergeable recompute → endless ping-pong). Stay REBASING as a silent
    same-state re-poll (no ntfy), bounded by the attempt counter."""
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
        lambda repo, branch: sha,  # remote already has it
    )
    pushed = []
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        lambda repo, branch, remote_url, token: pushed.append(branch),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING  # silent re-poll, not pushed
    assert pushed == []  # the no-op force-push was skipped


def test_rebasing_noop_blocks_after_max_attempts(tmp_path, monkeypatch):
    """A no-op rebase that never resolves the conflict is bounded: once
    the attempt budget is spent the ticket goes BLOCKED (once), instead
    of ping-ponging forever."""
    ctx = _gh(tmp_path, rebase_max_attempts="2")
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    sha = "cafebabecafebabecafebabecafebabecafebabe"
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.head_sha",
        lambda repo: sha,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.remote_branch_sha",
        lambda repo, branch: sha,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push")),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # attempt 1 → REBASING (re-poll), attempt 2 (== max) → BLOCKED
    o1 = MergeStage().run(t, ctx)
    assert o1.next_state is State.REBASING
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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
        "robotsix_mill.stages.merge.git_ops.push",
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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
    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
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
            "mergeable": True,
        },
    )

    # Step 2: REBASING → rebase agent runs, succeeds → IMPLEMENT_COMPLETE.
    out2 = MergeStage().run(t, ctx)
    assert calls["branch"] == f"mill/{t.id}"
    assert calls["target"] == "main"
    assert str(repo_dir) in calls["repo_dir"]
    assert push_calls["branch"] == f"mill/{t.id}"
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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

    def boom(*, settings, repo_dir, branch, target):
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert push_called == []  # never called


def test_push_failure_after_rebase_success_blocks(tmp_path, monkeypatch):
    """Rebase succeeds but force-push fails → BLOCKED (through REBASING)."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    def boom_push(repo, branch, remote_url, token):
        raise RuntimeError("remote rejected")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        boom_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "force-push failed" in out.note


def test_rebase_counter_resets_only_when_pr_becomes_mergeable(tmp_path, monkeypatch):
    """A push is NOT proof the conflict is resolved (git rebase rewrites
    SHAs every run). The attempt counter must persist across rebase+push
    cycles and only reset to 0 when the IMPLEMENT_COMPLETE poll sees a
    mergeable PR — otherwise the loop is unbounded."""
    ctx = _gh(tmp_path, rebase_max_attempts="3")

    call_count = [0]

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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
    # pr_status mock needed because step 2 rebase succeeds+pushes and
    # the post-rebase routing checks whether a PR exists.
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

    def fake_push(repo, branch, remote_url, token):
        pass

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
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

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
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

    def fake_push(repo, branch, remote_url, token):
        push_args.update(branch=branch, remote_url=remote_url, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        fake_push,
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
    """Regression: the post-rebase force-push must use github_token()
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
        "robotsix_mill.stages.merge.github_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    seen = {}
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        lambda repo, branch, remote_url, token: seen.update(token=token),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    assert seen.get("token") == "MINTED-APP-TOK"  # not the raw "t"


# ============================================================
# D. Merge-stage CI branching (updated for IMPLEMENT_COMPLETE)
# ============================================================


def test_mergeable_failing_ci_falls_back_to_implement_complete(tmp_path, monkeypatch):
    """Mergeable PR + failing CI → IMPLEMENT_COMPLETE (silent fallback from HUMAN_MR_APPROVAL)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_mergeable_green_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """Mergeable PR + green CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_none_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """check_status returns None (no checks) → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: None,
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_pending_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """Mergeable PR + pending CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_check_status_exception_is_noop(tmp_path, monkeypatch):
    """check_status raises → transient re-poll."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_conflicting_pr_skips_check_status(tmp_path, monkeypatch):
    """Conflicting PR → silent fallback to IMPLEMENT_COMPLETE; check_status never called."""
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
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return {"conclusion": "success", "failing": []}

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)

    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert check_calls == []  # never called for conflicting PR


def test_merged_pr_skips_check_status(tmp_path, monkeypatch):
    """Merged PR → DONE; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
        },
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return None

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.DONE
    assert check_calls == []  # never called


def test_closed_pr_skips_check_status(tmp_path, monkeypatch):
    """Closed PR → BLOCKED; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return None

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert check_calls == []


# --- New: fetch-before-rebase-agent tests ---


def test_fetch_called_before_rebase_agent(tmp_path, monkeypatch):
    """git_ops.fetch is called before run_rebase_agent in _handle_conflict."""
    ctx = _gh(tmp_path)
    calls = []

    def fake_fetch(repo, *, remote_url, token, branch):
        calls.append("fetch")

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        calls.append("agent")
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert calls == ["fetch", "agent"]


def test_fetch_failure_does_not_invoke_agent(tmp_path, monkeypatch):
    """When git_ops.fetch raises CalledProcessError, the agent is not invoked."""
    import subprocess

    ctx = _gh(tmp_path)
    agent_called = []

    def fake_fetch(repo, *, remote_url, token, branch):
        raise subprocess.CalledProcessError(1, "git fetch")

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert agent_called == []
    # With default max_attempts (3), a failed attempt stays in REBASING
    assert out.next_state is State.REBASING


# --- tracing: root span only on first attempt ---


def test_root_span_only_on_first_rebase_attempt(tmp_path, monkeypatch):
    """start_ticket_root_span must fire only on attempt==1.
    Retries (attempt>1) skip the root span to avoid creating duplicate
    Langfuse traces for the same logical rebase operation."""
    import contextlib

    from robotsix_mill.runtime import tracing as tr

    ctx = _gh(tmp_path, rebase_max_attempts="3")

    root_calls = []
    stage_calls = []

    @contextlib.contextmanager
    def fake_root(ticket_id, stage_name, extra_attributes=None, repo_config=None):
        root_calls.append({"ticket_id": ticket_id, "stage_name": stage_name})
        yield

    @contextlib.contextmanager
    def fake_stage(stage_name):
        stage_calls.append(stage_name)
        yield

    # Capture real functions before patching to avoid recursion gotchas
    # if the wrapper were to import the real function after patching.
    _real_root = tr.start_ticket_root_span
    _real_stage = tr.trace_stage

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(tr, "trace_stage", fake_stage)
    # Agent always fails → stays REBASING (retry loop).
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="FAILED", summary="nope"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Run 3 times — simulating poll cycles.
    for _ in range(3):
        MergeStage().run(t, ctx)

    # Root span must have been called exactly once (first attempt only).
    assert len(root_calls) == 1, (
        f"expected 1 root span call, got {len(root_calls)}: {root_calls}"
    )
    assert root_calls[0]["ticket_id"] == t.id

    # trace_stage("rebase") called once per invocation.
    assert len(stage_calls) == 3, (
        f"expected 3 stage calls, got {len(stage_calls)}: {stage_calls}"
    )
    assert all(s == "rebase" for s in stage_calls)


# ============================================================
# E. Auto-merge gate (new)
# ============================================================


def _write_review_artifact(ctx, ticket, *, verdict="APPROVE", eligible=True):
    """Helper: write a review.md artifact for auto-merge tests."""
    art_dir = ctx.service.workspace(ticket).artifacts_dir
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "review.md").write_text(
        f"verdict: {verdict}\nauto_merge_eligible: {str(eligible).lower()}\n",
        encoding="utf-8",
    )


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
    """Artifact file doesn't exist → HUMAN_MR_APPROVAL."""
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
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


def test_auto_merge_skipped_when_not_eligible(tmp_path, monkeypatch):
    """Artifact says auto_merge_eligible: false → HUMAN_MR_APPROVAL."""
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
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


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
    """mergeable=False → IMPLEMENT_COMPLETE (silent fallback)."""
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


def test_merge_pr_failure_stays_human_mr_approval(tmp_path, monkeypatch):
    """merge_pr returns {'merged': False} → HUMAN_MR_APPROVAL (not BLOCKED)."""
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
    assert out.next_state is State.HUMAN_MR_APPROVAL


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


def test_eligible_forge_merge_failed_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """Eligible + CI success + forge rejects → HUMAN_MR_APPROVAL + comment."""
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
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "forge merge failed: branch protection" in (merge_events[0].note or "")


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

    assert "auto-merge disabled in config" in (merge_events[0].note or "")


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


def test_not_eligible_artifact_missing_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """No review artifact → HUMAN_MR_APPROVAL + comment."""
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

    t = _human_mr_approval(ctx)
    # NO review artifact

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "no review artifact" in (merge_events[0].note or "")


def test_not_eligible_flagged_false_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """auto_merge_eligible: false → HUMAN_MR_APPROVAL + comment."""
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "not auto-merge eligible" in (merge_events[0].note or "")


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


def test_waiting_auto_merge_becomes_human_when_eligibility_changes(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE poll where artifact now says not eligible
    → HUMAN_MR_APPROVAL."""
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
    # First write the artifact as eligible so the WAITING_AUTO_MERGE
    # transition is plausible.
    _write_review_artifact(ctx, t, eligible=True)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    # Now change the artifact to not eligible.
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]
    assert len(merge_events) == 1
    assert "not auto-merge eligible" in (merge_events[0].note or "")


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
    """WAITING_AUTO_MERGE + mergeable=False → IMPLEMENT_COMPLETE."""
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note

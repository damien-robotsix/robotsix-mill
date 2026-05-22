import pytest

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
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**env)
    db.init_db(s)
    return StageContext(settings=s, service=TicketService(s))


def _human_mr_approval(ctx):
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _in_rebasing(ctx):
    """Create a ticket already in REBASING state."""
    t = _human_mr_approval(ctx)
    ctx.service.transition(t.id, State.REBASING, note="PR conflicting")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    return _ctx(
        tmp_path, FORGE_KIND="github", FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git", **extra,
    )


# --- existing paths (unchanged) ---

def test_blocked_when_forge_unconfigured(tmp_path):
    ctx = _ctx(tmp_path)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED and "forge not configured" in out.note


def test_merged_to_done(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": True, "state": "closed",
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
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "closed", "url": "u",
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "closed without merge" in out.note


def test_open_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
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
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_open_mergeable_none_is_noop(tmp_path, monkeypatch):
    """mergeable=None (unchecked) → treat as mergeable, no rebase."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": None,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
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
            github.GitHubForge, "pr_status",
            lambda self, *, source_branch, m=mergeable: {
                "merged": False, "state": "open", "url": "u",
                "mergeable": m,
            },
        )
        out = MergeStage().run(_human_mr_approval(ctx), ctx)
        assert out.next_state is State.HUMAN_MR_APPROVAL
        assert "REBASING" not in str(out.next_state.value)


# --- New: conflicting PR on HUMAN_MR_APPROVAL → REBASING (detection only) ---

def test_conflicting_pr_transitions_to_rebasing(tmp_path, monkeypatch):
    """HUMAN_MR_APPROVAL + mergeable=False → REBASING, no rebase agent called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )

    agent_called = []

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )

    t = _human_mr_approval(ctx)
    # Even with a valid workspace clone, the rebase agent must NOT be called
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert agent_called == []  # rebase agent NOT invoked


# --- New: REBASING path — clean rebase → HUMAN_MR_APPROVAL ---

def test_rebasing_clean_rebase_returns_to_human_mr_approval(tmp_path, monkeypatch):
    """Ticket in REBASING → rebase agent succeeds → force-push → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert push_calls["branch"] == f"mill/{t.id}"


# --- REBASING: no-op rebase (branch already current) skips force-push ---

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
        "robotsix_mill.stages.merge.git_ops.head_sha", lambda repo: sha,
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
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="2")
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
        "robotsix_mill.stages.merge.git_ops.head_sha", lambda repo: sha,
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
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="3")
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
    assert out.next_state is State.REBASING  # retry, not HUMAN_MR_APPROVAL

    counter_path = (
        ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    )
    assert _read_counter(counter_path) == 1


# --- REBASING: exhausted → BLOCKED ---

def test_rebasing_exhausted_blocks(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt == max → Outcome(BLOCKED)."""
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="1")

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "rebase failed after 1 attempt" in out.note
    assert push_called == []  # never force-pushed on failure


# --- original conflicting PR tests, now running through REBASING state ---

def test_conflicting_pr_invokes_rebase_agent(tmp_path, monkeypatch):
    """Full cycle: HUMAN_MR_APPROVAL + mergeable=False → REBASING → then rebase on next poll."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )
    calls = {}

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        calls.update(repo_dir=repo_dir, branch=branch, target=target)
        return RebaseResult(status="DONE", summary="ok")  # success

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
    )

    t = _human_mr_approval(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Step 1: HUMAN_MR_APPROVAL + conflicting → REBASING (detection only).
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert calls == {}  # agent not called yet

    # Actually transition the ticket to REBASING.
    ctx.service.transition(t.id, State.REBASING, note="conflicting")
    t = ctx.service.get(t.id)

    # Step 2: REBASING → rebase agent runs, succeeds → HUMAN_MR_APPROVAL.
    out2 = MergeStage().run(t, ctx)
    assert calls["branch"] == f"mill/{t.id}"
    assert calls["target"] == "main"
    assert str(repo_dir) in calls["repo_dir"]
    assert push_calls["branch"] == f"mill/{t.id}"
    assert out2.next_state is State.HUMAN_MR_APPROVAL


def test_conflicting_pr_no_workspace_clone_blocks(tmp_path, monkeypatch):
    """If the workspace clone is missing in REBASING, cannot rebase → BLOCKED."""
    ctx = _gh(tmp_path)
    # No repo dir created — workspace is empty.
    t = _in_rebasing(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note


def test_rebase_failure_exhausts_attempts_then_blocks(tmp_path, monkeypatch):
    """Agent returns False for every attempt → BLOCKED after max (through REBASING)."""
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="2")

    agent_calls = []

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        agent_calls.append(1)
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="1")

    def boom(*, settings, repo_dir, branch, target):
        raise RuntimeError("LLM timeout")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", boom,
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
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="1")

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
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
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    def boom_push(repo, branch, remote_url, token):
        raise RuntimeError("remote rejected")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", boom_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "force-push failed" in out.note


def test_rebase_counter_resets_only_when_pr_becomes_mergeable(
    tmp_path, monkeypatch
):
    """A push is NOT proof the conflict is resolved (git rebase rewrites
    SHAs every run). The attempt counter must persist across rebase+push
    cycles and only reset to 0 when the HUMAN_MR_APPROVAL poll sees a mergeable
    PR — otherwise the loop is unbounded."""
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="3")

    call_count = [0]

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        call_count[0] += 1
        # First call fails, second succeeds.
        if call_count[0] == 2:
            return RebaseResult(status="DONE", summary="ok")
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    def fake_push(repo, branch, remote_url, token):
        pass

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    counter_path = (
        ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    )

    # Attempt 1 fails → counter=1, stays REBASING
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert _read_counter(counter_path) == 1

    # Attempt 2 succeeds+pushes → back to HUMAN_MR_APPROVAL, but counter is
    # PERSISTED (==2), NOT reset — a push doesn't prove resolution.
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(counter_path) == 2

    # Now the HUMAN_MR_APPROVAL poll sees a genuinely mergeable PR → the
    # conflict is really gone → counter resets to 0.
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "success"},
    )
    ctx.service.transition(t.id, State.HUMAN_MR_APPROVAL, note="rebased")
    out3 = MergeStage().run(ctx.service.get(t.id), ctx)
    assert out3.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(counter_path) == 0


def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    """The force-push must reference only the ticket's own branch."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target, memory=""):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_args = {}

    def fake_push(repo, branch, remote_url, token):
        push_args.update(branch=branch, remote_url=remote_url, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
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


def test_rebase_force_push_uses_minted_token_not_raw_forge_token(
    tmp_path, monkeypatch
):
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
        "robotsix_mill.stages.merge.github_token", lambda s: "MINTED-APP-TOK"
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

    assert seen.get("token") == "MINTED-APP-TOK"   # not the raw "t"


# ============================================================
# D. Merge-stage CI branching (new)
# ============================================================

def test_mergeable_failing_ci_transitions_to_fixing_ci(tmp_path, monkeypatch):
    """D.20: Mergeable PR + failing CI → FIXING_CI."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": None, "text": None, "annotations": []}],
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_mergeable_green_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """D.21: Mergeable PR + green CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_none_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """D.21: check_status returns None (no checks) → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: None,
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_pending_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """D.22: Mergeable PR + pending CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_check_status_exception_is_noop(tmp_path, monkeypatch):
    """D.23: check_status raises → transient re-poll."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_conflicting_pr_skips_check_status(tmp_path, monkeypatch):
    """D.24: Conflicting PR → rebase path; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return {"conclusion": "success", "failing": []}

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="FAILED", summary="nope"),
    )

    t = _human_mr_approval(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert check_calls == []  # never called for conflicting PR


def test_merged_pr_skips_check_status(tmp_path, monkeypatch):
    """D.25: Merged PR → DONE; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": True, "state": "closed", "url": "u",
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
    """D.26: Closed PR → BLOCKED; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "closed", "url": "u",
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
        "robotsix_mill.stages.merge.git_ops.fetch", fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
        "robotsix_mill.stages.merge.git_ops.fetch", fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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

    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="3")

    root_calls = []
    stage_calls = []

    @contextlib.contextmanager
    def fake_root(ticket_id, stage_name=None, extra_attributes=None):
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

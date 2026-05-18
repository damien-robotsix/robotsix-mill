import pytest

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


def _in_review(ctx):
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IN_REVIEW):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _in_rebasing(ctx):
    """Create a ticket already in REBASING state."""
    t = _in_review(ctx)
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
    out = MergeStage().run(_in_review(ctx), ctx)
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
    t = _in_review(ctx)
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
    out = MergeStage().run(_in_review(ctx), ctx)
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
    out = MergeStage().run(_in_review(ctx), ctx)
    assert out.next_state is State.IN_REVIEW  # same state = worker no-op


def test_transient_error_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)

    def boom(self, *, source_branch):
        raise RuntimeError("api down")

    monkeypatch.setattr(github.GitHubForge, "pr_status", boom)
    out = MergeStage().run(_in_review(ctx), ctx)
    assert out.next_state is State.IN_REVIEW  # retry next poll, not blocked


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
    out = MergeStage().run(_in_review(ctx), ctx)
    assert out.next_state is State.IN_REVIEW


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
    out = MergeStage().run(_in_review(ctx), ctx)
    assert out.next_state is State.IN_REVIEW


# --- New: mergeable PR never enters REBASING ---

def test_mergeable_pr_never_enters_rebasing(tmp_path, monkeypatch):
    """mergeable=True/None → OUTCOME(IN_REVIEW), never REBASING."""
    ctx = _gh(tmp_path)
    for mergeable in (True, None):
        monkeypatch.setattr(
            github.GitHubForge, "pr_status",
            lambda self, *, source_branch, m=mergeable: {
                "merged": False, "state": "open", "url": "u",
                "mergeable": m,
            },
        )
        out = MergeStage().run(_in_review(ctx), ctx)
        assert out.next_state is State.IN_REVIEW
        assert "REBASING" not in str(out.next_state.value)


# --- New: conflicting PR on IN_REVIEW → REBASING (detection only) ---

def test_conflicting_pr_transitions_to_rebasing(tmp_path, monkeypatch):
    """IN_REVIEW + mergeable=False → REBASING, no rebase agent called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )

    agent_called = []

    def fake_rebase(*, settings, repo_dir, branch, target):
        agent_called.append(1)
        return True

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )

    t = _in_review(ctx)
    # Even with a valid workspace clone, the rebase agent must NOT be called
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert agent_called == []  # rebase agent NOT invoked


# --- New: REBASING path — clean rebase → IN_REVIEW ---

def test_rebasing_clean_rebase_returns_to_in_review(tmp_path, monkeypatch):
    """Ticket in REBASING → rebase agent succeeds → force-push → IN_REVIEW."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target):
        return True

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
    assert out.next_state is State.IN_REVIEW
    assert push_calls["branch"] == f"mill/{t.id}"


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

    def fake_rebase(*, settings, repo_dir, branch, target):
        return False

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING  # retry, not IN_REVIEW

    counter_path = (
        ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    )
    assert _read_counter(counter_path) == 1


# --- REBASING: exhausted → BLOCKED ---

def test_rebasing_exhausted_blocks(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt == max → Outcome(BLOCKED)."""
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="1")

    def fake_rebase(*, settings, repo_dir, branch, target):
        return False

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
    """Full cycle: IN_REVIEW + mergeable=False → REBASING → then rebase on next poll."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )
    calls = {}

    def fake_rebase(*, settings, repo_dir, branch, target):
        calls.update(repo_dir=repo_dir, branch=branch, target=target)
        return True  # success

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
    )
    push_calls = {}

    def fake_push(repo, branch, remote_url, token):
        push_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push", fake_push,
    )

    t = _in_review(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Step 1: IN_REVIEW + conflicting → REBASING (detection only).
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert calls == {}  # agent not called yet

    # Actually transition the ticket to REBASING.
    ctx.service.transition(t.id, State.REBASING, note="conflicting")
    t = ctx.service.get(t.id)

    # Step 2: REBASING → rebase agent runs, succeeds → IN_REVIEW.
    out2 = MergeStage().run(t, ctx)
    assert calls["branch"] == f"mill/{t.id}"
    assert calls["target"] == "main"
    assert str(repo_dir) in calls["repo_dir"]
    assert push_calls["branch"] == f"mill/{t.id}"
    assert out2.next_state is State.IN_REVIEW


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

    def fake_rebase(*, settings, repo_dir, branch, target):
        agent_calls.append(1)
        return False

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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

    def fake_rebase(*, settings, repo_dir, branch, target):
        return False

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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

    def fake_rebase(*, settings, repo_dir, branch, target):
        return True

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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


def test_rebase_attempt_counter_resets_on_success(tmp_path, monkeypatch):
    """After a successful rebase+push, the attempt counter resets to 0."""
    ctx = _gh(tmp_path, MILL_REBASE_MAX_ATTEMPTS="3")

    call_count = [0]

    def fake_rebase(*, settings, repo_dir, branch, target):
        call_count[0] += 1
        # First call fails, second succeeds.
        return call_count[0] == 2

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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

    # Attempt 2 succeeds → counter reset to 0, back to IN_REVIEW
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.IN_REVIEW
    assert _read_counter(counter_path) == 0


def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    """The force-push must reference only the ticket's own branch."""
    ctx = _gh(tmp_path)

    def fake_rebase(*, settings, repo_dir, branch, target):
        return True

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent", fake_rebase,
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
    """Regression: merge's post-rebase force-push passed s.forge_token
    (empty in App mode) instead of github_token() -> unauthenticated
    push -> git exit 128 -> ticket BLOCKED. It must use the minted
    token like deliver does."""
    ctx = _gh(tmp_path)  # FORGE_TOKEN="t" (raw); minted token differs
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "u",
            "mergeable": False,
        },
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: True,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.github_token", lambda s: "MINTED-APP-TOK"
    )
    seen = {}
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.push",
        lambda repo, branch, remote_url, token: seen.update(token=token),
    )

    t = _in_review(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    assert seen.get("token") == "MINTED-APP-TOK"   # not the raw "t"

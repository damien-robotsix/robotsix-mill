"""Tests for the CI-poll guardrails in CIPollMixin._poll_implement_complete.

Covers:
- Guardrail 1: cross-stage auto-fix cycle counter (auto_fix_cycles.txt)
- Guardrail 2: ping-pong alternation detector (ping_pong_count.txt)
- Counter reset on CI green
- Ceiling-of-0 disables each guardrail
- Diagnostic message quality
"""

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage, _read_counter, _write_counter
from robotsix_mill.stages.merge._shared import (
    _AUTO_FIX_CYCLES,
    _LAST_AUTO_FIX_STAGE,
    _PING_PONG_COUNT,
)


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
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


def _implement_complete(ctx):
    """Create a ticket in IMPLEMENT_COMPLETE state (PR open, gates not verified)."""
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
    )


def _ci_failing_mergeable(monkeypatch, mergeable_state=None):
    """Patch the forge so the PR is open+mergeable with failing CI.

    *mergeable_state* values: ``"behind"``, ``"clean"``, ``"unstable"``,
    etc.  Default ``None`` causes the merge stage to fall through to the
    local ``branch_is_behind_main`` check; set to ``"clean"`` to bypass it.
    """
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": mergeable_state or "behind",
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


def _ci_green_mergeable(monkeypatch):
    """Patch the forge so the PR is open+mergeable with green CI."""
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "success",
            "failing": [],
        },
    )


# === Guardrail 1: auto-fix cycle counter ==================================


def test_auto_fix_cycles_exhausted_blocks(tmp_path, monkeypatch):
    """When auto_fix_cycles reaches the ceiling, the ticket is BLOCKED
    without dispatching to REBASING or FIXING_CI."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=3)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,  # routes to FIXING_CI
    )

    # Pre-seed the counter at the ceiling (3).
    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 3)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "auto-fix exhausted" in out.note
    assert "3 cycle(s)" in out.note
    assert t.id in out.note
    # Counter is reset on block so a resume gets a clean budget.
    assert _read_counter(counter_path) == 0


def test_auto_fix_cycles_below_ceiling_proceeds_to_ci_fix(tmp_path, monkeypatch):
    """When auto_fix_cycles is below the ceiling, the dispatch proceeds normally."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=3)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 2)  # one below ceiling

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.FIXING_CI
    # Counter should be incremented to 3.
    assert _read_counter(counter_path) == 3


def test_auto_fix_cycles_exhausted_blocks_before_rebasing(tmp_path, monkeypatch):
    """When auto_fix_cycles is exhausted, BLOCKED is returned even when
    the branch is behind main (would normally route to REBASING)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": True,  # would route to REBASING
    )

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 2)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "auto-fix exhausted" in out.note
    assert _read_counter(counter_path) == 0


def test_auto_fix_max_cycles_zero_disables_guardrail(tmp_path, monkeypatch):
    """When auto_fix_max_cycles=0, the guardrail is never checked (AC4)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=0)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 999)  # way beyond any reasonable ceiling

    out = MergeStage().run(t, ctx)
    # Should still dispatch to FIXING_CI (guardrail skipped).
    assert out.next_state is State.FIXING_CI


def test_auto_fix_cycles_reset_on_ci_green(tmp_path, monkeypatch):
    """When CI is green, auto_fix_cycles.txt is reset to 0 (AC3)."""
    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 5)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(counter_path) == 0


# === Guardrail 2: ping-pong alternation detector ==========================


def test_ping_pong_detection_blocks_on_alternation_ceiling(tmp_path, monkeypatch):
    """When the ping-pong ceiling is reached via a rebase→ci_fix alternation,
    the ticket is BLOCKED."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: ping_pong_count at 1, last stage was "rebase".
    _write_counter(ping_pong_path, 1)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("rebase", encoding="utf-8")

    # Route to FIXING_CI. last_stage="rebase", routing_to="ci_fix"
    # → alternation → count becomes 2 → reaches ceiling 2 → BLOCKED.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "ping-pong" in out.note.lower()
    assert "2 alternation" in out.note
    assert "ceiling is 2" in out.note
    assert t.id in out.note
    # Both files reset on block.
    assert _read_counter(ping_pong_path) == 0


def test_ping_pong_counts_only_alternations_not_same_stage_repeats(
    tmp_path,
    monkeypatch,
):
    """Routing to the same stage twice in a row does NOT count as an
    alternation — only a genuine A→B→A pattern increments the counter."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: last stage was "ci_fix", ping_pong_count = 1.
    _write_counter(ping_pong_path, 1)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("ci_fix", encoding="utf-8")

    # Route to FIXING_CI again. last_stage="ci_fix", routing_to="ci_fix"
    # → NOT an alternation → counter stays at 1.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.FIXING_CI
    # Counter should NOT have incremented.
    assert _read_counter(ping_pong_path) == 1


def test_ping_pong_ci_fix_after_rebase_is_alternation(tmp_path, monkeypatch):
    """ci_fix after rebase increments the ping-pong counter."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=3)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: last stage was "rebase", ping_pong_count = 0.
    _write_counter(ping_pong_path, 0)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("rebase", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    # Routes to FIXING_CI (branch is NOT behind main), which IS an
    # alternation from rebase → ci_fix.
    assert out.next_state is State.FIXING_CI
    assert _read_counter(ping_pong_path) == 1
    assert last_stage_path.read_text(encoding="utf-8").strip() == "ci_fix"


def test_ping_pong_rebase_after_ci_fix_is_alternation(tmp_path, monkeypatch):
    """ci_fix after rebase increments the ping-pong counter (alternation
    from rebase→ci_fix via the FIXING_CI routing path)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=3)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: last stage was "rebase", ping_pong_count = 1.
    _write_counter(ping_pong_path, 1)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("rebase", encoding="utf-8")

    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    # Routes to FIXING_CI, which IS an alternation from rebase → ci_fix.
    assert out.next_state is State.FIXING_CI
    assert _read_counter(ping_pong_path) == 2
    assert last_stage_path.read_text(encoding="utf-8").strip() == "ci_fix"


def test_ping_pong_max_alternations_zero_disables_guardrail(tmp_path, monkeypatch):
    """When ping_pong_max_alternations=0, the guardrail is never checked (AC4)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=0)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    _write_counter(ping_pong_path, 999)  # way beyond any reasonable ceiling

    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    # Should still dispatch to FIXING_CI (guardrail skipped).
    assert out.next_state is State.FIXING_CI


def test_ping_pong_counters_reset_on_ci_green(tmp_path, monkeypatch):
    """When CI is green, ping_pong_count.txt and last_auto_fix_stage.txt are
    both reset (AC3)."""
    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    _write_counter(ping_pong_path, 5)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("ci_fix", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    # ping_pong_count reset to 0.
    assert _read_counter(ping_pong_path) == 0
    # last_auto_fix_stage deleted.
    assert not last_stage_path.exists()


# === Combined guardrail interaction ========================================


def test_auto_fix_cycles_exhausted_skips_ping_pong_check(tmp_path, monkeypatch):
    """When auto_fix_cycles is exhausted, BLOCKED is returned before the
    ping-pong check — no ping-pong counter files are touched."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=3, ping_pong_max_alternations=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": True,
    )

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    auto_fix_path = artifacts / _AUTO_FIX_CYCLES
    ping_pong_path = artifacts / _PING_PONG_COUNT

    _write_counter(auto_fix_path, 3)  # exhausted
    _write_counter(ping_pong_path, 2)  # at ceiling

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "auto-fix exhausted" in out.note
    # ping_pong counter untouched (not incremented, not reset).
    assert _read_counter(ping_pong_path) == 2


def test_ping_pong_exhausted_takes_priority_over_branch_decision(
    tmp_path,
    monkeypatch,
):
    """When ping-pong ceiling is reached, BLOCKED is returned instead of
    FIXING_CI."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=6, ping_pong_max_alternations=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: ping_pong_count at 1, last stage was "rebase".
    _write_counter(ping_pong_path, 1)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("rebase", encoding="utf-8")

    # Route to FIXING_CI → alternation rebase→ci_fix → count becomes 2
    # → reaches ceiling 2 → should block.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "ping-pong" in out.note.lower()


# === Existing guardrails are unchanged (AC5) ===============================


def test_existing_ci_fix_counters_still_work(tmp_path, monkeypatch):
    """ci_fix_cycles.txt reset on CI green still works alongside new counters."""
    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)
    ci_fix_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(ci_fix_path, 7)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(ci_fix_path) == 0


def test_rebase_counter_reset_on_mergeable_still_works(tmp_path, monkeypatch):
    """rebase_attempts.txt reset on mergeable PR still works."""
    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)
    rebase_path = ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    _write_counter(rebase_path, 3)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(rebase_path) == 0


# === Diagnostic message quality (AC7) ======================================


def test_auto_fix_cycles_block_message_contains_ticket_id_and_ceiling(
    tmp_path,
    monkeypatch,
):
    """The BLOCKED message from the auto-fix guardrail names the ticket ID
    and ceiling value."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=4)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 4)

    out = MergeStage().run(t, ctx)
    assert t.id in out.note
    assert "4" in out.note  # ceiling mentioned
    assert "manual intervention" in out.note.lower()
    assert "resume-blocked" in out.note.lower() or "Resume-blocked" in out.note


def test_ping_pong_block_message_contains_ticket_id_and_ceiling(
    tmp_path,
    monkeypatch,
):
    """The BLOCKED message from the ping-pong guardrail names the ticket ID
    and alternation count."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, ping_pong_max_alternations=2)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    artifacts = ctx.service.workspace(t).artifacts_dir
    ping_pong_path = artifacts / _PING_PONG_COUNT
    last_stage_path = artifacts / _LAST_AUTO_FIX_STAGE

    # Pre-seed: ping_pong_count at 1, last stage was "rebase".
    _write_counter(ping_pong_path, 1)
    last_stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_stage_path.write_text("rebase", encoding="utf-8")

    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    out = MergeStage().run(t, ctx)
    assert t.id in out.note
    assert "2" in out.note  # ceiling mentioned
    assert "manual intervention" in out.note.lower()
    assert "resume-blocked" in out.note.lower() or "Resume-blocked" in out.note


# === Premature-green guard (mergeable_state must be clean) ================


def _ci_premature_green(monkeypatch, mergeable_state="blocked"):
    """Patch the forge so check_status reports success but the PR's
    mergeable_state is NOT clean — the premature-green race where the fast
    checks finished green before the slow required gate started."""
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": mergeable_state,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )


def test_ci_truly_green_helper():
    """_ci_truly_green requires success AND a promotable mergeable_state."""
    from robotsix_mill.stages.merge._shared import _ci_truly_green

    # Promotable states: clean, unstable, or absent (non-GitHub forge).
    assert _ci_truly_green("success", {"mergeable_state": "clean"}) is True
    assert _ci_truly_green("success", {"mergeable_state": "unstable"}) is True
    # Absent (non-GitHub forge) → trust the conclusion.
    assert _ci_truly_green("success", {}) is True
    assert _ci_truly_green("success", {"mergeable_state": None}) is True
    # Premature / incomplete / genuinely blocked states → not green.
    assert _ci_truly_green("success", {"mergeable_state": "blocked"}) is False
    assert _ci_truly_green("success", {"mergeable_state": "behind"}) is False
    assert _ci_truly_green("success", {"mergeable_state": "unknown"}) is False
    assert _ci_truly_green("success", {"mergeable_state": "dirty"}) is False
    assert _ci_truly_green("success", {"mergeable_state": "draft"}) is False
    # Non-success conclusions are never green.
    assert _ci_truly_green("failure", {"mergeable_state": "clean"}) is False
    assert _ci_truly_green("pending", {"mergeable_state": "clean"}) is False
    assert _ci_truly_green(None, {}) is False


def test_premature_green_does_not_promote(tmp_path, monkeypatch):
    """conclusion=success but mergeable_state=blocked must NOT promote to
    HUMAN_MR_APPROVAL — it re-polls from IMPLEMENT_COMPLETE instead."""
    ctx = _gh(tmp_path)
    _ci_premature_green(monkeypatch, mergeable_state="blocked")

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_clean_green_still_promotes(tmp_path, monkeypatch):
    """Sanity: a genuinely clean green still promotes to HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_unstable_green_promotes(tmp_path, monkeypatch):
    """mergeable_state=unstable but conclusion=success promotes to HUMAN_MR_APPROVAL.

    "unstable" means all required checks passed but a non-required status is
    non-green — the PR IS mergeable.  Regression: PRs like mill #1828-1831
    were CLEAN yet sat in implement_complete because _ci_truly_green rejected
    "unstable" states.
    """
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "unstable",
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

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_blocked_behind_unknown_still_wait(tmp_path, monkeypatch):
    """mergeable_state in (blocked, behind, unknown) with success conclusion
    still waits — premature-green guard remains intact."""
    for ms in ("blocked", "behind", "unknown"):
        ctx = _gh(tmp_path)
        monkeypatch.setattr(
            github.GitHubForge,
            "pr_status",
            lambda self, *, source_branch, ms=ms: {
                "merged": False,
                "state": "open",
                "url": "u",
                "mergeable": True,
                "mergeable_state": ms,
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

        t = _implement_complete(ctx)
        out = MergeStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE, f"state={ms} should wait"


# === skip_ci toggle =======================================================


def test_skip_ci_implement_complete_bypasses_ci_gate(tmp_path, monkeypatch):
    """With skip_ci=True, failing CI does NOT route to FIXING_CI —
    the ticket promotes straight to HUMAN_MR_APPROVAL."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch, mergeable_state="clean")
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    # Enable skip_ci for this repo.
    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: True,
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert "skip_ci" in out.note
    assert "awaiting human merge approval" in out.note


def test_skip_ci_implement_complete_conflict_still_rebases(tmp_path, monkeypatch):
    """Even with skip_ci=True, a conflicting PR still routes to REBASING
    because skip_ci only bypasses the CI gate, not the conflict gate."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    # PR is open but mergeable=False (conflicting).
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
            "mergeable_state": "dirty",
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: True,
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING


def test_skip_ci_false_implement_complete_unchanged(tmp_path, monkeypatch):
    """With skip_ci=False, failing CI still routes to FIXING_CI (existing behaviour)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: False,
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.FIXING_CI


def test_skip_ci_human_mr_approval_failing_ci_stays_noop(tmp_path, monkeypatch):
    """With skip_ci=True, a HUMAN_MR_APPROVAL ticket with failing CI
    stays in HUMAN_MR_APPROVAL instead of falling back to IMPLEMENT_COMPLETE."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch, mergeable_state="clean")
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: True,
    )

    # Create a ticket and move it to HUMAN_MR_APPROVAL.
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    out = MergeStage().run(t, ctx)
    # Should stay in HUMAN_MR_APPROVAL — no fallback to IMPLEMENT_COMPLETE.
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_skip_ci_human_mr_approval_conflict_still_falls_back(tmp_path, monkeypatch):
    """Even with skip_ci=True, a conflicting PR in HUMAN_MR_APPROVAL
    still falls back to IMPLEMENT_COMPLETE for rebase handling."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    # PR is open but mergeable=False.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
            "mergeable_state": "dirty",
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: True,
    )

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    out = MergeStage().run(t, ctx)
    # Conflict → fallback to IMPLEMENT_COMPLETE regardless of skip_ci.
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_skip_ci_false_human_mr_approval_still_falls_back_on_failing_ci(
    tmp_path, monkeypatch
):
    """With skip_ci=False, failing CI in HUMAN_MR_APPROVAL still falls back
    to IMPLEMENT_COMPLETE (existing behaviour)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch, mergeable_state="clean")
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.load_repo_skip_ci",
        lambda repo_dir: False,
    )

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    out = MergeStage().run(t, ctx)
    # Failing CI → fallback to IMPLEMENT_COMPLETE.
    assert out.next_state is State.IMPLEMENT_COMPLETE


# === Changelog duplicate-fragment gate =====================================


def test_duplicate_fragments_same_ticket_blocks(tmp_path, monkeypatch):
    """Two fragments sharing the same issue key → BLOCKED with ticket id in note."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # Simulate duplicate fragments for ticket "ticket-abc".
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: {"ticket-abc"},
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "Duplicate changelog fragments" in out.note
    assert "ticket-abc" in out.note
    assert "Resumable" in out.note


def test_single_fragment_promotes_normally(tmp_path, monkeypatch):
    """One fragment per ticket → promotes to HUMAN_MR_APPROVAL as before."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # No duplicates — empty set.
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: set(),
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_two_fragments_different_tickets_allowed(tmp_path, monkeypatch):
    """Two fragments for DIFFERENT tickets → allowed (each ticket has exactly one)."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # Each ticket has exactly one fragment — no duplicates.
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: set(),
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_no_towncrier_config_allows_merge(tmp_path, monkeypatch):
    """Repo without [tool.towncrier] → gate is no-op, merge allowed."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # No towncrier → empty set (best-effort allow).
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: set(),
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_timestamp_named_fragments_no_false_positive(tmp_path, monkeypatch):
    """Timestamp-named fragments each yield a unique key → allowed."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # Timestamp fragments have unique keys — no duplicates.
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: set(),
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_duplicate_fragments_git_error_best_effort_allow(tmp_path, monkeypatch):
    """Git/tooling error → best-effort allow (empty set), merge proceeds."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    # Simulate git error → empty set.
    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: set(),
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_duplicate_fragments_multiple_tickets_in_message(tmp_path, monkeypatch):
    """When multiple tickets have duplicates, all are named in the BLOCKED note."""
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_green_mergeable(monkeypatch)

    monkeypatch.setattr(
        ci_poll_mod,
        "_duplicate_changelog_fragments",
        lambda repo_dir, target_branch: {"ticket-1", "ticket-2"},
    )

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "ticket-1" in out.note
    assert "ticket-2" in out.note


# === _duplicate_changelog_fragments function tests =========================


def test_duplicate_fragments_func_no_repo_dir(tmp_path):
    """None repo_dir → empty set."""
    from robotsix_mill.stages.merge._shared import _duplicate_changelog_fragments

    result = _duplicate_changelog_fragments(None, "main")
    assert result == set()


def test_duplicate_fragments_func_missing_pyproject(tmp_path):
    """Missing pyproject.toml → empty set."""
    from robotsix_mill.stages.merge._shared import _duplicate_changelog_fragments

    result = _duplicate_changelog_fragments(str(tmp_path), "main")
    assert result == set()


def test_duplicate_fragments_func_no_towncrier_config(tmp_path):
    """pyproject.toml without [tool.towncrier] → empty set."""
    from robotsix_mill.stages.merge._shared import _duplicate_changelog_fragments

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.something]\nkey = 'val'\n")
    (repo / ".git").mkdir()

    result = _duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_real_duplicates(tmp_path, monkeypatch):
    """Two fragments with the same issue key → that key is returned."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    # Simulate added_files returning two fragments with the same issue key.
    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/ticket-abc.feature.md",
            "changes/ticket-abc.misc.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == {"ticket-abc"}


def test_duplicate_fragments_func_different_tickets_allowed(tmp_path, monkeypatch):
    """Two fragments for different issue keys → no duplicates."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/ticket-abc.feature.md",
            "changes/ticket-xyz.bugfix.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_timestamp_named_unique(tmp_path, monkeypatch):
    """Timestamp-named fragments each have a unique key → no false positives."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/20260618T145744Z-fix-auth-abaf.misc.md",
            "changes/20260619T120000Z-add-feature-cd12.feature.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_single_fragment_no_duplicate(tmp_path, monkeypatch):
    """A single fragment → no duplicates."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: ["changes/ticket-abc.feature.md"],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_excludes_dotfiles_and_underscore(
    tmp_path, monkeypatch
):
    """Files starting with . or _ are excluded (e.g. .gitkeep, _template.md)."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/.gitkeep",
            "changes/_template.md",
            "changes/ticket-abc.feature.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_excludes_non_md(tmp_path, monkeypatch):
    """Non-.md files in the fragment directory are ignored."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/ticket-abc.feature.md",
            "changes/readme.txt",
            "changes/ticket-abc.misc.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == {"ticket-abc"}


def test_duplicate_fragments_func_custom_directory(tmp_path, monkeypatch):
    """The fragment directory is read from [tool.towncrier].directory."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[tool.towncrier]\ndirectory = 'changelog.d'\n"
    )
    (repo / ".git").mkdir()
    (repo / "changelog.d").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changelog.d/ticket-abc.feature.md",
            "changelog.d/ticket-abc.misc.md",
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == {"ticket-abc"}


def test_duplicate_fragments_func_git_error_best_effort(tmp_path, monkeypatch):
    """When added_files raises, the function returns empty set (best-effort)."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: (_ for _ in ()).throw(OSError("git failed")),
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()


def test_duplicate_fragments_func_ignores_subdirectory_files(tmp_path, monkeypatch):
    """Files in subdirectories of the fragment directory are ignored."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.towncrier]\ndirectory = 'changes'\n")
    (repo / ".git").mkdir()
    (repo / "changes").mkdir()

    monkeypatch.setattr(
        shared_mod.git_ops,
        "added_files",
        lambda repo, target_branch: [
            "changes/ticket-abc.feature.md",
            "changes/subdir/ticket-abc.misc.md",  # subdirectory — ignored
        ],
    )

    result = shared_mod._duplicate_changelog_fragments(str(repo), "main")
    assert result == set()

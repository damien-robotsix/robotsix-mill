"""Tests for the CI-poll guardrails in CIPollMixin._poll_implement_complete.

Covers:
- Guardrail 1: cross-stage auto-fix cycle counter (auto_fix_cycles.txt)
- Guardrail 2: ping-pong alternation detector (ping_pong_count.txt)
- Counter reset on CI green
- Ceiling-of-0 disables each guardrail
- Diagnostic message quality
"""

import datetime

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.merge import MergeStage, _read_counter, _write_counter
from robotsix_mill.stages.merge._shared import (
    _AUTO_FIX_CYCLES,
    _LAST_AUTO_FIX_STAGE,
    _PING_PONG_COUNT,
    _REBASE_LAST_TS,
)


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
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


def test_green_behind_routes_to_rebasing(tmp_path, monkeypatch):
    """conclusion=success + mergeable_state=behind → REBASING.

    Under a strict up-to-date branch policy a green PR behind the target
    stays "behind" forever — no re-poll changes it. The gate check must
    dispatch the rebase agent to catch the branch up instead of waiting.
    Regression: six chat PRs sat in implement_complete/waiting_auto_merge
    indefinitely, each auto-merge stranding the survivors further behind.
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

    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert "behind" in (out.note or "")


def test_blocked_unknown_still_wait(tmp_path, monkeypatch):
    """mergeable_state in (blocked, unknown) with success conclusion
    still waits — premature-green guard remains intact."""
    for ms in ("blocked", "unknown"):
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
    now routes directly to REBASING (autonomous rebase) instead of
    the old IMPLEMENT_COMPLETE intermediate hop."""
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
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
    # Conflict → REBASING (autonomous rebase enabled by default).
    assert out.next_state is State.REBASING


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


# ---------------------------------------------------------------------------
# Tracker ticket PR-baseline guard
# ---------------------------------------------------------------------------


def test_tracker_ticket_merged_pr_blocks(tmp_path, monkeypatch):
    """When pr_status returns None for a tracker ticket but the tracked PR
    (extracted from the description) is merged, the outcome is BLOCKED."""
    from robotsix_mill.core.models import SourceKind

    ctx = _gh(tmp_path)

    t = ctx.service.create(
        title="Track external PR: test-repo#42",
        description="Tracked PR for testing.\n\n- URL: https://github.com/owner/repo/pull/42",
        source=SourceKind.ORPHANED_PR_CHECK,
    )
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    t = ctx.service.get(t.id)

    # Write the description into the workspace so read_description() finds it.
    ws = ctx.service.workspace(t)
    ws.description_path.write_text(
        "Tracked PR for testing.\n\n- URL: https://github.com/owner/repo/pull/42",
        encoding="utf-8",
    )

    # No PR on the mill branch, but the tracked PR is merged.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status_by_url",
        lambda self, *, url: {"merged": True, "state": "closed", "url": url},
    )

    outcome = MergeStage()._poll_implement_complete(t, ctx)
    assert outcome.next_state is State.BLOCKED
    assert "merged" in outcome.note.lower()


# === Pre-existing target-branch CI debt: ci_fix exemption ==================


def _main_debt(monkeypatch):
    """Patch the forge so the PR's only failing workflow also fails on main.

    That is exactly the shape ``_main_branch_ci_debt`` treats as
    pre-existing debt: every workflow failing on the PR head is failing on
    the merge target too.
    """
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha=None, branch=None: [
            {
                "name": "CI",
                "workflow_id": 1,
                "conclusion": "failure",
                "created_at": "2026-01-01T00:00:00Z",
                # A push run only counts as a PR check when head_branch is
                # set (a tag push is a release, not a check).
                "event": "pull_request" if head_sha else "push",
                "head_branch": "main",
            }
        ],
    )


def _debt_ctx(tmp_path, monkeypatch):
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_merge_main_debt_detection_enabled=True)
    _ci_failing_mergeable(monkeypatch, mergeable_state="clean")
    # The debt check keys off the PR head sha; without it the helper
    # short-circuits to "no debt" and the guard never fires.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "sha": "deadbeef",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    _main_debt(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    return ctx


def _implement_complete_with_source(ctx, source):
    t = ctx.service.create("x", "y", source=source)
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def test_main_debt_blocks_an_ordinary_ticket(tmp_path, monkeypatch):
    """A normal ticket whose CI failure is pure main debt is still BLOCKED.

    Pins the guard itself so the ci_fix exemption below can't be mistaken
    for the detection having simply stopped working.
    """
    ctx = _debt_ctx(tmp_path, monkeypatch)
    t = _implement_complete_with_source(ctx, SourceKind.USER)

    out = MergeStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "pre-existing target-branch debt" in out.note


def test_main_debt_does_not_block_a_ci_fix_ticket(tmp_path, monkeypatch):
    """A ci_fix dependency ticket is exempt from the main-debt guard.

    The ticket exists to repair that exact debt, so blocking it on the
    debt deadlocks the board: the repair for red main is refused because
    main is red. It must fall through to the bounded auto-fix loop.
    """
    ctx = _debt_ctx(tmp_path, monkeypatch)
    t = _implement_complete_with_source(ctx, SourceKind.CI_FIX_DEPENDENCY)

    out = MergeStage().run(t, ctx)

    assert out.next_state is not State.BLOCKED
    assert out.next_state is State.FIXING_CI


def test_main_debt_does_not_block_a_ci_sourced_ticket(tmp_path, monkeypatch):
    """A ``ci``-sourced ticket is exempt for the same reason.

    The CI monitor files "CI failure: <workflow> on main" tickets whose
    whole purpose is to turn the target branch green again.  Blocking one
    on the very debt it repairs stalls every sibling behind it —
    robotsix-ui #56 fixed main's lint and typecheck and was refused
    because main's lint and typecheck were red.
    """
    ctx = _debt_ctx(tmp_path, monkeypatch)
    t = _implement_complete_with_source(ctx, SourceKind.CI)

    out = MergeStage().run(t, ctx)

    assert out.next_state is not State.BLOCKED
    assert out.next_state is State.FIXING_CI


# === Branch refresh before CI evaluation ===================================


def test_branch_refreshed_before_ci_check_in_poll_implement_complete(
    tmp_path, monkeypatch
):
    """_poll_implement_complete calls _refresh_branch_for_ci before check_status.

    When resuming from BLOCKED the branch may carry a stale SHA whose CI
    run was already failing.  The refresh (rebase + optional empty commit)
    must happen BEFORE CI is evaluated so a fresh run is triggered.
    """
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch, mergeable_state="clean")

    # Record call order.
    call_order: list[str] = []

    def _tracking_refresh(*a, **kw):
        call_order.append("refresh")
        return True

    monkeypatch.setattr(ci_poll_mod, "_refresh_branch_for_ci", _tracking_refresh)
    monkeypatch.setattr(ci_poll_mod, "_workspace_repo_dir", lambda c, t: "/repo")

    # Also track check_status calls.
    _orig_cs = github.GitHubForge.check_status

    def _tracking_cs(self, *, source_branch, require_checks=False):
        call_order.append("check_status")
        return _orig_cs(self, source_branch=source_branch)

    monkeypatch.setattr(github.GitHubForge, "check_status", _tracking_cs)

    t = _implement_complete(ctx)
    # Point workspace repo dir to our real tmp dir.
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    monkeypatch.setattr(
        ci_poll_mod,
        "_workspace_repo_dir",
        lambda c, tkt: str(repo_path) if tkt.id == t.id else None,
    )

    MergeStage().run(t, ctx)

    # A concluded (failing) run must still be refreshed, and the refresh must
    # land before the status the stage ACTS on is read — otherwise a stale
    # failing SHA gets re-diagnosed as a real failure instead of re-run.
    assert "refresh" in call_order, f"refresh not called; order={call_order}"
    assert call_order[-1] == "check_status", (
        f"the status the stage acts on must be read after the refresh; "
        f"got order={call_order}"
    )
    assert call_order.index("refresh") < len(call_order) - 1, (
        f"_refresh_branch_for_ci must precede the acted-on check_status; "
        f"got order={call_order}"
    )


def test_no_branch_refresh_while_ci_is_still_running(tmp_path, monkeypatch):
    """A refresh must never fire while checks are in flight.

    Regression (2026-08-01): the refresh ran unconditionally on every poll.
    A new head SHA makes the forge abandon the in-progress run and start a
    fresh one, and the poll interval (120s) is far shorter than a CI run — so
    each poll restarted the checks it was waiting on and no run could ever
    conclude. 472 empty commits in one hour across 15 tickets; one looped for
    20 hours restarting 18 checks every 2 minutes, and 22 tickets stacked up
    in IMPLEMENT_COMPLETE because none could finish CI.
    """
    from robotsix_mill.stages.merge import ci_poll as ci_poll_mod

    ctx = _gh(tmp_path)

    # CI reports checks still pending — nothing has concluded.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "mergeable_state": "blocked",
            "pending": ["test", "lint"],
        },
    )

    refreshed: list[str] = []
    monkeypatch.setattr(
        ci_poll_mod,
        "_refresh_branch_for_ci",
        lambda *a, **kw: refreshed.append("refresh") or True,
    )

    t = _implement_complete(ctx)
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    (repo_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(
        ci_poll_mod,
        "_workspace_repo_dir",
        lambda c, tkt: str(repo_path) if tkt.id == t.id else None,
    )

    out = MergeStage().run(t, ctx)

    assert refreshed == [], "must not push a new SHA while checks are running"
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_ci_run_in_flight_predicate():
    """Anything short of a real conclusion counts as in-flight."""
    from robotsix_mill.stages.merge.ci_poll import _ci_run_in_flight

    assert _ci_run_in_flight({"pending": ["test"], "conclusion": "failure"}) is True
    assert _ci_run_in_flight({"conclusion": "pending"}) is True
    assert _ci_run_in_flight({"conclusion": "in_progress"}) is True
    assert _ci_run_in_flight({"conclusion": None}) is True
    assert _ci_run_in_flight({}) is True
    assert _ci_run_in_flight({"conclusion": "failure"}) is False
    assert _ci_run_in_flight({"conclusion": "success"}) is False


def test_stale_failing_run_replaced_by_fresh_green_after_refresh(tmp_path, monkeypatch):
    """Regression: a stale failing CI run on an unchanged branch is
    superseded by a fresh green run after the branch is refreshed.

    The forge's check_status is called exactly once after the refresh,
    and the fresh result (green) advances the ticket rather than
    re-reading the old stale failure.
    """
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda c, t: "/repo")

    # Create a real .git dir so the refresh guard passes.
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    monkeypatch.setattr(
        merge_mod,
        "_workspace_repo_dir",
        lambda c, tkt: str(repo_path),
    )

    # Mock git ops for the refresh.
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda p: "abc123")
    monkeypatch.setattr(merge_mod.git_ops, "ls_remote_sha", lambda *a, **kw: "abc123")
    monkeypatch.setattr(merge_mod.git_ops, "try_rebase_onto", lambda *a, **kw: False)
    monkeypatch.setattr(merge_mod.git_ops, "empty_commit", lambda *a, **kw: None)
    monkeypatch.setattr(merge_mod.git_ops, "push", lambda *a, **kw: None)

    # The forge returns GREEN CI — as if the refresh triggered a new
    # run that passed (the transient flake resolved).
    _ci_green_mergeable(monkeypatch)

    t = _implement_complete(ctx)

    out = MergeStage().run(t, ctx)

    # The ticket should advance past CI to human approval.
    assert out.next_state is State.HUMAN_MR_APPROVAL, (
        f"Expected HUMAN_MR_APPROVAL after fresh green CI, got {out.next_state}: {out.note}"
    )


# --- the CI refresh is bounded to one empty commit per branch head ----


def _refresh_env(tmp_path, monkeypatch, *, head_seq):
    """Stub git_ops for _refresh_branch_for_ci: rebase is always a no-op and
    the remote matches local, so the empty-commit path is live. ``head_seq``
    supplies successive head SHAs."""
    from robotsix_mill.stages.merge import _shared as shared_mod

    pushed: list[str] = []
    state = {"head": head_seq[0], "i": 0}

    monkeypatch.setattr(shared_mod.git_ops, "try_rebase_onto", lambda *a, **k: False)
    monkeypatch.setattr(shared_mod.git_ops, "head_sha", lambda repo: state["head"])
    monkeypatch.setattr(
        shared_mod.git_ops,
        "ls_remote_sha",
        lambda remote_url, ref, token=None: state["head"],
    )

    def _empty_commit(repo, message):
        state["i"] += 1
        state["head"] = head_seq[min(state["i"], len(head_seq) - 1)]

    monkeypatch.setattr(shared_mod.git_ops, "empty_commit", _empty_commit)
    monkeypatch.setattr(
        shared_mod.git_ops,
        "push",
        lambda repo, branch, url, token: pushed.append(state["head"]),
    )
    return pushed


def test_refresh_pushes_at_most_one_empty_commit_per_head(tmp_path, monkeypatch):
    """Regression: a ticket bouncing IMPLEMENT_COMPLETE -> FIXING_CI ->
    IMPLEMENT_COMPLETE re-enters the merge poll each time, and each entry
    pushed another empty commit. Observed 2026-08-02 on robotsix-http
    ...-d320: three empty commits in 22 seconds from two call sites, each
    one restarting the CI run the previous had triggered.
    """
    from robotsix_mill.stages.merge._shared import (
        _CI_POLL_REFRESH_SHA,
        _refresh_branch_for_ci,
    )

    pushed = _refresh_env(tmp_path, monkeypatch, head_seq=["sha-a", "sha-b", "sha-c"])
    sentinel = tmp_path / "artifacts" / _CI_POLL_REFRESH_SHA
    repo = tmp_path / "repo"
    repo.mkdir()

    args = (str(repo), "br", "main", "https://remote", "tok", "tid")
    assert _refresh_branch_for_ci(*args, sentinel_path=sentinel) is True
    assert len(pushed) == 1

    # Same head → no second commit, however many times the poll re-enters.
    for _ in range(3):
        assert _refresh_branch_for_ci(*args, sentinel_path=sentinel) is False
    assert len(pushed) == 1, f"one refresh per head; got {pushed}"


def test_refresh_allowed_again_once_the_branch_really_moves(tmp_path, monkeypatch):
    """The sentinel must not wedge the refresh forever — a real push (a
    landed fix) moves the head, and that new head gets its own budget."""
    from robotsix_mill.stages.merge import _shared as shared_mod
    from robotsix_mill.stages.merge._shared import (
        _CI_POLL_REFRESH_SHA,
        _refresh_branch_for_ci,
    )

    pushed = _refresh_env(tmp_path, monkeypatch, head_seq=["sha-a", "sha-b"])
    sentinel = tmp_path / "artifacts" / _CI_POLL_REFRESH_SHA
    repo = tmp_path / "repo"
    repo.mkdir()
    args = (str(repo), "br", "main", "https://remote", "tok", "tid")

    assert _refresh_branch_for_ci(*args, sentinel_path=sentinel) is True
    assert _refresh_branch_for_ci(*args, sentinel_path=sentinel) is False

    # Something real pushes and moves the branch on.
    monkeypatch.setattr(shared_mod.git_ops, "head_sha", lambda repo: "sha-landed")
    monkeypatch.setattr(
        shared_mod.git_ops,
        "ls_remote_sha",
        lambda remote_url, ref, token=None: "sha-landed",
    )
    assert _refresh_branch_for_ci(*args, sentinel_path=sentinel) is True
    assert len(pushed) == 2


def test_successful_rebase_does_not_also_push_an_empty_commit(tmp_path, monkeypatch):
    """A rebase that pushed already produced a fresh head and a fresh run;
    an empty commit on top would only invalidate it."""
    from robotsix_mill.stages.merge import _shared as shared_mod
    from robotsix_mill.stages.merge._shared import _refresh_branch_for_ci

    empty_commits: list[str] = []
    monkeypatch.setattr(shared_mod.git_ops, "try_rebase_onto", lambda *a, **k: True)
    monkeypatch.setattr(shared_mod.git_ops, "push", lambda *a, **k: None)
    monkeypatch.setattr(shared_mod.git_ops, "head_sha", lambda repo: "same")
    monkeypatch.setattr(
        shared_mod.git_ops, "ls_remote_sha", lambda remote_url, ref, token=None: "same"
    )
    monkeypatch.setattr(
        shared_mod.git_ops,
        "empty_commit",
        lambda repo, message: empty_commits.append(message),
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    assert (
        _refresh_branch_for_ci(str(repo), "br", "main", "https://remote", "tok", "tid")
        is True
    )
    assert empty_commits == [], (
        "the rebase push already triggered a fresh run; the empty commit "
        "would abandon it"
    )


# === Transient CI failure does not consume cycle ceiling ===================


def test_transient_ci_failure_does_not_increment_auto_fix_counter(
    tmp_path,
    monkeypatch,
):
    """When CI fails but the failure is transient (infra flake), the
    auto_fix_cycles counter is NOT incremented and the ticket routes
    to REBASING instead of FIXING_CI."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=6)
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
            "conclusion": "failure",
            "failing": [
                {
                    "name": "test",
                    "summary": None,
                    "text": "ECONNRESET: connection reset by peer",
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 0)

    out = MergeStage().run(t, ctx)
    # Should route to REBASING (not FIXING_CI), and counter should NOT increment.
    assert out.next_state is State.REBASING
    assert "transient" in out.note.lower()
    assert _read_counter(counter_path) == 0


def test_transient_ci_failure_at_ceiling_does_not_block(
    tmp_path,
    monkeypatch,
):
    """When the auto-fix ceiling is reached but the current failure is
    transient, the ticket routes to REBASING instead of BLOCKED."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=3)
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
            "conclusion": "failure",
            "failing": [
                {
                    "name": "test",
                    "summary": None,
                    "text": "The runner has received a shutdown signal",
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 3)  # at ceiling

    out = MergeStage().run(t, ctx)
    # Should route to REBASING, not BLOCKED.
    assert out.next_state is State.REBASING
    assert "transient" in out.note.lower()


def test_deterministic_ci_failure_still_increments_counter(
    tmp_path,
    monkeypatch,
):
    """A deterministic CI failure (lint error) still increments the
    auto-fix counter and routes to FIXING_CI."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=6)
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
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": None,
                    "text": "F841 local variable `foo` is assigned to but never used",
                    "annotations": [
                        {
                            "path": "src/mod.py",
                            "start_line": 10,
                            "message": "F841",
                            "level": "failure",
                        }
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )

    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 0)

    out = MergeStage().run(t, ctx)
    # Should route to FIXING_CI and counter should increment.
    assert out.next_state is State.FIXING_CI
    assert _read_counter(counter_path) == 1


def test_green_pr_behind_many_times_never_hits_cycle_ceiling(
    tmp_path,
    monkeypatch,
):
    """A PR that is green but behind does NOT accumulate auto-fix cycles
    even after many rebase cycles — the cycle counter only increments
    on CI failure, not on clean rebases.

    Regression: a perfectly green PR on a fast-moving main could be
    hard-blocked because transient CI failures (not the PR's fault)
    consumed the cycle ceiling.  This test pins the invariant that
    a sustained-green PR cycling through REBASING never trips the
    ceiling guard.
    """
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_fix_max_cycles=6)
    # CI is green but the PR is behind the target.
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
        },
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    # Simulate 10 polls of a green-but-behind PR.  Each poll should
    # route to REBASING and never touch the auto-fix counter.
    t = _implement_complete(ctx)
    counter_path = ctx.service.workspace(t).artifacts_dir / _AUTO_FIX_CYCLES
    _write_counter(counter_path, 0)

    for i in range(10):
        out = MergeStage().run(t, ctx)
        assert out.next_state is State.REBASING, (
            f"Iteration {i}: expected REBASING, got {out.next_state}"
        )
        assert _read_counter(counter_path) == 0, (
            f"Iteration {i}: counter should stay at 0, got {_read_counter(counter_path)}"
        )

    # After 10 cycles the ticket is still not blocked.
    assert out.next_state is not State.BLOCKED


# === Parked-PR rebase cooldown ===========================================


def test_human_mr_approval_cooldown_prevents_rebase_on_conflict(tmp_path, monkeypatch):
    """A fresh rebase timestamp + conflicting PR → stays in HUMAN_MR_APPROVAL."""
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    # Write a fresh last_rebase_at.txt timestamp.
    ts_path = ctx.service.workspace(t).artifacts_dir / _REBASE_LAST_TS
    ts_path.write_text(
        datetime.datetime.now(datetime.UTC).isoformat(),
        encoding="utf-8",
    )

    out = MergeStage().run(t, ctx)
    # Cooldown active → stays in HUMAN_MR_APPROVAL.
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_human_mr_approval_cooldown_expired_allows_rebase(tmp_path, monkeypatch):
    """A stale rebase timestamp (>4h ago) + conflicting PR → REBASING."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    # Write a stale timestamp (5 hours ago).
    ts_path = ctx.service.workspace(t).artifacts_dir / _REBASE_LAST_TS
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=5)
    ts_path.write_text(stale.isoformat(), encoding="utf-8")

    out = MergeStage().run(t, ctx)
    # Cooldown expired → REBASING (autonomous rebase enabled by default).
    assert out.next_state is State.REBASING


def test_human_mr_approval_cooldown_zero_always_allows_rebase(tmp_path, monkeypatch):
    """parked_rebase_cooldown_hours=0 → cooldown disabled, conflicting PR routes to REBASING."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, parked_rebase_cooldown_hours=0)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    # Write a fresh timestamp — should be ignored when cooldown is 0.
    ts_path = ctx.service.workspace(t).artifacts_dir / _REBASE_LAST_TS
    ts_path.write_text(
        datetime.datetime.now(datetime.UTC).isoformat(),
        encoding="utf-8",
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING


def test_human_mr_approval_no_timestamp_allows_rebase(tmp_path, monkeypatch):
    """No last_rebase_at.txt → routes to REBASING (autonomous rebase enabled)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")

    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")

    # No last_rebase_at.txt written — cooldown has no reference point.
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING

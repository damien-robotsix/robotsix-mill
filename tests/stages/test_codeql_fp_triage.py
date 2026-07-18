"""Tests for the codeql_fp_triage sub-agent integration in ci_fix."""

import json

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import CIFixStage
from robotsix_mill.stages.ci_fix_codeql import _eligible_for_triage
from robotsix_mill.stages.ci_fix_helpers import (
    _only_codeql_failing,
    _read_counter,
    _write_counter,
)
from robotsix_mill.vcs import git_ops
from robotsix_mill.agents.codeql_fp_triage import (
    AlertVerdict,
    CodeQLFpTriageResult,
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


def _fixing_ci(ctx):
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.FIXING_CI,
    ):
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


def _setup_repo(ctx, ticket):
    repo_dir = ctx.service.workspace(ticket).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)
    return str(repo_dir)


def _codeql_alert(number, path, security_severity_level=None):
    """Build a synthetic code-scanning alert dict in the shape
    ``GitHubForge.list_code_scanning_alerts`` returns."""
    return {
        "number": number,
        "rule": "py/unused-import",
        "severity": security_severity_level or "warning",
        "security_severity_level": security_severity_level,
        "path": path,
        "line": 10,
        "message": f"Import of '{path}' is not used",
        "url": f"https://github.com/o/r/security/code-scanning/{number}",
    }


# ---------------------------------------------------------------------------
#  Unit tests: _only_codeql_failing
# ---------------------------------------------------------------------------


def test_only_codeql_failing_true():
    assert _only_codeql_failing([{"name": "CodeQL"}]) is True


def test_only_codeql_failing_true_code_scanning():
    assert _only_codeql_failing([{"name": "Code Scanning"}]) is True


def test_only_codeql_failing_true_multiple_codeql():
    assert (
        _only_codeql_failing([{"name": "CodeQL"}, {"name": "CodeQL / Analyze"}]) is True
    )


def test_only_codeql_failing_false_mixed():
    assert (
        _only_codeql_failing([{"name": "CodeQL"}, {"name": "pytest (3.11)"}]) is False
    )


def test_only_codeql_failing_false_non_codeql():
    assert _only_codeql_failing([{"name": "pytest"}]) is False


def test_only_codeql_failing_false_empty():
    assert _only_codeql_failing([]) is False


# ---------------------------------------------------------------------------
#  Unit tests: _eligible_for_triage
# ---------------------------------------------------------------------------


def test_eligible_in_scope_non_security():
    alerts = [
        _codeql_alert(1, "src/foo.py"),
        _codeql_alert(2, "src/bar.py"),
    ]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=5)
    assert len(result) == 1
    assert result[0]["number"] == 1


def test_eligible_excludes_out_of_scope():
    alerts = [_codeql_alert(1, "src/other.py")]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=5)
    assert len(result) == 0


def test_eligible_excludes_security_severity():
    alerts = [
        _codeql_alert(1, "src/foo.py", security_severity_level="high"),
        _codeql_alert(2, "src/foo.py", security_severity_level="medium"),
        _codeql_alert(3, "src/foo.py", security_severity_level="low"),
    ]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=5)
    assert len(result) == 0


def test_eligible_excludes_null_number():
    alerts = [
        {
            "number": None,
            "rule": "py/unused-import",
            "severity": "warning",
            "security_severity_level": None,
            "path": "src/foo.py",
            "line": 10,
            "message": "unused",
            "url": "",
        }
    ]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=5)
    assert len(result) == 0


def test_eligible_respects_max_dismissals():
    alerts = [_codeql_alert(i, "src/foo.py") for i in range(1, 10)]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=3)
    assert len(result) == 3
    assert [a["number"] for a in result] == [1, 2, 3]


def test_eligible_empty_path():
    alerts = [
        {
            "number": 1,
            "rule": "py/unused-import",
            "severity": "warning",
            "security_severity_level": None,
            "path": "",
            "line": 10,
            "message": "unused",
            "url": "",
        }
    ]
    changed = {"src/foo.py"}
    result = _eligible_for_triage(alerts, changed, max_dismissals=5)
    assert len(result) == 0


# ---------------------------------------------------------------------------
#  Integration: ONLY CodeQL failing → triage runs → dismissal → unblock
# ---------------------------------------------------------------------------


def test_codeql_only_dismisses_and_unblocks(tmp_path, monkeypatch):
    """When the ONLY failing check is CodeQL and the triage agent dismisses
    an eligible alert, the ticket returns IMPLEMENT_COMPLETE (not BLOCKED)."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",  # trigger ceiling immediately
        codeql_fp_triage_enabled="true",
    )
    # Mock: only CodeQL is failing.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # In-scope alert, non-security.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(42, "src/foo.py", security_severity_level=None)
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    # Mock agent: dismiss alert 42.
    def fake_triage(**kw):
        return CodeQLFpTriageResult(
            verdicts=[
                AlertVerdict(
                    alert_number=42,
                    verdict="dismiss",
                    rationale="re-exported via __getattr__",
                )
            ],
            summary="dismissed",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )
    # Mock the forge dismissal.
    dismissed = {}

    def fake_dismiss(self, *, number, reason, comment):
        dismissed[number] = (reason, comment)
        return True

    monkeypatch.setattr(github.GitHubForge, "dismiss_code_scanning_alert", fake_dismiss)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    # Seed cycle counter at ceiling.
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert dismissed.get(42) == ("false positive", "re-exported via __getattr__")


# ---------------------------------------------------------------------------
#  Integration: test failing alongside CodeQL → still BLOCKED
# ---------------------------------------------------------------------------


def test_mixed_failures_still_blocks(tmp_path, monkeypatch):
    """When a test is also failing (not just CodeQL), triage is skipped
    and the ticket BLOCKs as usual."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []},
                {"name": "pytest", "summary": "", "text": None, "annotations": []},
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert len(triage_called) == 0  # triage never ran


# ---------------------------------------------------------------------------
#  Integration: security-severity alert → not eligible → still BLOCKED
# ---------------------------------------------------------------------------


def test_security_severity_alert_never_dismissed(tmp_path, monkeypatch):
    """An alert with a non-null security_severity_level is NEVER eligible
    for triage, so the ticket still BLOCKs."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # Security-severity alert (high).
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(99, "src/foo.py", security_severity_level="high")
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    # Triage should NOT run — no eligible alerts.
    assert len(triage_called) == 0


# ---------------------------------------------------------------------------
#  Integration: out-of-scope alert → still BLOCKED
# ---------------------------------------------------------------------------


def test_out_of_scope_alert_not_eligible(tmp_path, monkeypatch):
    """An alert in a file NOT changed by this PR is ineligible."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(1, "untouched/file.py", security_severity_level=None)
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert len(triage_called) == 0


# ---------------------------------------------------------------------------
#  Integration: agent abstains → still BLOCKED
# ---------------------------------------------------------------------------


def test_agent_abstains_still_blocks(tmp_path, monkeypatch):
    """When the triage agent abstains on all alerts, the ticket still BLOCKs."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(1, "src/foo.py", security_severity_level=None)
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    # Agent abstains.
    def fake_triage(**kw):
        return CodeQLFpTriageResult(
            verdicts=[
                AlertVerdict(
                    alert_number=1,
                    verdict="abstain",
                    rationale="could not verify re-export",
                )
            ],
            summary="abstained",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


# ---------------------------------------------------------------------------
#  Integration: run-once sentinel
# ---------------------------------------------------------------------------


def test_run_once_sentinel_prevents_second_triage(tmp_path, monkeypatch):
    """The sentinel file prevents the triage from running a second time."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(1, "src/foo.py", security_severity_level=None)
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult(
            verdicts=[AlertVerdict(alert_number=1, verdict="dismiss", rationale="ok")],
            summary="dismissed",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "dismiss_code_scanning_alert",
        lambda self, *, number, reason, comment: True,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"

    # First run: triage should fire.
    _write_counter(cycle_path, 1)
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE
    assert len(triage_called) == 1

    # Second run: sentinel exists → triage skipped → BLOCKED.
    _write_counter(cycle_path, 1)
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert len(triage_called) == 1  # no second call


# ---------------------------------------------------------------------------
#  Integration: early trigger before attempt cap
# ---------------------------------------------------------------------------


def test_early_triage_before_attempt_cap(tmp_path, monkeypatch):
    """The early FP triage fires on the first CodeQL-only poll, BEFORE
    any ci_fix attempt is consumed — so the attempt counter stays at 0."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="3",  # ceiling NOT reachable early
        ci_fix_max_attempts="2",
        codeql_fp_triage_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            _codeql_alert(1, "src/foo.py", security_severity_level=None)
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [{"path": "src/foo.py"}],
    )

    def fake_triage(**kw):
        return CodeQLFpTriageResult(
            verdicts=[AlertVerdict(alert_number=1, verdict="dismiss", rationale="fp")],
            summary="dismissed",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "dismiss_code_scanning_alert",
        lambda self, *, number, reason, comment: True,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Cycle counter at 0 (not at ceiling) — only the early trigger applies.
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    assert _read_counter(cycle_path) == 0

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # Attempt counter was never incremented — the triage intercepted first.
    attempt_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"
    assert _read_counter(attempt_path) == 0


def test_early_triage_skipped_when_non_codeql_fails(tmp_path, monkeypatch):
    """When a non-CodeQL check also fails, the early trigger skips and the
    ci-fix agent runs. With the agent now owning the loop, a crash → BLOCKED
    in one shot (no per-poll attempt retry)."""
    ctx = _gh(
        tmp_path,
        codeql_fp_triage_enabled="true",
    )
    ci_fail = {
        "conclusion": "failure",
        "failing": [
            {"name": "CodeQL", "summary": "", "text": None, "annotations": []},
            {"name": "pytest", "summary": "", "text": None, "annotations": []},
        ],
    }
    monkeypatch.setattr(
        github.GitHubForge, "check_status", lambda self, *, source_branch, require_checks=False: ci_fail
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status", lambda self, *, source_branch, require_checks=False: {"sha": "abc"}
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [],
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )
    # Agent crasher so we don't need to mock push.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.reconcile_with_remote_pr",
        lambda repo, remote_url, branch, token: git_ops.ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("crash")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    # Triage should NOT have run (non-CodeQL check present).
    assert len(triage_called) == 0
    # The ci-fix agent ran and crashed → the stage blocks (no retry loop).
    assert out.next_state is State.BLOCKED


# ---------------------------------------------------------------------------
#  Integration: feature-flag disabled → triage skipped → BLOCKED
# ---------------------------------------------------------------------------


def test_flag_disabled_skips_triage(tmp_path, monkeypatch):
    """When codeql_fp_triage_enabled is False, the triage is skipped."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_cycles="1",
        codeql_fp_triage_enabled="false",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )

    triage_called = []

    def fake_triage(**kw):
        triage_called.append(1)
        return CodeQLFpTriageResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        fake_triage,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    _write_counter(cycle_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert len(triage_called) == 0


# ---------------------------------------------------------------------------
#  Smoke test: agent wiring (build_agent kwarg drift)
# ---------------------------------------------------------------------------


def test_codeql_fp_triage_agent_wiring_smoke(monkeypatch):
    """Verify that run_codeql_fp_triage_agent calls load_and_run_agent with
    the correct definition_name so kwarg drift can't ship silently."""
    from robotsix_mill.agents.codeql_fp_triage import run_codeql_fp_triage_agent
    from pathlib import Path
    from robotsix_mill.config import Settings

    s = Settings(
        data_dir="/tmp/test_triage",
    )
    # Wire Secrets so get_secrets().openrouter_api_key is not None.
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured = {}

    def fake_load_and_run(
        *,
        settings,
        definition_name,
        tools,
        prompt,
        what,
        repo_dir,
        board_id,
        system_prompt_format_kwargs,
    ):
        captured["definition_name"] = definition_name
        captured["what"] = what
        captured["tools"] = tools
        # Return a fake result.
        from types import SimpleNamespace

        return SimpleNamespace(
            output=CodeQLFpTriageResult(
                verdicts=[AlertVerdict(alert_number=1, verdict="abstain")],
                summary="test",
            )
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.yaml_loader.load_and_run_agent",
        fake_load_and_run,
    )

    result = run_codeql_fp_triage_agent(
        settings=s,
        repo_dir=Path("/tmp/test"),
        alerts_json=json.dumps([_codeql_alert(1, "src/foo.py")]),
        ticket_id="test-123",
        board_id="test-board",
    )

    assert captured["definition_name"] == "codeql_fp_triage"
    assert captured["what"] == "codeql_fp_triage"
    assert len(captured["tools"]) > 0
    assert len(result.verdicts) == 1
    assert result.verdicts[0].verdict == "abstain"


# ---------------------------------------------------------------------------
#  Unit test: dismiss_code_scanning_alert forge method (mock PATCH)
# ---------------------------------------------------------------------------


def test_dismiss_code_scanning_alert_patch(monkeypatch):
    """Verify that GitHubForge.dismiss_code_scanning_alert sends the
    correct PATCH request to the dismiss endpoint."""
    from robotsix_mill.forge.github import GitHubForge
    from robotsix_mill.config import Settings, RepoConfig

    s = Settings(
        data_dir="/tmp/test_dismiss",
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    rc = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    forge = GitHubForge(s, repo_config=rc)

    patches = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_patch(path, **kwargs):
        patches.append((path, kwargs))
        return FakeResponse()

    monkeypatch.setattr(forge._http, "patch", fake_patch)

    result = forge.dismiss_code_scanning_alert(
        number=42,
        reason="false positive",
        comment="re-exported via __getattr__",
    )

    assert result is True
    assert len(patches) == 1
    path, kwargs = patches[0]
    assert "42" in path
    assert kwargs["json"]["state"] == "dismissed"
    assert kwargs["json"]["dismissed_reason"] == "false positive"
    assert kwargs["json"]["dismissed_comment"] == "re-exported via __getattr__"

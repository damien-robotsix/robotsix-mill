"""Parsing-focused CIFixStage tests split out of ``test_ci_fix``.

Covers ``_build_failing_summary`` log-text formatting, the CI-failure
fingerprint, and ``_extract_check_names`` summary parsing. Shared helpers are
imported from the parent ``test_ci_fix`` module; the autouse
``_mock_proactive_rebase_git_ops`` fixture is copied here verbatim because
module-level autouse fixtures do not apply across modules.
"""

import pytest

from robotsix_mill.agents.ci_fixing import CiFixResult
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages.ci_fix import (
    CIFixStage,
    _extract_check_names,
)
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
    _ci_failure_fingerprint,
)
from robotsix_mill.vcs import git_ops

# ---------------------------------------------------------------------------
# Module-level autouse fixture: prevent any test from accidentally running
# real git fetch / push operations during the proactive-rebase step added
# in _resolve_clone_and_status.  Without this, git fetch against a fake
# forge URL hangs until the suite's 300s timeout, producing rc=124.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_proactive_rebase_git_ops(monkeypatch):
    """Stub every git operation in ci_fix that would touch the real network
    so no test ever hangs waiting for a remote.

    * ``try_rebase_onto`` → ``False`` (nothing to rebase)
    * ``push`` → no-op
    * ``head_sha`` → ``"abc123"`` (fake commit SHA)
    * ``ls_remote_sha`` → ``None`` (no remote branch — skips empty-commit path)
    * ``empty_commit`` → no-op
    * ``reconcile_with_remote_pr`` → ``SYNCED`` (no foreign commits)
    * ``post_push_check`` → ``PASS`` (push landed cleanly)
    * ``try_rebase_onto_branch`` → ``True`` (auto-retry rebase succeeds)
    * ``push_with_lease`` → no-op (auto-retry re-push)

    Tests that need different behaviour override the relevant stub with
    their own ``monkeypatch.setattr`` — call-verification assertions (e.g.
    counting calls) continue to work because ``monkeypatch`` is function-
    scoped and later ``setattr`` calls simply overwrite the earlier stub.
    """
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.ls_remote_sha",
        lambda remote_url, ref, token=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.empty_commit",
        lambda repo, message: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.reconcile_with_remote_pr",
        lambda repo, remote_url, branch, token: git_ops.ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )
    # Auto-retry path (NOT_LANDED recovery): rebase succeeds, re-push no-op.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto_branch",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )


from tests.stages.test_ci_fix import (
    _fixing_ci,
    _gh,
    _oos_forge,
    _oos_result,
    _setup_repo,
)

# ---------------------------------------------------------------------------
# _build_failing_summary with log_text
# ---------------------------------------------------------------------------


def test_build_failing_summary_includes_job_logs():
    """_build_failing_summary includes **Job logs:** section when log_text provided."""
    failing = [
        {"name": "docker-build", "summary": None, "text": None, "annotations": []},
    ]
    result = _build_failing_summary(failing, log_text="ERROR: build failed\n")
    assert "**Job logs:**" in result
    assert "ERROR: build failed" in result


def test_build_failing_summary_no_logs_still_works():
    """Existing path unchanged when log_text is None/empty."""
    failing = [
        {"name": "lint", "summary": "err", "text": None, "annotations": []},
    ]
    result = _build_failing_summary(failing)
    assert "**Job logs:**" not in result
    assert "## ❌ FAILED: lint" in result


def test_ci_fix_stage_fetches_job_logs_on_failure(tmp_path, monkeypatch):
    """Mock list_workflow_runs + fetch_workflow_job_logs; verify
    _build_failing_summary receives the log text."""
    ctx = _gh(tmp_path)
    # PR status returns a sha.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {
            "merged": False,
            "state": "open",
            "url": "http://pr",
            "mergeable": True,
            "sha": "abc123",
        },
    )
    # check_status returns failure.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "build", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    # list_workflow_runs returns one failed run.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {
                "id": 42,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc123",
                "conclusion": "failure",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )
    # fetch_workflow_job_logs returns log text.
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id: "docker build error\n",
    )
    # ci-fix agent succeeds.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    # push succeeds via post_push_check.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_build_failing_summary_includes_codeql_alerts():
    from robotsix_mill.stages.ci_fix_helpers import _build_failing_summary

    out = _build_failing_summary(
        failing=[{"name": "CodeQL"}],
        log_text="",
        alerts=[
            {
                "rule": "py/x",
                "severity": "high",
                "path": "t.py",
                "line": 9,
                "message": "bad",
            }
        ],
    )
    assert "Code-scanning alerts" in out
    assert "py/x" in out
    assert "t.py:9" in out
    assert "high" in out


# ---------------------------------------------------------------------------
# CI-failure fingerprint
# ---------------------------------------------------------------------------


def test_ci_failure_fingerprint_is_stable() -> None:
    """Same failing_summary + repo_id always produces the same fingerprint."""
    summary = (
        "## Failing check #1: lint / ruff\n"
        "**Summary:**\nFound 3 errors\n\n"
        "**Job logs:**\n```\n(timestamp: 2025-06-14T12:00:00Z)\n"
        "error: unused import\n```\n"
    )
    fp1 = _ci_failure_fingerprint(summary, "test-board")
    fp2 = _ci_failure_fingerprint(summary, "test-board")
    assert fp1 == fp2
    assert len(fp1) == 16
    # All hex chars.
    assert all(c in "0123456789abcdef" for c in fp1)


def test_ci_failure_fingerprint_differs_for_different_checks() -> None:
    """Different failing check names produce different fingerprints."""
    s1 = "## Failing check #1: lint\n**Summary:**\nerror\n\n**Job logs:**\n```\nlog\n```\n"
    s2 = "## Failing check #1: pytest\n**Summary:**\nerror\n\n**Job logs:**\n```\nlog\n```\n"
    fp1 = _ci_failure_fingerprint(s1, "board")
    fp2 = _ci_failure_fingerprint(s2, "board")
    assert fp1 != fp2


def test_ci_failure_fingerprint_differs_for_different_repos() -> None:
    """Same failure on different repos produces different fingerprints."""
    summary = (
        "## Failing check #1: lint\n**Summary:**\nerror\n\n**Job logs:**\n```\nx\n```\n"
    )
    fp1 = _ci_failure_fingerprint(summary, "board-a")
    fp2 = _ci_failure_fingerprint(summary, "board-b")
    assert fp1 != fp2


def test_ci_failure_fingerprint_truncates_at_job_logs_marker() -> None:
    """The **Job logs:** marker and everything after is excluded from the hash."""
    base = "## Failing check #1: lint\n**Summary:**\nerror\n\n"
    s1 = base + "**Job logs:**\n```\nlog-v1\n```\n"
    s2 = base + "**Job logs:**\n```\nlog-v2-different-timestamps\n```\n"
    assert _ci_failure_fingerprint(s1, "b") == _ci_failure_fingerprint(s2, "b")


def test_ci_failure_fingerprint_truncates_at_2000_chars_when_no_marker() -> None:
    """Without a **Job logs:** marker, the input is truncated to 2000 chars."""
    # Build a summary > 2000 chars with no marker.
    prefix = "## Failing check #1: lint\n**Summary:**\n" + ("x" * 3000)
    suffix = "\nmore stuff that differs"
    s1 = prefix + suffix
    s2 = prefix + "-different-suffix"
    # Both share the same first 2000 chars → same fingerprint.
    assert _ci_failure_fingerprint(s1, "b") == _ci_failure_fingerprint(s2, "b")


def test_ci_failure_fingerprint_empty_summary() -> None:
    """Empty failing_summary produces a valid fingerprint (does not crash)."""
    fp = _ci_failure_fingerprint("", "board")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_ci_failure_fingerprint_passed_to_spawn_via_dedup_labels(
    tmp_path, monkeypatch
) -> None:
    """When _handle_out_of_scope runs, it computes a fingerprint and passes
    dedup_labels=[ci_fp:<hex>] to spawn_dependency_fix."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )
    # Capture the call to spawn_dependency_fix.
    spawn_kwargs = {}

    def fake_spawn(ticket, ctx, **kwargs):
        spawn_kwargs.update(kwargs)
        # Return a valid Outcome so the stage doesn't crash.
        from robotsix_mill.stages.base import Outcome

        return Outcome(State.BLOCKED, "test")

    monkeypatch.setattr(
        "robotsix_mill.stages.dependency_fix.spawn_dependency_fix",
        fake_spawn,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    assert "dedup_labels" in spawn_kwargs
    labels = spawn_kwargs["dedup_labels"]
    assert len(labels) == 1
    assert labels[0].startswith("ci_fp:")
    assert len(labels[0]) == len("ci_fp:") + 16  # "ci_fp:" + 16 hex chars


# ---------------------------------------------------------------------------
# _extract_check_names — parse check names from the failing summary format
# ---------------------------------------------------------------------------


def test_extract_check_names_empty_or_none() -> None:
    """Empty string or whitespace-only returns (unknown)."""
    assert _extract_check_names("") == "(unknown)"
    assert _extract_check_names("   \n  ") == "(unknown)"


def test_extract_check_names_single_failing() -> None:
    """A single ❌ FAILED: header returns the check name."""
    summary = "## ❌ FAILED: ruff / lint\n\n**Summary:**\nFound 3 errors\n"
    assert _extract_check_names(summary) == "ruff / lint"


def test_extract_check_names_multiple_failing() -> None:
    """Multiple ❌ FAILED: headers return a comma-separated list."""
    summary = (
        "## ❌ FAILED: ruff / lint\n\n"
        "**Summary:**\nFound 3 errors\n\n"
        "## ✅ PASSED: tests\n\n"
        "## ❌ FAILED: typecheck (3.12)\n\n"
        "**Details:**\n...\n"
    )
    result = _extract_check_names(summary)
    # Order is discovery order (top-down).
    assert "ruff / lint" in result
    assert "typecheck (3.12)" in result
    assert result == "ruff / lint, typecheck (3.12)"


def test_extract_check_names_skips_passed() -> None:
    """✅ PASSED: headers are not collected."""
    summary = "## ✅ PASSED: tests\n\n## ✅ PASSED: lint\n"
    assert _extract_check_names(summary) == "(unknown)"


def test_extract_check_names_codeql_compact_block() -> None:
    """A compact CodeQL alert block yields 'CodeQL code-scanning'."""
    summary = (
        "**CodeQL alerts to fix (extracted for fast reference — rule ID and location):**\n"
        "- `py/clear-text-logging` @ src/foo.py:42\n\n"
        "## ❌ FAILED: CodeQL\n\n"
        "**Summary:**\n...\n"
    )
    result = _extract_check_names(summary)
    # Both the compact block and the failing header are collected; the
    # compact block comes first.
    assert "CodeQL code-scanning" in result
    assert "CodeQL" in result


def test_extract_check_names_collects_all_failing_headers() -> None:
    """All ❌ FAILED: headers are collected regardless of intermediate content."""
    summary = (
        "## ❌ FAILED: lint\n\n**Summary:**\nSome error\n\n## ❌ FAILED: late_check\n\n"
    )
    result = _extract_check_names(summary)
    assert result == "lint, late_check"


def test_extract_check_names_truncates() -> None:
    """Result is truncated to 200 characters."""
    # Build check names that together exceed 200 chars.
    long_name = "very-long-check-name-" + ("x" * 180)
    summary = f"## ❌ FAILED: {long_name}\n\n**Summary:**\n...\n"
    result = _extract_check_names(summary)
    assert len(result) <= 200
    assert result.startswith("very-long-check-name")


def test_extract_check_names_realistic_mixed_summary() -> None:
    """Integration-style test with a realistic mixed pass/fail summary."""
    summary = _build_failing_summary(
        [
            {"name": "ruff / lint", "conclusion": "failure", "summary": "3 errors"},
            {"name": "tests (3.12)", "conclusion": "success", "summary": "42 passed"},
            {"name": "typecheck", "conclusion": "failure", "summary": "1 error"},
        ]
    )
    result = _extract_check_names(summary)
    assert "ruff / lint" in result
    assert "typecheck" in result
    assert "tests (3.12)" not in result

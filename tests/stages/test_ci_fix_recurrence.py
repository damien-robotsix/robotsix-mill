"""Recurrence detection for ci_fix-spawned dependency tickets."""

from datetime import UTC, datetime, timedelta

from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.scm.forge import github
from robotsix_mill.stages.ci_fix import CIFixStage
from robotsix_mill.stages.ci_fix_recurrence import (
    extract_failure_signature,
    find_prior_fixes,
    signature_label,
)
from tests.stages.test_ci_fix_out_of_scope import (
    _fixing_ci,
    _gh,
    _oos_forge,
    _oos_result,
    _setup_repo,
)

_TS = "2026-10-28T08:30:02.0000000Z "
_ERESOLVE_LOG = f"""\
{_TS}> npm ci
{_TS}npm error code ERESOLVE
{_TS}npm error ERESOLVE could not resolve
{_TS}npm error
{_TS}npm error While resolving: typescript-eslint@8.46.0
{_TS}npm error Found: typescript@7.0.2
{_TS}npm error node_modules/typescript
{_TS}npm error   dev typescript@"^7.0.2" from the root project
{_TS}npm error
{_TS}npm error Could not resolve dependency:
{_TS}npm error peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.46.0
{_TS}npm error node_modules/typescript-eslint
"""


def test_extract_signature_npm_eresolve():
    assert (
        extract_failure_signature(_ERESOLVE_LOG)
        == "npm ci ERESOLVE typescript/typescript-eslint"
    )


def test_extract_signature_is_version_independent():
    bumped = _ERESOLVE_LOG.replace("7.0.2", "7.1.0").replace("8.46.0", "8.50.1")
    assert extract_failure_signature(bumped) == extract_failure_signature(_ERESOLVE_LOG)


def test_extract_signature_scoped_packages_legacy_npm_err():
    log = (
        "npm ERR! code ERESOLVE\n"
        "npm ERR! Found: @types/react@19.0.0\n"
        'npm ERR! peer @types/react@"^18" from @testing-library/react@14.0.0\n'
    )
    assert (
        extract_failure_signature(log)
        == "npm ERESOLVE @types/react/@testing-library/react"
    )


def test_extract_signature_none_for_unrelated_failure():
    assert extract_failure_signature("") is None
    assert extract_failure_signature("FAILED tests/test_x.py::test_y") is None
    # ERESOLVE without the conflict detail cannot be keyed.
    assert extract_failure_signature("npm error code ERESOLVE") is None


def test_signature_label_is_stable():
    sig = "npm ci ERESOLVE typescript/typescript-eslint"
    assert signature_label(sig) == signature_label(sig)
    assert signature_label(sig).startswith("ci_sig:")
    assert signature_label(sig) != signature_label("npm ci ERESOLVE a/b")


def _wire_eresolve_run(monkeypatch, check_name):
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": check_name, "summary": "x", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, **k: [
            {
                "id": 1,
                "name": "CI",
                "conclusion": "failure",
                "html_url": "https://github.com/o/r/actions/runs/1",
            }
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, **k: _ERESOLVE_LOG,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )


def _spawn(ctx):
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    return ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)[0]


def test_recurring_failure_links_prior_fix_and_demands_guard(tmp_path, monkeypatch):
    """A second fix ticket for the same ERESOLVE — surfaced by a different
    check name — carries the prior ticket and the prevent-recurrence ask."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch, alert_paths=())
    _wire_eresolve_run(monkeypatch, "typecheck")

    first = _spawn(ctx)
    first_desc = ctx.service.workspace(first).read_description()
    assert "## Recurrence" not in first_desc
    ctx.service.set_branch(first.id, f"mill/{first.id}")
    ctx.service.transition(first.id, State.DONE)

    _wire_eresolve_run(monkeypatch, "build-lint")
    second = _spawn(ctx)
    assert second.id != first.id
    desc = ctx.service.workspace(second).read_description()
    assert "## Recurrence" in desc
    assert "`npm ci ERESOLVE typescript/typescript-eslint`" in desc
    assert first.id in desc
    assert f"mill/{first.id}" in desc
    assert "prevent recurrence" in desc
    assert "dependabot ignore/group" in desc


def test_find_prior_fixes_respects_window_and_state(tmp_path):
    ctx = _gh(tmp_path)
    label = signature_label("npm ci ERESOLVE a/b")
    open_t = ctx.service.create("o", "d", source=SourceKind.CI_FIX_DEPENDENCY)
    ctx.service.set_labels(open_t.id, [label])
    closed_t = ctx.service.create("c", "d", source=SourceKind.CI_FIX_DEPENDENCY)
    ctx.service.set_labels(closed_t.id, [label])
    ctx.service.transition(closed_t.id, State.READY)
    ctx.service.transition(closed_t.id, State.DONE)

    prior = find_prior_fixes(ctx, label, "test-board")
    assert [p.ticket_id for p in prior] == [closed_t.id]

    later = datetime.now(UTC) + timedelta(days=91)
    assert find_prior_fixes(ctx, label, "test-board", now=later) == []

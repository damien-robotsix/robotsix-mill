"""Tests for the deterministic stale-CI gate (``stages/ci_stale_gate.py``).

The forge is always a hand-rolled fake — no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext, ci_stale_gate
from robotsix_mill.stages.ci_stale_gate import evaluate_stale_ci

F_SHA = "a" * 40
NEW_SHA = "b" * 40
NOW = datetime(2026, 10, 27, tzinfo=UTC)
WF_PATH = ".github/workflows/ci.yml"


def _body(created: str = "2026-10-26T10:00:00Z", run_id: int = 100) -> str:
    return "\n".join(
        [
            "**Workflow:** CI",
            f"**Path:** {WF_PATH}",
            "**Branch:** main",
            f"**Run:** [{run_id}](https://github.com/o/r/actions/runs/{run_id})",
            f"**Commit:** `{F_SHA}`",
            f"**Created:** {created}",
        ]
    )


def _run(rid: int, sha: str, conclusion: str, created: str) -> dict[str, Any]:
    return {
        "id": rid,
        "name": "CI",
        "head_sha": sha,
        "conclusion": conclusion,
        "created_at": created,
        "path": WF_PATH,
        "html_url": f"https://github.com/o/r/actions/runs/{rid}",
        "run_attempt": 1,
    }


class FakeForge:
    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        commits: list[str] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        wf_text: str | None = None,
        log_text: str = "",
    ) -> None:
        self.log_text = log_text
        self.runs = runs or []
        self.commits = commits
        self.jobs = jobs or []
        self.wf_text = wf_text

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        return self.runs

    def get_workflow_run(self, *, run_id):
        return None

    def list_path_commits_since(self, *, path, branch, since):
        return self.commits

    def list_workflow_run_jobs(self, *, run_id):
        return self.jobs

    def get_workflow_file(self, *, path):
        return self.wf_text

    def fetch_workflow_job_logs(self, *, run_id, full_log=False):
        return self.log_text


def _ticket(title: str = "CI failure: CI on main", source=SourceKind.CI):
    return SimpleNamespace(id="t1", title=title, source=source, branch=None)


_RED = _run(100, F_SHA, "failure", "2026-10-26T10:00:00Z")

_REUSABLE_WF = """
name: CI
on: [push]
jobs:
  ci:
    uses: damien-robotsix/robotsix-github-workflows/.github/workflows/python-ci.yml@deadbeef
  local:
    runs-on: ubuntu-latest
    steps:
      - name: Setup shared
        uses: damien-robotsix/robotsix-github-workflows/.github/actions/setup@deadbeef
      - name: Run tests
        run: pytest
"""


# --- green-since ------------------------------------------------------------


def test_green_since_closes():
    forge = FakeForge(
        runs=[_run(101, NEW_SHA, "success", "2026-10-26T12:00:00Z"), _RED]
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.action == "close"
    assert v.note == (
        f"Stale CI failure: workflow 'CI' is green on main at or after the "
        f"failing commit {F_SHA} (green run: "
        "https://github.com/o/r/actions/runs/101). No change needed."
    )


def test_still_red_proceeds():
    forge = FakeForge(runs=[_RED])
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


def test_non_ci_ticket_skipped():
    forge = FakeForge(
        runs=[_run(101, NEW_SHA, "success", "2026-10-26T12:00:00Z"), _RED]
    )
    t = _ticket(title="Add a feature", source=SourceKind.USER)
    assert evaluate_stale_ci(forge, t, _body(), stale_days=14, now=NOW) is None


def test_title_only_ci_ticket_is_checked():
    forge = FakeForge(
        runs=[_run(101, NEW_SHA, "success", "2026-10-26T12:00:00Z"), _RED]
    )
    t = _ticket(source=SourceKind.USER)
    v = evaluate_stale_ci(forge, t, _body(), stale_days=14, now=NOW)
    assert v is not None and v.action == "close"


# --- stale-run ----------------------------------------------------------------


def test_stale_run_with_workflow_change_closes():
    old = "2026-09-01T00:00:00Z"
    forge = FakeForge(runs=[_run(100, F_SHA, "failure", old)], commits=["c0ffee"])
    v = evaluate_stale_ci(forge, _ticket(), _body(created=old), stale_days=14, now=NOW)
    assert v is not None and v.action == "close"
    assert v.note.startswith("Stale CI failure: workflow 'CI'")
    assert WF_PATH in v.note and "c0ffee" in v.note


def test_stale_run_without_workflow_change_proceeds():
    old = "2026-09-01T00:00:00Z"
    forge = FakeForge(runs=[_run(100, F_SHA, "failure", old)], commits=[])
    assert (
        evaluate_stale_ci(forge, _ticket(), _body(created=old), stale_days=14, now=NOW)
        is None
    )


def test_recent_run_with_workflow_change_proceeds():
    forge = FakeForge(runs=[_RED], commits=["c0ffee"])
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


# --- upstream-only -----------------------------------------------------------


def _job(name: str, step: str, conclusion: str = "failure") -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": step, "conclusion": conclusion}],
    }


def test_upstream_reusable_job_blocks():
    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("ci / Audit", "Audit dependencies")],
        wf_text=_REUSABLE_WF,
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.action == "block"
    assert v.note == (
        "Not fixable from this repo: failure is internal to "
        "damien-robotsix/robotsix-github-workflows/.github/workflows/"
        "python-ci.yml@deadbeef. ci-auto-close will close this once 'CI' goes "
        "green on main."
    )


def test_upstream_composite_action_step_blocks():
    forge = FakeForge(
        runs=[_RED], jobs=[_job("local", "Setup shared")], wf_text=_REUSABLE_WF
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.action == "block"
    assert "actions/setup@deadbeef" in v.note


def test_reusable_job_running_caller_tests_proceeds():
    forge = FakeForge(
        runs=[_RED], jobs=[_job("ci / Tests", "Run tests")], wf_text=_REUSABLE_WF
    )
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


def test_local_step_failure_proceeds():
    forge = FakeForge(
        runs=[_RED], jobs=[_job("local", "Run tests")], wf_text=_REUSABLE_WF
    )
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


def test_mixed_upstream_and_local_failures_proceed():
    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("ci / Audit", "Audit dependencies"), _job("local", "Build")],
        wf_text=_REUSABLE_WF,
    )
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


def test_unreadable_workflow_file_proceeds():
    forge = FakeForge(runs=[_RED], jobs=[_job("ci / Audit", "Audit dependencies")])
    assert evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW) is None


# --- caller-side evidence: missing secret / variable -------------------------

_RELEASE_WF = """
name: release-please
on: [push]
jobs:
  release:
    uses: damien-robotsix/robotsix-github-workflows/.github/workflows/release-please.yml@47523e8a
    with:
      app-id: ${{ vars.RELEASE_APP_ID }}
    secrets:
      private-key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
      token: ${{ secrets.GITHUB_TOKEN }}
"""

_RELEASE_REF = (
    "damien-robotsix/robotsix-github-workflows/.github/workflows/"
    "release-please.yml@47523e8a"
)

_EMPTY_APP_ID_LOG = """\
2026-10-28T07:30:00.0000000Z ##[group]Run actions/checkout@v4
2026-10-28T07:30:00.0000000Z with:
2026-10-28T07:30:00.0000000Z   path: 
2026-10-28T07:30:00.0000000Z ##[endgroup]
2026-10-28T07:30:01.0000000Z ##[group]Run actions/create-github-app-token@v1
2026-10-28T07:30:01.0000000Z with:
2026-10-28T07:30:01.0000000Z   app-id: 
2026-10-28T07:30:01.0000000Z   private-key: 
2026-10-28T07:30:01.0000000Z ##[endgroup]
2026-10-28T07:30:02.0000000Z ##[error][@octokit/auth-app] appId option is required
"""


def _release_run(rid: int, conclusion: str, created: str) -> dict[str, Any]:
    r = _run(rid, F_SHA, conclusion, created)
    r.update(name="release-please", path=".github/workflows/release-please.yml")
    return r


def test_empty_caller_secret_is_repo_setting_block():
    from robotsix_mill.stages.ci_infra_block import REPO_SETTING_BLOCK_MARKER

    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("release / release-please", "Create app token")],
        wf_text=_RELEASE_WF,
        log_text=_EMPTY_APP_ID_LOG,
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.action == "block"
    assert v.note.startswith(REPO_SETTING_BLOCK_MARKER)
    assert "Not fixable from this repo" not in v.note
    assert v.setting == "secret RELEASE_APP_PRIVATE_KEY / variable RELEASE_APP_ID"
    assert "add secret RELEASE_APP_PRIVATE_KEY / variable RELEASE_APP_ID on" in v.note


def test_repo_setting_block_names_green_sibling():
    me = FakeForge(
        runs=[_RED],
        jobs=[_job("release / release-please", "Create app token")],
        wf_text=_RELEASE_WF,
        log_text=_EMPTY_APP_ID_LOG,
    )
    sib = FakeForge(
        runs=[_release_run(7, "success", "2026-10-26T10:20:00Z")],
        wf_text=_RELEASE_WF,
    )
    v = evaluate_stale_ci(
        me,
        _ticket(),
        _body(),
        stale_days=14,
        now=NOW,
        siblings=lambda: [("o/robotsix-browser", sib)],
    )
    assert v is not None and v.setting
    assert "(see o/robotsix-browser" in v.note


def test_input_required_message_is_repo_setting_block():
    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("ci / Audit", "Audit dependencies")],
        wf_text=_REUSABLE_WF,
        log_text="##[error]Input required and not supplied: token\n",
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.setting == "input token"


def test_missing_required_secret_from_reusable_contract():
    class ContractForge(FakeForge):
        def _get_workflow_file(self, *, owner, repo, path):
            assert (repo, path) == (
                "robotsix-github-workflows",
                ".github/workflows/python-ci.yml",
            )
            return (
                "on:\n  workflow_call:\n    secrets:\n      CODECOV_TOKEN:\n"
                "        required: true\n"
            )

    forge = ContractForge(
        runs=[_RED],
        jobs=[_job("ci / Audit", "Audit dependencies")],
        wf_text=_REUSABLE_WF,
    )
    v = evaluate_stale_ci(forge, _ticket(), _body(), stale_days=14, now=NOW)
    assert v is not None and v.setting == "secret CODECOV_TOKEN"


def test_reusable_failing_on_every_caller_stays_upstream_only():
    me = FakeForge(
        runs=[_RED],
        jobs=[_job("release / release-please", "Release")],
        wf_text=_RELEASE_WF,
        log_text="##[group]Run release\n##[error]upstream boom\n",
    )
    sib = FakeForge(
        runs=[_release_run(7, "failure", "2026-10-26T10:20:00Z")],
        wf_text=_RELEASE_WF,
    )
    v = evaluate_stale_ci(
        me, _ticket(), _body(), stale_days=14, now=NOW, siblings=lambda: [("s", sib)]
    )
    assert v is not None and v.setting is None
    assert v.note.startswith(
        f"Not fixable from this repo: failure is internal to {_RELEASE_REF}"
    )


def test_green_sibling_refuses_upstream_only():
    me = FakeForge(
        runs=[_RED],
        jobs=[_job("release / release-please", "Release")],
        wf_text=_RELEASE_WF,
    )
    sib = FakeForge(
        runs=[_release_run(7, "success", "2026-10-26T10:30:00Z")],
        wf_text=_RELEASE_WF,
    )
    assert (
        evaluate_stale_ci(
            me,
            _ticket(),
            _body(),
            stale_days=14,
            now=NOW,
            siblings=lambda: [("s", sib)],
        )
        is None
    )
    # A green sibling run outside the hour window does not count.
    sib.runs = [_release_run(7, "success", "2026-10-26T13:00:00Z")]
    v = evaluate_stale_ci(
        me, _ticket(), _body(), stale_days=14, now=NOW, siblings=lambda: [("s", sib)]
    )
    assert v is not None and v.action == "block"


def test_filing_repo_setting_block_escalates_once(settings, service, monkeypatch):
    from robotsix_mill.agents.runners import repo_setting_block_runner as rsb

    sent: list[str] = []
    monkeypatch.setattr(
        rsb, "_default_escalate", lambda repo, setting, tid, msg=None: sent.append(msg)
    )
    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("release / release-please", "Create app token")],
        wf_text=_RELEASE_WF,
        log_text=_EMPTY_APP_ID_LOG,
    )
    for _ in range(2):
        t = service.create(
            title="CI failure: CI on main", description=_body(), source=SourceKind.CI
        )
        _worker(settings)._apply_stale_ci_gate(service, forge, t, _body())
        assert service.get(t.id).state is State.BLOCKED
    assert len(sent) == 1 and "RELEASE_APP_ID" in sent[0]


# --- refine: an already-green workflow never reaches READY -------------------


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


def _green_forge(monkeypatch, forge):
    import robotsix_mill.scm.forge as forge_mod

    monkeypatch.setattr(forge_mod, "get_forge", lambda *a, **kw: forge)


def test_refine_closes_already_green_ci_ticket(ctx, service, monkeypatch):
    from robotsix_mill.stages.refine import RefineStage

    _green_forge(
        monkeypatch,
        FakeForge(runs=[_run(101, NEW_SHA, "success", "2026-10-26T12:00:00Z"), _RED]),
    )

    def _no_clone(*a, **kw):  # the gate must short-circuit before any clone
        raise AssertionError("refine reached the clone phase")

    monkeypatch.setattr(RefineStage, "_clone_or_resume", staticmethod(_no_clone))
    t = service.create(
        title="CI failure: CI on main", description=_body(), source=SourceKind.CI
    )
    outcome = RefineStage().run(t, ctx)
    assert outcome.next_state is State.DONE
    assert outcome.note.startswith("Stale CI failure:")
    assert outcome.next_state not in (State.READY, State.HUMAN_ISSUE_APPROVAL)


def test_refine_blocks_upstream_only_ci_ticket(ctx, service, monkeypatch):
    from robotsix_mill.stages.refine import RefineStage

    _green_forge(
        monkeypatch,
        FakeForge(
            runs=[_RED],
            jobs=[_job("ci / Audit", "Audit dependencies")],
            wf_text=_REUSABLE_WF,
        ),
    )
    t = service.create(
        title="CI failure: CI on main", description=_body(), source=SourceKind.CI
    )
    outcome = RefineStage().run(t, ctx)
    assert outcome.next_state is State.BLOCKED
    assert outcome.note.startswith("Not fixable from this repo:")


def test_check_ticket_swallows_forge_errors(settings, repo_config, monkeypatch):
    import robotsix_mill.scm.forge as forge_mod

    def _boom(*a, **kw):
        raise RuntimeError("no forge")

    monkeypatch.setattr(forge_mod, "get_forge", _boom)
    assert ci_stale_gate.check_ticket(settings, repo_config, _ticket(), _body()) is None


# --- CI-monitor filing ---------------------------------------------------------


def _worker(settings):
    from robotsix_mill.runtime.worker.poll_loops_ci import PollLoopsCiMixin

    w = PollLoopsCiMixin.__new__(PollLoopsCiMixin)
    w.ctx = SimpleNamespace(settings=settings)
    return w


def test_filing_closes_already_green_ticket(settings, service):
    t = service.create(
        title="CI failure: CI on main", description=_body(), source=SourceKind.CI
    )
    forge = FakeForge(
        runs=[_run(101, NEW_SHA, "success", "2026-10-26T12:00:00Z"), _RED]
    )
    _worker(settings)._apply_stale_ci_gate(service, forge, t, _body())
    assert service.get(t.id).state is State.DONE


def test_filing_blocks_upstream_only_ticket(settings, service):
    t = service.create(
        title="CI failure: CI on main", description=_body(), source=SourceKind.CI
    )
    forge = FakeForge(
        runs=[_RED],
        jobs=[_job("ci / Audit", "Audit dependencies")],
        wf_text=_REUSABLE_WF,
    )
    _worker(settings)._apply_stale_ci_gate(service, forge, t, _body())
    assert service.get(t.id).state is State.BLOCKED


def test_filing_leaves_real_failure_in_draft(settings, service):
    t = service.create(
        title="CI failure: CI on main", description=_body(), source=SourceKind.CI
    )
    _worker(settings)._apply_stale_ci_gate(service, FakeForge(runs=[_RED]), t, _body())
    assert service.get(t.id).state is State.DRAFT

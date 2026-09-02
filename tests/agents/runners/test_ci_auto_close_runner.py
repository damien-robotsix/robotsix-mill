"""Tests for ``runners.ci_auto_close_runner``.

Real :class:`TicketService` on a ``tmp_path`` SQLite DB; forge is mocked.
"""

from __future__ import annotations

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import ci_auto_close_runner as car
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

_BOARD = "test-board"


class _FakeForge:
    def __init__(self, runs):
        self._runs = runs

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        return self._runs


def _prepare(tmp_path, monkeypatch, runs=None):
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path / "data"), require_approval="false")
    db.init_db(settings, board_id=_BOARD)
    _reset_repos_config()
    rc = RepoConfig(
        repo_id="test-repo",
        board_id=_BOARD,
        langfuse_project_name="t",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    _cfg._repos_config = ReposRegistry(repos={rc.repo_id: rc})
    monkeypatch.setattr(
        car, "get_forge", lambda settings, repo_config=None: _FakeForge(runs or [])
    )
    return settings, TicketService(settings, board_id=_BOARD)


def _ci_ticket(
    service,
    settings,
    wf="ci / tests",
    branch="main",
    f_sha="abc123",
    run_id=100,
    state=State.DRAFT,
    has_branch=False,
):
    body = (
        f"**Workflow:** {wf}\n**Path:** .github/workflows/ci.yml\n"
        f"**Branch:** {branch}\n**Run:** [{run_id}](https://x/run/{run_id})\n"
        f"**Commit:** `{f_sha}`\n**Created:** 2026-09-01T00:00:00Z\n\nlogs..."
    )
    t = service.create(
        title=f"CI failure: {wf} on {branch}",
        description=body,
        source=SourceKind.CI,
    )
    if has_branch:
        from robotsix_mill.core import db as _db
        from robotsix_mill.core.models import Ticket

        with _db.session(settings, _BOARD) as s:
            row = s.get(Ticket, t.id)
            row.branch = "ci-fix-1"
            s.add(row)
            s.commit()
    if state is State.DRAFT:
        return t
    if state is State.HUMAN_ISSUE_APPROVAL:
        service.transition(t.id, State.HUMAN_ISSUE_APPROVAL)
    elif state is State.BLOCKED:
        service.transition(t.id, State.READY)
        service.transition(t.id, State.BLOCKED, note="ci fix failed")
    return service.get(t.id)


def _run(settings):
    return car.run_ci_auto_close(settings)


def test_red_then_fixed_two_consecutive_greens_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "cafe01",
                "conclusion": "success",
                "id": 300,
                "html_url": "https://x/run/300",
                "created_at": "2026-09-02T03:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.DRAFT)

    result = _run(settings)

    assert result["closed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.DONE
    events = service.history(t.id)
    done = [e for e in events if e.state is State.DONE]
    assert done and "https://x/run/300" in done[-1].note


def test_single_green_on_new_head_fix_identifiable_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_still_red_not_closed(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "failure",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_single_green_same_commit_not_closed(tmp_path, monkeypatch):
    # One green on the SAME failing commit — no fix commit identifiable → need 2.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_blocked_ticket_closes_via_mark_done(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.BLOCKED)

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_human_issue_approval_ticket_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.HUMAN_ISSUE_APPROVAL)

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_ticket_with_branch_not_closed(tmp_path, monkeypatch):
    # A branch means open MR with unmerged work — keep normal flow.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", has_branch=True)

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_non_ci_ticket_not_touched(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
        ],
    )
    t = service.create(title="some task", description="body", source=SourceKind.USER)

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_parse_workflow_branch_from_title_fallback():
    from types import SimpleNamespace

    ticket = SimpleNamespace(title="CI failure: ci / tests on main")
    wf, branch = car._parse_workflow_branch(ticket, "")
    assert wf == "ci / tests"
    assert branch == "main"

"""Tests for ``runners.infra_account_block_runner``.

Real :class:`TicketService` on ``tmp_path`` SQLite DBs; only the forge and
the repos registry are faked.  The scenario is the 2026-09-18 outage:
GitHub disabled hosted runners for the account, so tickets across several
private-repo boards parked on the account/billing block.  The pass must
escalate ONCE fleet-wide (not per ticket) and auto-resume each ticket once
its repo's hosted CI succeeds again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import infra_account_block_runner as iab
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages.ci_infra_block import infra_account_block_note


class _FakeForge:
    def __init__(self, *, conclusion, head_sha="a0985df8abc"):
        self.conclusion = conclusion
        self.head_sha = head_sha
        self.updated_branches: list[str] = []

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        if not self.head_sha:
            return []
        return [{"id": 2, "head_sha": self.head_sha, "conclusion": self.conclusion}]

    def commit_ci_conclusion(self, *, sha):
        if self.conclusion is None:
            return None
        return {"conclusion": self.conclusion, "failing": [], "pending": []}

    def update_branch(self, *, source_branch):
        self.updated_branches.append(source_branch)
        return {"updated": True}


def _prepare(tmp_path, monkeypatch, forge, *, boards):
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path / "data"), require_approval="false")
    _reset_repos_config()
    repos: dict[str, RepoConfig] = {}
    services: dict[str, TicketService] = {}
    for board in boards:
        db.init_db(settings, board_id=board)
        rc = RepoConfig(
            repo_id=f"repo-{board}",
            board_id=board,
            langfuse_project_name="t",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        repos[rc.repo_id] = rc
        services[board] = TicketService(settings, board_id=board)
    _cfg._repos_config = ReposRegistry(repos=repos)
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge", lambda s, repo_config=None: forge
    )
    return settings, services


def _parked(service, title, branch):
    t = service.create(title=title, description="spec body long enough to count")
    service.transition(t.id, State.READY, note="approved")
    service.set_branch(t.id, branch)
    service.transition(t.id, State.BLOCKED, note=infra_account_block_note())
    return service.get(t.id)


def test_single_escalation_for_many_tickets_across_repos(tmp_path, monkeypatch):
    """N tickets across M repos sharing the condition → exactly ONE escalation."""
    forge = _FakeForge(conclusion="failure")  # target still red → stay parked
    settings, services = _prepare(
        tmp_path, monkeypatch, forge, boards=["board-a", "board-b"]
    )
    _parked(services["board-a"], "a1", "mill/a1")
    _parked(services["board-a"], "a2", "mill/a2")
    _parked(services["board-b"], "b1", "mill/b1")

    calls: list[tuple[list[str], str | None]] = []

    result = iab.run_infra_account_block_recovery(
        settings, escalate=lambda repos, first: calls.append((repos, first))
    )

    assert result["escalated"] == 1
    assert len(calls) == 1
    repos, first_observed = calls[0]
    assert repos == ["repo-board-a", "repo-board-b"]  # deduped + sorted
    assert first_observed is not None  # names the first-observed timestamp
    assert result["resumed"] == 0
    assert result["still_parked"] == 3


def test_reescalation_deduped_within_window(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="failure")
    settings, services = _prepare(tmp_path, monkeypatch, forge, boards=["board-a"])
    _parked(services["board-a"], "a1", "mill/a1")

    now = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    calls: list[list[str]] = []

    def esc(repos, first):
        calls.append(repos)

    iab.run_infra_account_block_recovery(settings, escalate=esc, now=now)
    # A minute later, still within the 24h window → no re-escalation.
    iab.run_infra_account_block_recovery(
        settings, escalate=esc, now=now + timedelta(minutes=1)
    )
    assert len(calls) == 1
    # A day later, still unresolved → re-escalate.
    iab.run_infra_account_block_recovery(
        settings, escalate=esc, now=now + timedelta(days=1, seconds=1)
    )
    assert len(calls) == 2


def test_tickets_resume_when_hosted_ci_green_again(tmp_path, monkeypatch):
    """AC: a ticket blocked this way resumes when a later hosted run succeeds."""
    forge = _FakeForge(conclusion="success")
    settings, services = _prepare(tmp_path, monkeypatch, forge, boards=["board-a"])
    t = _parked(services["board-a"], "a1", "mill/a1")

    result = iab.run_infra_account_block_recovery(
        settings, escalate=lambda repos, first: None
    )

    assert result["resumed"] == 1
    fresh = services["board-a"].get(t.id)
    assert fresh.state is State.READY
    assert forge.updated_branches == ["mill/a1"]
    assert any(
        "hosted CI on `main` succeeded again" in (c.body or "")
        for c in services["board-a"].list_comments(t.id)
    )


def test_unrelated_blocks_are_ignored_no_escalation(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="failure")
    settings, services = _prepare(tmp_path, monkeypatch, forge, boards=["board-a"])
    svc = services["board-a"]
    t = svc.create("x", "spec body long enough to count")
    svc.transition(t.id, State.READY, note="approved")
    svc.transition(t.id, State.BLOCKED, note="ci fix agent could not turn CI green")

    calls: list[list[str]] = []
    result = iab.run_infra_account_block_recovery(
        settings, escalate=lambda repos, first: calls.append(repos)
    )

    assert result == {"escalated": 0, "resumed": 0, "still_parked": 0, "skipped": 0}
    assert calls == []
    assert svc.get(t.id).state is State.BLOCKED

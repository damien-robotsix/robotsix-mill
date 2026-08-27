"""Tests for ``runners.upstream_ci_recovery_runner``.

Real :class:`TicketService` on a ``tmp_path`` SQLite DB; only the forge and
the repos registry are faked.  The scenario is the 2026-08-26 robotsix-chat
one: tickets parked by ``_check_upstream_ci_breakage`` while ``main`` was
red, ``main`` goes green, nothing resumed them.
"""

from __future__ import annotations

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import upstream_ci_recovery_runner as ucr
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER

_BOARD = "test-board"

_PARK_NOTE = (
    f"{UPSTREAM_CI_BLOCK_MARKER}: the following check(s) are failing on both "
    "this PR **and** the target branch `main` (644029d1): Container image "
    "scan (Trivy). The target branch CI is broken — this PR's changes are "
    "not the cause."
)


class _FakeForge:
    def __init__(self, *, conclusion, head_sha="a0985df8abc", update_result=None):
        self.conclusion = conclusion
        self.head_sha = head_sha
        self.update_result = update_result or {"updated": True}
        self.updated_branches: list[str] = []
        self.ccc_shas: list[str] = []

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        if not self.head_sha:
            return []
        return [
            {"id": 2, "head_sha": self.head_sha, "conclusion": "success"},
            {"id": 1, "head_sha": "older000", "conclusion": "failure"},
        ]

    def commit_ci_conclusion(self, *, sha):
        self.ccc_shas.append(sha)
        if self.conclusion is None:
            return None
        return {"conclusion": self.conclusion, "failing": [], "pending": []}

    def update_branch(self, *, source_branch):
        self.updated_branches.append(source_branch)
        return self.update_result


def _prepare(tmp_path, monkeypatch, forge):
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
        "robotsix_mill.forge.get_forge", lambda s, repo_config=None: forge
    )
    return settings, TicketService(settings, board_id=_BOARD)


def _parked_ticket(service, title="t", note=_PARK_NOTE, branch="mill/t"):
    t = service.create(title=title, description="spec body long enough to count")
    service.transition(t.id, State.READY, note="approved")
    service.set_branch(t.id, branch)
    service.transition(t.id, State.BLOCKED, note=note)
    return service.get(t.id)


def test_parked_tickets_resume_when_target_is_green(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="success")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    a = _parked_ticket(service, "a", branch="mill/a")
    b = _parked_ticket(service, "b", branch="mill/b")
    assert a.state is State.BLOCKED and b.state is State.BLOCKED

    result = ucr.run_upstream_ci_recovery(settings)

    assert result == {"resumed": 2, "still_parked": 0, "skipped": 0}
    for t in (a, b):
        fresh = service.get(t.id)
        assert fresh.state is State.READY, "resume goes back to blocked_from"
        assert fresh.blocked_from is None
    # The PR branch was refreshed against the green base so its CI re-runs.
    assert sorted(forge.updated_branches) == ["mill/a", "mill/b"]
    # The guard's own status call was used, on the target's current head.
    assert forge.ccc_shas == ["a0985df8abc"]
    # The resume note names the target commit and the refresh outcome.
    comments = service.list_comments(a.id)
    assert any(
        "green again (a0985df8)" in (c.body or "")
        and "PR branch updated" in (c.body or "")
        for c in comments
    )


def test_red_target_keeps_tickets_parked(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="failure")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    t = _parked_ticket(service)

    result = ucr.run_upstream_ci_recovery(settings)

    assert result == {"resumed": 0, "still_parked": 1, "skipped": 0}
    assert service.get(t.id).state is State.BLOCKED
    assert forge.updated_branches == []


def test_pending_or_unknown_target_status_keeps_tickets_parked(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="pending")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    t = _parked_ticket(service)
    assert ucr.run_upstream_ci_recovery(settings)["still_parked"] == 1
    assert service.get(t.id).state is State.BLOCKED

    forge.conclusion = None  # CI status unavailable
    assert ucr.run_upstream_ci_recovery(settings)["still_parked"] == 1
    assert service.get(t.id).state is State.BLOCKED

    forge.conclusion = "success"
    forge.head_sha = ""  # target has no runs at all
    assert ucr.run_upstream_ci_recovery(settings)["still_parked"] == 1
    assert service.get(t.id).state is State.BLOCKED


def test_other_blocked_tickets_are_left_alone(tmp_path, monkeypatch):
    """Only the LATEST blocked note decides; unrelated blocks are untouched."""
    forge = _FakeForge(conclusion="success")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    other = _parked_ticket(
        service, "other", note="ci fix agent could not turn CI green"
    )
    # Parked upstream once, then resumed and blocked again for another reason.
    reblocked = _parked_ticket(service, "reblocked")
    service.resume_blocked(reblocked.id)
    service.transition(
        reblocked.id, State.BLOCKED, note="spec unchanged since last attempt"
    )

    result = ucr.run_upstream_ci_recovery(settings)

    assert result["resumed"] == 0
    assert service.get(other.id).state is State.BLOCKED
    assert service.get(reblocked.id).state is State.BLOCKED
    # No forge traffic at all when nothing on the board is upstream-parked.
    assert forge.ccc_shas == []


def test_multi_repo_prefixed_note_is_recognised(tmp_path, monkeypatch):
    """The merge path prefixes the note with ``[repo_id] ``."""
    forge = _FakeForge(conclusion="success")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    t = _parked_ticket(service, note=f"[robotsix-chat] {_PARK_NOTE}")
    assert ucr.run_upstream_ci_recovery(settings)["resumed"] == 1
    assert service.get(t.id).state is State.READY


def test_update_branch_failure_does_not_prevent_resume(tmp_path, monkeypatch):
    forge = _FakeForge(
        conclusion="success",
        update_result={"updated": False, "reason": "not supported"},
    )
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    t = _parked_ticket(service)
    assert ucr.run_upstream_ci_recovery(settings)["resumed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.READY
    assert any(
        "PR branch not updated (not supported)" in (c.body or "")
        for c in service.list_comments(t.id)
    )


def test_board_without_repo_config_is_skipped_not_crashed(tmp_path, monkeypatch):
    forge = _FakeForge(conclusion="success")
    settings, service = _prepare(tmp_path, monkeypatch, forge)
    t = _parked_ticket(service)
    _cfg._repos_config = ReposRegistry(repos={})
    result = ucr.run_upstream_ci_recovery(settings)
    assert result == {"resumed": 0, "still_parked": 0, "skipped": 1}
    assert service.get(t.id).state is State.BLOCKED

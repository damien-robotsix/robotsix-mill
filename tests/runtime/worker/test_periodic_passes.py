"""Tests for the CI-debt auto-resume periodic pass."""

from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

# The function under test.
from robotsix_mill.runtime.worker.periodic_passes import _ci_debt_recheck_pass


CI_DEBT_NOTE = (
    "CI blocked by pre-existing target-branch debt: workflow(s) "
    "lint, test are failing on the merge target too and were not "
    "introduced by this PR. Operator must stabilise the target "
    "branch's CI before this can merge."
)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=str(tmp_path))


@pytest.fixture
def repo_config():
    return RepoConfig(
        repo_id="test-repo",
        
        forge_remote_url="https://github.com/test/repo",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


@pytest.fixture
def svc(settings, repo_config):
    return TicketService(settings, board_id=repo_config.board_id)


def _make_blocked_ticket(svc: TicketService, note: str) -> Ticket:
    """Create a ticket, transition it through the pipeline to
    IMPLEMENT_COMPLETE, then to BLOCKED (so blocked_from =
    IMPLEMENT_COMPLETE, enabling the resume-to-originating-state path)."""
    t = svc.create("Test ticket", "Test body")
    svc.transition(t.id, State.READY, note="refined")
    svc.transition(t.id, State.DOCUMENTING, note="docs")
    svc.transition(t.id, State.DELIVERABLE, note="deliverable")
    svc.transition(t.id, State.IMPLEMENT_COMPLETE, note="merge stage started")
    svc.transition(t.id, State.BLOCKED, note=note)
    return svc.get(t.id)


def _mock_forge(conclusions: dict[str, str]) -> MagicMock:
    """Return a mock Forge whose ``list_workflow_runs`` returns one run
    per workflow name with the given conclusion."""
    forge = MagicMock()
    runs = [
        {
            "id": i + 1,
            "name": name,
            "workflow_id": i + 100,
            "conclusion": conclusion,
            "head_sha": "abc123",
            "html_url": f"https://github.com/test/repo/actions/runs/{i + 1}",
            "created_at": "2025-01-01T00:00:00Z",
            "event": "push",
            "head_branch": "main",
            "path": ".github/workflows/ci.yml",
        }
        for i, (name, conclusion) in enumerate(conclusions.items())
    ]
    forge.list_workflow_runs.return_value = runs
    return forge


# ---------------------------------------------------------------------------
# Auto-resume: all workflows green
# ---------------------------------------------------------------------------


def test_all_workflows_green_transitions_to_implement_complete(
    settings, repo_config, svc, monkeypatch
):
    """When all named workflows are green on the target branch, the
    ticket transitions from BLOCKED to IMPLEMENT_COMPLETE."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    forge = _mock_forge({"lint": "success", "test": "success"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.IMPLEMENT_COMPLETE


def test_mixed_conclusions_some_green_stays_blocked(
    settings, repo_config, svc, monkeypatch
):
    """When some workflows are green but one is still failing, the
    ticket stays BLOCKED."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    forge = _mock_forge({"lint": "success", "test": "failure"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED


def test_all_workflows_failing_stays_blocked(settings, repo_config, svc, monkeypatch):
    """When all workflows are still failing, the ticket stays BLOCKED."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    forge = _mock_forge({"lint": "failure", "test": "failure"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED


def test_neutral_and_skipped_conclusions_are_green(
    settings, repo_config, svc, monkeypatch
):
    """'neutral' and 'skipped' conclusions also count as green."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    forge = _mock_forge({"lint": "neutral", "test": "skipped"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.IMPLEMENT_COMPLETE


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


def test_no_matching_note_skipped(settings, repo_config, svc, monkeypatch):
    """A BLOCKED ticket without the CI-debt note pattern is left alone."""
    t = svc.create("Some other blocked ticket", "body")
    svc.transition(t.id, State.BLOCKED, note="Something else blocked this")

    forge = _mock_forge({"lint": "success"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED


def test_missing_workflow_run_stays_blocked(settings, repo_config, svc, monkeypatch):
    """When a named workflow has no recent run, the ticket stays BLOCKED."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    # Only 'lint' has a run; 'test' is missing.
    forge = _mock_forge({"lint": "success"})
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED


def test_list_workflow_runs_error_survives(settings, repo_config, svc, monkeypatch):
    """When forge.list_workflow_runs raises, the pass skips the ticket
    (logs a warning) but does NOT crash."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)

    forge = MagicMock()
    forge.list_workflow_runs.side_effect = RuntimeError("API down")
    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda *a, **kw: forge,
    )

    # Must not raise.
    _ci_debt_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED

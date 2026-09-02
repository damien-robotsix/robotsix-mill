"""Tests for the CI-debt auto-resume periodic pass."""

from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

# The function under test.
from robotsix_mill.runtime.worker.periodic_passes import _blocked_recheck_pass

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
        board_id="test-board",
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


def _annotate(settings, repo_config, ticket_id: str, note: str) -> None:
    """Append a same-state event to a BLOCKED ticket (a trace link etc.)."""
    from robotsix_mill.core import db as core_db
    from robotsix_mill.core.service._helpers import _make_event

    with core_db.session(settings, repo_config.board_id) as s:
        s.add(_make_event(s, ticket_id=ticket_id, state=State.BLOCKED, note=note))
        s.commit()


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

    _blocked_recheck_pass(settings, repo_config)

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

    _blocked_recheck_pass(settings, repo_config)

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

    _blocked_recheck_pass(settings, repo_config)

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

    _blocked_recheck_pass(settings, repo_config)

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

    _blocked_recheck_pass(settings, repo_config)

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

    _blocked_recheck_pass(settings, repo_config)

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
    _blocked_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.BLOCKED


# ---------------------------------------------------------------------------
# The block reason is the transition INTO blocked, not the last note
# ---------------------------------------------------------------------------
#
# A ticket keeps accumulating events while it sits in BLOCKED — trace
# links, operator comments, same-state re-polls — and they are all
# recorded with state=blocked. Reading the tail (or merely the last
# blocked-state note) mistook one of those annotations for the block
# reason, so the debt note stopped matching and the ticket became
# invisible to this pass permanently.


def test_debt_note_survives_a_later_annotation(settings, repo_config, svc, monkeypatch):
    """The regression: an appended trace note must not hide the debt."""
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)
    # A trace link appended while the ticket sits in BLOCKED. This is an
    # annotation, not a transition (blocked -> blocked is not a legal
    # edge), so it is written straight to the event log the way the
    # tracing path writes it — same state, later id.
    _annotate(settings, repo_config, t.id, "🔍 [Trace: review](https://lf/x)")

    forge = _mock_forge({"lint": "success", "test": "success"})
    monkeypatch.setattr("robotsix_mill.forge.get_forge", lambda *a, **kw: forge)

    _blocked_recheck_pass(settings, repo_config)

    assert svc.get(t.id).state == State.IMPLEMENT_COMPLETE


def test_a_newer_unrelated_block_reason_wins(settings, repo_config, svc, monkeypatch):
    """Re-blocking for a different reason must NOT be auto-resumed.

    The debt is history at that point; the ticket is now held by
    something this pass knows nothing about.
    """
    t = _make_blocked_ticket(svc, CI_DEBT_NOTE)
    svc.transition(t.id, State.IMPLEMENT_COMPLETE, note="debt cleared")
    svc.transition(t.id, State.BLOCKED, note="Blocked partly on CodeQL code-scanning")

    forge = _mock_forge({"lint": "success", "test": "success"})
    monkeypatch.setattr("robotsix_mill.forge.get_forge", lambda *a, **kw: forge)

    _blocked_recheck_pass(settings, repo_config)

    assert svc.get(t.id).state == State.BLOCKED


# ---------------------------------------------------------------------------
# Structured block reasons (machine-checkable, not prose)
# ---------------------------------------------------------------------------
#
# New blocks carry a ``block_reason`` JSON on the Ticket
# ({"kind": "target_branch_red", "workflows": [...]}) that the rechecker
# reads directly instead of substring-matching the note. This is what
# fixes the 2026-09-02 incident: the two tickets' prose note omitted the
# canonical "too" suffix, so the legacy regex missed them and they stayed
# parked after main went green.


def _make_blocked_ticket_reason(svc, note, block_reason):
    """Block a ticket through IMPLEMENT_COMPLETE with a structured reason."""
    t = svc.create("Test ticket", "Test body")
    svc.transition(t.id, State.READY, note="refined")
    svc.transition(t.id, State.DOCUMENTING, note="docs")
    svc.transition(t.id, State.DELIVERABLE, note="deliverable")
    svc.transition(t.id, State.IMPLEMENT_COMPLETE, note="merge stage started")
    svc.transition(t.id, State.BLOCKED, note=note, block_reason=block_reason)
    return svc.get(t.id)


def test_structured_reason_resumes_with_evidence(
    settings, repo_config, svc, monkeypatch
):
    """A structured target_branch_red block resumes when green, and the
    resume note names the run IDs as evidence."""
    from robotsix_mill.core.block_reason import TARGET_BRANCH_RED, encode

    # Prose note that does NOT match the legacy regex (no "too" suffix) —
    # exactly the shape that stranded the 2026-09-02 tickets. The
    # structured reason is what the rechecker trusts.
    note = (
        "CI blocked by pre-existing target-branch debt: workflow(s) "
        "CI, Security Audit are failing on the merge target"
    )
    t = _make_blocked_ticket_reason(
        svc, note, encode(TARGET_BRANCH_RED, workflows=["CI", "Security Audit"])
    )

    forge = _mock_forge({"CI": "success", "Security Audit": "success"})
    monkeypatch.setattr("robotsix_mill.forge.get_forge", lambda *a, **kw: forge)

    _blocked_recheck_pass(settings, repo_config)

    updated = svc.get(t.id)
    assert updated.state == State.IMPLEMENT_COMPLETE
    # The resume note names the evidence (workflow run IDs).
    events = svc.history(t.id, order="desc")
    resume = next(
        (ev.note or "" for ev in events if ev.state is State.IMPLEMENT_COMPLETE),
        "",
    )
    assert "CI#1" in resume and "Security Audit#2" in resume


def test_structured_unknown_kind_is_skipped(settings, repo_config, svc, monkeypatch):
    """A structured reason of an unhandled kind is never resumed (the
    rechecker only resumes what it can positively verify cleared)."""
    from robotsix_mill.core.block_reason import encode

    t = _make_blocked_ticket_reason(
        svc, "blocked", encode("some_future_kind", provider="x")
    )

    forge = _mock_forge({"CI": "success"})
    monkeypatch.setattr("robotsix_mill.forge.get_forge", lambda *a, **kw: forge)

    _blocked_recheck_pass(settings, repo_config)

    assert svc.get(t.id).state == State.BLOCKED


def test_structured_still_red_stays_blocked(settings, repo_config, svc, monkeypatch):
    """A structured target_branch_red block with a still-red workflow stays
    BLOCKED."""
    from robotsix_mill.core.block_reason import TARGET_BRANCH_RED, encode

    t = _make_blocked_ticket_reason(
        svc, "blocked", encode(TARGET_BRANCH_RED, workflows=["lint"])
    )

    forge = _mock_forge({"lint": "failure"})
    monkeypatch.setattr("robotsix_mill.forge.get_forge", lambda *a, **kw: forge)

    _blocked_recheck_pass(settings, repo_config)

    assert svc.get(t.id).state == State.BLOCKED


def test_resume_clears_block_reason(settings, repo_config, svc):
    """Leaving BLOCKED via resume_blocked clears the structured reason."""
    from robotsix_mill.core.block_reason import TARGET_BRANCH_RED, encode

    t = _make_blocked_ticket_reason(
        svc, "blocked", encode(TARGET_BRANCH_RED, workflows=["lint"])
    )
    assert svc.get(t.id).block_reason is not None

    svc.resume_blocked(t.id, note="operator resume")

    updated = svc.get(t.id)
    assert updated.state != State.BLOCKED
    assert updated.block_reason is None

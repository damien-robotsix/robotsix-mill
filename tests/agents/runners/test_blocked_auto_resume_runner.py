"""Tests for ``runners.blocked_auto_resume_runner``.

Real :class:`TicketService` on a ``tmp_path`` SQLite DB; no forge, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import blocked_auto_resume_runner as bar
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


def _prepare(tmp_path, **env):
    db.reset_engine()
    settings = Settings(
        data_dir=str(tmp_path / "data"), require_approval="false", **env
    )
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
    return settings, TicketService(settings, board_id=_BOARD)


def _blocked_ticket(service, note, title="t"):
    t = service.create(title=title, description="spec body long enough to count")
    service.transition(t.id, State.READY, note="approved")
    service.transition(t.id, State.BLOCKED, note=note)
    return service.get(t.id)


_LATER = datetime.now(UTC) + timedelta(hours=2)


def test_resumable_note_is_resumed_after_cooldown(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(
        service, "agent error — resumable: You've hit your session limit"
    )

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["resumed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.READY
    assert fresh.blocked_from is None
    comments = service.list_comments(t.id)
    assert any((c.body or "").startswith("[auto-resume 1/1]") for c in comments)


def test_fresh_block_is_left_cooling(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(service, "stage timed out after 2400s — resumable")

    result = bar.run_blocked_auto_resume(settings)  # now ≈ block time

    assert result == {
        "resumed": 0,
        "cooling": 1,
        "budget_exhausted": 0,
        "not_matched": 0,
        "cycles_escalated": 0,
    }
    assert service.get(t.id).state is State.BLOCKED


def test_budget_is_one_resume_per_ticket_by_default(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(
        service, "ci fix agent could not turn CI green within its iteration budget"
    )

    assert bar.run_blocked_auto_resume(settings, now=_LATER)["resumed"] == 1
    # …it blocks again for the same reason
    service.transition(
        t.id,
        State.BLOCKED,
        note="ci fix agent could not turn CI green within its iteration budget",
    )

    result = bar.run_blocked_auto_resume(settings, now=_LATER + timedelta(hours=2))

    assert result["resumed"] == 0
    assert result["budget_exhausted"] == 1
    assert service.get(t.id).state is State.BLOCKED


def test_fingerprint_and_upstream_parks_are_never_touched(tmp_path):
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(
        service,
        "implement — resumable: spec unchanged since last spec-determined implement attempt",
        "a",
    )
    b = _blocked_ticket(
        service, f"{UPSTREAM_CI_BLOCK_MARKER}: main is red — resumable", "b"
    )
    c = _blocked_ticket(service, "scope triage REJECT — operator decision needed", "c")

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["resumed"] == 0
    assert result["not_matched"] == 3
    for t in (a, b, c):
        assert service.get(t.id).state is State.BLOCKED


def test_disabled_when_no_patterns_or_zero_budget(tmp_path):
    settings, service = _prepare(tmp_path, blocked_auto_resume_max_per_ticket=0)
    _blocked_ticket(service, "agent error — resumable: boom")
    assert bar.run_blocked_auto_resume(settings, now=_LATER)["resumed"] == 0


def test_matches_helper_is_case_insensitive_and_ignores_bad_patterns():
    assert bar._matches("Agent Error — Resumable: x", ["agent error — resumable"])
    assert not bar._matches("agent error — resumable: spec unchanged", ["— resumable"])
    assert bar._matches("stage timed out", ["([unclosed", "timed out"])


def _cycle_marker_comments(service, ticket_id):
    return [
        c
        for c in service.list_comments(ticket_id)
        if (c.body or "").startswith(bar.CYCLE_MARKER)
    ]


def test_two_ticket_cycle_is_escalated_not_resumed(tmp_path):
    """A ↔ B dependency deadlock: both members are escalated with a
    cycle-marker comment and are NOT auto-resumed, even though their block
    notes match a resumable pattern."""
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(service, "agent error — resumable: boom", "a")
    b = _blocked_ticket(service, "agent error — resumable: boom", "b")
    service.set_depends_on(a.id, [b.id])
    service.set_depends_on(b.id, [a.id])

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["cycles_escalated"] == 2
    assert result["resumed"] == 0
    assert result["not_matched"] == 0
    for t in (a, b):
        assert service.get(t.id).state is State.BLOCKED
        assert len(_cycle_marker_comments(service, t.id)) == 1


def test_n_ticket_cycle_marks_every_member(tmp_path):
    """A → B → C → A is found and all three members are escalated."""
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(service, "no matching reason", "a")
    b = _blocked_ticket(service, "no matching reason", "b")
    c = _blocked_ticket(service, "no matching reason", "c")
    service.set_depends_on(a.id, [b.id])
    service.set_depends_on(b.id, [c.id])
    service.set_depends_on(c.id, [a.id])

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["cycles_escalated"] == 3
    assert result["not_matched"] == 0
    for t in (a, b, c):
        assert len(_cycle_marker_comments(service, t.id)) == 1


def test_edge_into_cycle_is_not_a_member(tmp_path):
    """A ticket with several outgoing edges — one into a B ↔ C cycle, one to
    an acyclic D — is not itself in the cycle and is not escalated."""
    settings, service = _prepare(tmp_path)
    x = _blocked_ticket(service, "no matching reason", "x")
    b = _blocked_ticket(service, "no matching reason", "b")
    c = _blocked_ticket(service, "no matching reason", "c")
    d = _blocked_ticket(service, "no matching reason", "d")
    service.set_depends_on(b.id, [c.id])
    service.set_depends_on(c.id, [b.id])
    service.set_depends_on(x.id, [b.id, d.id])

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["cycles_escalated"] == 2  # only b and c
    for t in (b, c):
        assert len(_cycle_marker_comments(service, t.id)) == 1
    for t in (x, d):
        assert _cycle_marker_comments(service, t.id) == []
    # x and d are the only tickets left for the ordinary classification path.
    assert result["not_matched"] == 2


def test_repeated_runs_do_not_re_report_cycle(tmp_path):
    """The CYCLE_MARKER comment is the idempotency key: a second pass over the
    still-unbroken cycle escalates nothing."""
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(service, "no matching reason", "a")
    b = _blocked_ticket(service, "no matching reason", "b")
    service.set_depends_on(a.id, [b.id])
    service.set_depends_on(b.id, [a.id])

    first = bar.run_blocked_auto_resume(settings, now=_LATER)
    second = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert first["cycles_escalated"] == 2
    assert second["cycles_escalated"] == 0
    for t in (a, b):
        assert len(_cycle_marker_comments(service, t.id)) == 1


def test_cycle_via_unblocks_edges_is_detected(tmp_path):
    """The inverse edge a dependency park leaves (``unblocks``) also closes a
    cycle: A unblocks B and B unblocks A is a 2-cycle."""
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(service, "no matching reason", "a")
    b = _blocked_ticket(service, "no matching reason", "b")
    service.set_unblocks(a.id, [b.id])
    service.set_unblocks(b.id, [a.id])

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["cycles_escalated"] == 2
    for t in (a, b):
        assert len(_cycle_marker_comments(service, t.id)) == 1


def test_default_patterns_cover_scope_triage_agent_error():
    """Tickets already BLOCKED by the implement stage's scope-triage error
    fall-through (infra errors swallowed before the fix) get the one
    automatic retry from the default pattern list."""
    from robotsix_mill.config._settings_periodic import _PeriodicSettings

    patterns = _PeriodicSettings.model_fields["blocked_auto_resume_patterns"].default
    note = (
        "scope-triage agent error (ClaudeSDKUsageExhaustedError: You've hit "
        "your session limit · resets 12pm (UTC)) — escalated for human review; "
        "resume-blocked re-runs the triage — out-of-scope: `a.py`"
    )
    assert bar._matches(note, patterns)

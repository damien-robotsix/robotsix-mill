import pytest

from robotsix_mill.core.service import TransitionError
from robotsix_mill.core.states import State, can_transition


def test_create_writes_db_and_workspace(service):
    t = service.create("Add a widget", "do the thing")
    assert t.state is State.DRAFT
    ws = service.workspace(t)
    assert ws.read_description() == "do the thing"
    assert t.content_hash == ws.content_hash()
    assert service.get(t.id).title == "Add a widget"
    assert service.history(t.id)[0].note == "created"


def test_default_source_is_user(service):
    t = service.create("Default source test")
    assert t.source == "user"


def test_explicit_source_is_stored(service):
    t = service.create("Explicit source", source="retrospect")
    assert t.source == "retrospect"


def test_list_filters_by_state(service):
    a = service.create("a")
    service.create("b")
    service.transition(a.id, State.READY)
    assert [t.id for t in service.list(state=State.READY)] == [a.id]
    assert len(service.list(state=State.DRAFT)) == 1
    assert len(service.list()) == 2


def test_transition_records_history(service):
    t = service.create("x")
    service.transition(t.id, State.READY, note="refined")
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    hist = service.history(t.id)
    assert hist[-1].state is State.READY
    assert hist[-1].note == "refined"


def test_illegal_transition_rejected(service):
    t = service.create("x")
    with pytest.raises(TransitionError):
        service.transition(t.id, State.DONE)  # draft -> done not allowed


def test_state_machine_edges():
    # draft → ready → deliverable → in_review(PR) → done(merged) → reviewed
    assert can_transition(State.DRAFT, State.READY)
    assert can_transition(State.READY, State.DELIVERABLE)
    assert can_transition(State.DELIVERABLE, State.IN_REVIEW)
    assert can_transition(State.IN_REVIEW, State.DONE)      # merged
    assert can_transition(State.IN_REVIEW, State.BLOCKED)   # closed unmerged
    assert can_transition(State.IN_REVIEW, State.REBASING)  # conflicting
    assert can_transition(State.REBASING, State.IN_REVIEW)  # rebase success
    assert can_transition(State.REBASING, State.BLOCKED)    # rebase exhausted
    assert can_transition(State.REBASING, State.ERRORED)    # rebase crash
    assert can_transition(State.DONE, State.CLOSED)       # retrospected
    assert not can_transition(State.CLOSED, State.DONE)   # terminal
    assert not can_transition(State.DELIVERABLE, State.DONE)  # via in_review
    assert not can_transition(State.READY, State.DONE)


# --- BLOCKED resume path ---


def test_blocked_from_done_can_transition_with_blocked_from():
    """can_transition(BLOCKED, DONE) returns True when blocked_from=DONE."""
    assert can_transition(State.BLOCKED, State.DONE, blocked_from=State.DONE)


def test_blocked_from_done_fails_without_blocked_from():
    """can_transition(BLOCKED, DONE) returns False without blocked_from."""
    assert not can_transition(State.BLOCKED, State.DONE)


def test_blocked_to_ready_always_allowed():
    """Existing BLOCKED → READY works regardless of blocked_from."""
    assert can_transition(State.BLOCKED, State.READY)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.DONE)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.READY)
    assert can_transition(State.BLOCKED, State.READY, blocked_from=None)


def test_blocked_to_draft_always_allowed():
    """Existing BLOCKED → DRAFT works regardless of blocked_from."""
    assert can_transition(State.BLOCKED, State.DRAFT)
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=State.DONE)
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=None)


def test_blocked_from_implement_can_resume_to_ready():
    """BLOCKED from READY can resume back to READY."""
    assert can_transition(State.BLOCKED, State.READY, blocked_from=State.READY)


def test_blocked_from_refine_can_resume_to_draft():
    """BLOCKED from DRAFT can resume back to DRAFT (also covered by override)."""
    assert can_transition(State.BLOCKED, State.DRAFT, blocked_from=State.DRAFT)


def test_blocked_resume_wrong_state_rejected():
    """BLOCKED from DONE cannot resume to a non-matching state via resume-only path."""
    assert not can_transition(
        State.BLOCKED, State.DELIVERABLE, blocked_from=State.DONE
    )


# --- REBASING-specific can_transition tests ---

def test_can_transition_covers_rebasing():
    """Verify all new edges involving REBASING."""
    # IN_REVIEW → REBASING
    assert can_transition(State.IN_REVIEW, State.REBASING)
    # REBASING → IN_REVIEW
    assert can_transition(State.REBASING, State.IN_REVIEW)
    # REBASING → ERRORED
    assert can_transition(State.REBASING, State.ERRORED)
    # REBASING → BLOCKED
    assert can_transition(State.REBASING, State.BLOCKED)
    # BLOCKED → REBASING with blocked_from=REBASING (resume)
    assert can_transition(
        State.BLOCKED, State.REBASING, blocked_from=State.REBASING
    )
    # BLOCKED → REBASING without blocked_from → False
    assert not can_transition(State.BLOCKED, State.REBASING)
    # REBASING → DONE is NOT allowed (must go through IN_REVIEW)
    assert not can_transition(State.REBASING, State.DONE)


# --- Service-level integration tests ---


def test_transition_to_blocked_records_blocked_from(service):
    """Transitioning to BLOCKED sets blocked_from to the current state."""
    t = service.create("block test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.blocked_from == State.READY.value


def test_resume_blocked_back_to_originating_state(service):
    """resume_blocked transitions BLOCKED → <blocked_from>."""
    t = service.create("resume test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert resumed.blocked_from is None
    hist = service.history(t.id)
    assert hist[-1].state is State.READY
    assert "resumed from blocked" in (hist[-1].note or "")


def test_resume_blocked_after_retrospect_failure(service):
    """Full scenario: DONE → BLOCKED → resume → DONE → CLOSED.
    This simulates a retrospect failure and proves the ticket can be
    recovered without re-running implement or refine."""
    t = service.create("retrospect fail test")
    # Walk through the pipeline to DONE
    service.transition(t.id, State.READY, note="refined")
    service.transition(t.id, State.DELIVERABLE, note="implemented")
    service.transition(t.id, State.IN_REVIEW, note="PR opened")
    service.transition(t.id, State.DONE, note="merged")
    # Now retrospect fails → BLOCKED
    service.transition(t.id, State.BLOCKED, note="retrospect failed")
    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    assert reloaded.blocked_from == State.DONE.value

    # Resume back to DONE
    resumed = service.resume_blocked(t.id)
    assert resumed.state is State.DONE
    assert resumed.blocked_from is None

    # Re-run retrospect → CLOSED
    service.transition(t.id, State.CLOSED, note="retrospect succeeded")
    closed = service.get(t.id)
    assert closed.state is State.CLOSED


def test_blocked_to_ready_still_works_after_blocked_from_recorded(service):
    """The existing BLOCKED → READY override still works."""
    t = service.create("override test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    assert service.get(t.id).blocked_from == State.READY.value
    # Override to READY
    service.transition(t.id, State.READY, note="manual unblock")
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.blocked_from is None


def test_blocked_to_draft_still_works_after_blocked_from_recorded(service):
    """The existing BLOCKED → DRAFT override still works."""
    t = service.create("override draft test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    # Override to DRAFT
    service.transition(t.id, State.DRAFT, note="manual unblock to draft")
    reloaded = service.get(t.id)
    assert reloaded.state is State.DRAFT
    assert reloaded.blocked_from is None


def test_resume_blocked_rejects_non_blocked_ticket(service):
    """resume_blocked raises TransitionError if ticket is not BLOCKED."""
    t = service.create("not blocked")
    with pytest.raises(TransitionError, match="not BLOCKED"):
        service.resume_blocked(t.id)


def test_resume_blocked_rejects_missing_blocked_from(service):
    """resume_blocked raises TransitionError if blocked_from is not set."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Ticket

    t = service.create("no blocked_from")
    # Manually set the ticket to BLOCKED with blocked_from=None via
    # direct DB manipulation to simulate a legacy record.
    with db.session(service.settings) as s:
        ticket = s.get(Ticket, t.id)
        ticket.state = State.BLOCKED
        ticket.blocked_from = None
        s.add(ticket)
        s.commit()

    with pytest.raises(TransitionError, match="no blocked_from"):
        service.resume_blocked(t.id)


def test_transition_table_consistency():
    """Every source state's declared destinations should be reachable
    and no dangling states exist."""
    from robotsix_mill.core.states import TRANSITIONS

    all_states = set(State)
    declared_sources = set(TRANSITIONS.keys())
    assert declared_sources == all_states, "TRANSITIONS must cover every State"

    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            assert dst in all_states, f"{src} -> {dst}: {dst} not a State"
            # Verify can_transition returns True for these edges
            assert can_transition(src, dst), (
                f"can_transition({src}, {dst}) should be True per TRANSITIONS"
            )

    # Terminal states: CLOSED must have no outgoing edges
    assert TRANSITIONS[State.CLOSED] == set()

    # Every active state must be able to reach BLOCKED and ERRORED
    for src in [
        State.DRAFT, State.AWAITING_APPROVAL, State.READY,
        State.DELIVERABLE, State.IN_REVIEW, State.REBASING, State.DONE,
    ]:
        assert State.BLOCKED in TRANSITIONS[src], (
            f"{src} missing BLOCKED escalation edge"
        )
        assert State.ERRORED in TRANSITIONS[src], (
            f"{src} missing ERRORED escalation edge"
        )


# --- cost_usd (Langfuse-synced, absolute) -----------------------------

def test_initial_cost_is_zero(service):
    t = service.create("cost test")
    assert t.cost_usd == 0.0


def test_set_cost_replaces(service):
    """set_cost writes *cost* as the absolute cost_usd — it replaces,
    not accumulates.  Langfuse session totals are authoritative."""
    t = service.create("set cost test")
    service.set_cost(t.id, 0.0042)
    service.set_cost(t.id, 0.0018)
    reloaded = service.get(t.id)
    # Absolute replace, not accumulate — last write wins.
    assert reloaded.cost_usd == pytest.approx(0.0018)


def test_set_cost_persists_through_transition(service):
    """Cost written before a state transition persists through it."""
    t = service.create("cost + transition")
    service.set_cost(t.id, 0.0050)
    service.transition(t.id, State.READY)
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.cost_usd == pytest.approx(0.0050)

    # Later sync updates to a new absolute value.
    service.set_cost(t.id, 0.0080)
    reloaded = service.get(t.id)
    assert reloaded.cost_usd == pytest.approx(0.0080)


def test_set_cost_missing_ticket_is_noop(service):
    """Calling set_cost on a nonexistent ticket should not raise."""
    service.set_cost("nonexistent-id", 1.0)  # no raise

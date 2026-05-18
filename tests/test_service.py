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


def test_default_cost_usd_is_zero(service):
    """New tickets start with cost_usd = 0.0."""
    t = service.create("Cost check")
    assert t.cost_usd == 0.0
    reloaded = service.get(t.id)
    assert reloaded.cost_usd == 0.0


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
    assert can_transition(State.DONE, State.CLOSED)       # retrospected
    assert not can_transition(State.CLOSED, State.DONE)   # terminal
    assert not can_transition(State.DELIVERABLE, State.DONE)  # via in_review
    assert not can_transition(State.READY, State.DONE)


# --- add_cost ---


def test_add_cost_single_increment(service):
    """add_cost increments cost_usd by the given amount."""
    t = service.create("Cost test")
    assert t.cost_usd == 0.0

    service.add_cost(t.id, 0.0123)
    reloaded = service.get(t.id)
    assert reloaded.cost_usd == 0.0123


def test_add_cost_accumulates(service):
    """Multiple add_cost calls accumulate the total."""
    t = service.create("Accumulate test")
    service.add_cost(t.id, 0.001)
    service.add_cost(t.id, 0.002)
    service.add_cost(t.id, 0.0005)
    reloaded = service.get(t.id)
    assert reloaded.cost_usd == pytest.approx(0.0035)


def test_add_cost_survives_state_transition(service):
    """Cost is preserved across state transitions."""
    t = service.create("Transition cost test")
    service.add_cost(t.id, 0.05)
    service.transition(t.id, State.READY, note="refined")
    reloaded = service.get(t.id)
    assert reloaded.state is State.READY
    assert reloaded.cost_usd == 0.05


def test_add_cost_resume_keeps_adding(service):
    """After a state transition, more cost can be added and accumulates."""
    t = service.create("Resume cost test")
    service.add_cost(t.id, 0.01)
    service.transition(t.id, State.READY)
    service.add_cost(t.id, 0.02)
    reloaded = service.get(t.id)
    assert reloaded.cost_usd == pytest.approx(0.03)


def test_add_cost_noop_on_missing_ticket(service):
    """Calling add_cost with a nonexistent ticket id does not raise."""
    service.add_cost("nonexistent-id", 999.0)  # must not raise

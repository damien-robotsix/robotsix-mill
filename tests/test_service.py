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
    assert can_transition(State.DONE, State.REVIEWED)       # retrospected
    assert not can_transition(State.REVIEWED, State.DONE)   # terminal
    assert not can_transition(State.DELIVERABLE, State.DONE)  # via in_review
    assert not can_transition(State.READY, State.DONE)

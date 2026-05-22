"""Tests for epic status re-evaluation: worker hook and agent."""

import pytest

from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import (
    _process_ticket_inner,
    _spawn_epic_reeval,
    _run_epic_reeval,
)
from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


# -----------------------------------------------------------------------
# Worker hook tests
# -----------------------------------------------------------------------


async def test_hook_fires_on_done_for_epic_parent(ctx, service, monkeypatch):
    """Worker hook: when a child reaches DONE and has an epic parent,
    _spawn_epic_reeval is called with the correct epic ID."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker._spawn_epic_reeval", fake_spawn
    )

    class DoneStage(Stage):
        name = "merge"
        input_state = State.HUMAN_MR_APPROVAL

        def run(self, _t, _c):
            return Outcome(State.DONE, "merged")

    monkeypatch.setitem(registry.STAGES, "merge", DoneStage())

    epic = service.create("My Epic", "Big goal", kind="epic")
    child = service.create("Child", "do the thing", parent_id=epic.id)
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL):
        service.transition(child.id, st)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == [epic.id]


async def test_hook_does_not_fire_for_non_epic_parent(ctx, service, monkeypatch):
    """Worker hook: when the parent is not an epic (kind="task"),
    _spawn_epic_reeval is NOT called."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker._spawn_epic_reeval", fake_spawn
    )

    class DoneStage(Stage):
        name = "merge"
        input_state = State.HUMAN_MR_APPROVAL

        def run(self, _t, _c):
            return Outcome(State.DONE, "merged")

    monkeypatch.setitem(registry.STAGES, "merge", DoneStage())

    parent = service.create("Parent task", "some task")
    child = service.create("Child", "do the thing", parent_id=parent.id)
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL):
        service.transition(child.id, st)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == []


async def test_hook_does_not_fire_for_non_done_transition(ctx, service, monkeypatch):
    """Worker hook: when the outcome is not DONE (e.g. CODE_REVIEW),
    _spawn_epic_reeval is NOT called."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker._spawn_epic_reeval", fake_spawn
    )

    class ReviewStage(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            return Outcome(State.CODE_REVIEW, "review time")

    monkeypatch.setitem(registry.STAGES, "implement", ReviewStage())

    epic = service.create("My Epic", "Big goal", kind="epic")
    child = service.create("Child", "do the thing", parent_id=epic.id)
    service.transition(child.id, State.READY)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == []


# -----------------------------------------------------------------------
# Agent unit test
# -----------------------------------------------------------------------


def test_epic_status_agent_result_shape(monkeypatch):
    """Monkeypatch run_epic_status_agent and verify the result shape."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    result = EpicStatusResult(
        decision="close", note="All children complete."
    )

    def fake_agent(*, settings, epic_title, epic_description, children):
        return result

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        fake_agent,
    )

    from robotsix_mill.agents.epic_status import run_epic_status_agent

    r = run_epic_status_agent(
        settings=None,
        epic_title="Test Epic",
        epic_description="Do stuff",
        children=[],
    )
    assert r.decision == "close"
    assert r.note == "All children complete."


# -----------------------------------------------------------------------
# End-to-end tests (via _run_epic_reeval)
# -----------------------------------------------------------------------


def test_e2e_all_children_done_closes_epic(settings, service, monkeypatch):
    """Create an epic with 2 children, both in DONE state.
    Agent returns "close" — assert epic transitions to EPIC_CLOSED."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="close", note="All children complete."
        ),
    )

    epic = service.create("My Epic", "Build the thing", kind="epic")
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    c2 = service.create("Child 2", "part 2", parent_id=epic.id)

    service.transition(c1.id, State.DONE)
    service.transition(c2.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    assert service.get(epic.id).state == State.EPIC_CLOSED
    assert "All children complete" in service.history(epic.id)[-1].note


def test_e2e_one_child_in_progress_keeps_open(settings, service, monkeypatch):
    """Create an epic with 2 children: one DONE, one READY.
    Agent returns "keep_open" — assert epic remains EPIC_OPEN."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open", note="Child 2 still in progress."
        ),
    )

    epic = service.create("My Epic", "Build the thing", kind="epic")
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    c2 = service.create("Child 2", "part 2", parent_id=epic.id)

    service.transition(c1.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    assert service.get(epic.id).state == State.EPIC_OPEN


def test_e2e_update_description(settings, service, monkeypatch):
    """Agent returns "update_description" with new text.
    Assert epic description is updated and epic stays EPIC_OPEN."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    new_desc = "Updated epic description: only 2 of 5 children remain."

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="update_description", note=new_desc,
        ),
    )

    epic = service.create("My Epic", "Build the thing", kind="epic")
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)

    service.transition(c1.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    assert service.get(epic.id).state == State.EPIC_OPEN
    assert service.workspace(epic).read_description() == new_desc

"""Tests for epic status re-evaluation: worker hook and agent."""

import pytest

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import (
    _process_ticket_inner,
    _run_epic_reeval,
)
from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


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
        "robotsix_mill.runtime.worker.processing._spawn_epic_reeval", fake_spawn
    )

    class DoneStage(Stage):
        name = "merge"
        input_state = State.HUMAN_MR_APPROVAL

        def run(self, _t, _c):
            return Outcome(State.DONE, "merged")

    monkeypatch.setitem(registry.STAGES, "merge", DoneStage())

    # Prevent the real retrospect stage from running after merge — it
    # would transition DONE→CLOSED and fire the epic hook a second time.
    class NoopRetrospectStage(Stage):
        name = "retrospect"
        input_state = State.DONE

        def run(self, _t, _c):
            return Outcome(State.DONE, "no-op")

    monkeypatch.setitem(registry.STAGES, "retrospect", NoopRetrospectStage())

    epic = service.create("My Epic", "Big goal", kind=TicketKind.EPIC)
    child = service.create("Child", "do the thing", parent_id=epic.id)
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(child.id, st)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == [epic.id]


async def test_hook_does_not_fire_for_non_epic_parent(ctx, service, monkeypatch):
    """Worker hook: when the parent is not an epic (kind=TicketKind.TASK),
    _spawn_epic_reeval is NOT called."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing._spawn_epic_reeval", fake_spawn
    )

    class DoneStage(Stage):
        name = "merge"
        input_state = State.HUMAN_MR_APPROVAL

        def run(self, _t, _c):
            return Outcome(State.DONE, "merged")

    monkeypatch.setitem(registry.STAGES, "merge", DoneStage())

    parent = service.create("Parent task", "some task")
    child = service.create("Child", "do the thing", parent_id=parent.id)
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        service.transition(child.id, st)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == []


async def test_hook_does_not_fire_for_non_done_transition(ctx, service, monkeypatch):
    """Worker hook: when the outcome is not DONE (e.g. DELIVERABLE),
    _spawn_epic_reeval is NOT called."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing._spawn_epic_reeval", fake_spawn
    )

    class ReviewStage(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _t, _c):
            # A legal, non-DONE outcome from READY (READY -> CODE_REVIEW
            # is not a direct edge; the path is READY -> DOCUMENTING ->
            # CODE_REVIEW). DELIVERABLE exercises the same "not DONE" case.
            return Outcome(State.DELIVERABLE, "deliverable time")

    monkeypatch.setitem(registry.STAGES, "implement", ReviewStage())

    epic = service.create("My Epic", "Big goal", kind=TicketKind.EPIC)
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

    result = EpicStatusResult(decision="close", note="All children complete.")

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


def test_children_table_renders_delivery_column():
    from robotsix_mill.agents.epic_status import _build_children_table

    children = [
        {"id": "C1", "title": "One", "state": "draft", "delivery": "merged"},
        {"id": "C2", "title": "Two", "state": "draft", "delivery": "unstarted"},
    ]
    table = _build_children_table(children)

    header = table.splitlines()[0]
    assert "Delivery" in header
    # Header column count matches the separator row column count.
    assert header.count("|") == table.splitlines()[1].count("|")
    assert "merged" in table
    assert "unstarted" in table


def test_child_closures_accepts_dict_and_list():
    from robotsix_mill.agents.epic_status import EpicStatusResult

    as_map = EpicStatusResult(decision="keep_open", child_closures={"C1": "S1"})
    assert as_map.child_closures == {"C1": "S1"}
    as_list = EpicStatusResult(decision="keep_open", child_closures=["C1"])
    assert as_list.child_closures == ["C1"]


# -----------------------------------------------------------------------
# End-to-end tests (via _run_epic_reeval)
# -----------------------------------------------------------------------


def test_e2e_all_children_done_closes_epic(settings, service, monkeypatch):
    """Create an epic with 2 children, both in DONE state.
    Agent returns "close" — assert epic transitions to EPIC_CLOSED."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(decision="close", note="All children complete."),
    )

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
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

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    service.create("Child 2", "part 2", parent_id=epic.id)

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
            decision="update_description",
            note=new_desc,
        ),
    )

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)

    service.transition(c1.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    assert service.get(epic.id).state == State.EPIC_OPEN
    assert service.workspace(epic).read_description() == new_desc


# -----------------------------------------------------------------------
# New child-ticket change tests
# -----------------------------------------------------------------------


def test_e2e_rewrites_generic_description(settings, service, monkeypatch):
    """Epic has a vague one-liner description. Agent returns
    ``update_description`` with a strategic rewrite. Assert the epic
    description is replaced and epic stays EPIC_OPEN."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    strategic = (
        "## Strategy\n\n"
        "1. Implement the core engine (done — child #1)\n"
        "2. Add the web dashboard (remaining — child #2)\n"
        "3. Write integration tests (remaining — child #3)\n"
    )

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="update_description",
            note=strategic,
        ),
    )

    epic = service.create("My Epic", "Make it better", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    service.transition(c1.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    assert service.get(epic.id).state == State.EPIC_OPEN
    assert service.workspace(epic).read_description() == strategic


def test_e2e_adds_new_child(settings, service, monkeypatch):
    """Agent returns ``new_children`` with one entry. Assert a new child
    ticket is created under the epic with kind=TicketKind.TASK."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Need more work.",
            new_children=[{"title": "New work", "body": "Do the new thing."}],
        ),
    )

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    service.transition(c1.id, State.DONE)

    _run_epic_reeval(epic.id, settings)

    children = service.list_children(epic.id)
    titles = [c.title for c in children]
    assert "New work" in titles
    new_child = next(c for c in children if c.title == "New work")
    assert new_child.kind == TicketKind.TASK
    assert new_child.parent_id == epic.id


def test_e2e_rescopes_draft_child(settings, service, monkeypatch):
    """Epic has a child in DRAFT. Agent returns rescope with a new title.
    Assert child's title is updated."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Old Title", "old body", parent_id=epic.id)
    # child starts in DRAFT

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Rescoping needed.",
            child_rescopes={child.id: {"title": "Better title"}},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    updated = service.get(child.id)
    assert updated.title == "Better title"


def test_e2e_skips_rescope_of_in_flight_child(settings, service, monkeypatch):
    """Epic has a child in READY (in-flight). Agent returns rescope.
    Assert child's title is NOT changed (reconciliation skips it)."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Old Title", "old body", parent_id=epic.id)
    service.transition(child.id, State.READY)  # move to in-flight

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Rescoping attempted.",
            child_rescopes={child.id: {"title": "Better title"}},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    updated = service.get(child.id)
    assert updated.title == "Old Title"  # unchanged


def test_e2e_closes_draft_child(settings, service, monkeypatch):
    """Epic has a child in DRAFT and a merged sibling that covers it.
    Agent returns child_closures mapping child -> merged sibling.
    Assert child transitions to CLOSED with a note naming the sibling."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Obsolete child", "no longer needed", parent_id=epic.id)
    # child starts in DRAFT
    sibling = service.create("Did the work", "delivers scope", parent_id=epic.id)
    service.transition(sibling.id, State.DONE, note="merged: http://example/pr/1")

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Closing obsolete child.",
            child_closures={child.id: sibling.id},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    updated = service.get(child.id)
    assert updated.state == State.CLOSED
    # The closure note names the covering merged sibling.
    events = service.history(child.id)
    close_notes = [e.note for e in events if e.state == State.CLOSED]
    assert any(sibling.id in (n or "") for n in close_notes)
    assert not any(
        "Obsoleted by epic re-evaluation after sibling merge" in (n or "")
        for n in close_notes
    )


def test_e2e_skips_closure_of_done_child(settings, service, monkeypatch):
    """Epic has a child in DONE. Agent returns child_closures.
    Assert child stays in DONE (reconciliation skips terminal children)."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Already done", "finished work", parent_id=epic.id)
    service.transition(child.id, State.DONE)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Attempting to close done child.",
            child_closures=[child.id],
        ),
    )

    _run_epic_reeval(epic.id, settings)

    updated = service.get(child.id)
    assert updated.state == State.DONE  # unchanged


# -----------------------------------------------------------------------
# Mixed operations & edge cases
# -----------------------------------------------------------------------


def test_e2e_mixed_operations(settings, service, monkeypatch):
    """Agent proposes new children + rescopes + closures in one call.
    Assert all safe operations are applied."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    draft_child = service.create("Rescope me", "old body", parent_id=epic.id)
    close_child = service.create("Close me", "obsolete", parent_id=epic.id)
    ready_child = service.create("In-flight", "don't touch", parent_id=epic.id)
    service.transition(ready_child.id, State.READY)
    merged_sibling = service.create("Covers it", "delivers scope", parent_id=epic.id)
    service.transition(
        merged_sibling.id, State.DONE, note="merged: http://example/pr/2"
    )

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Mixed operations.",
            new_children=[
                {"title": "Fresh work", "body": "Do this new thing."},
            ],
            child_rescopes={
                draft_child.id: {"title": "Rescoped title"},
                ready_child.id: {"title": "Should not apply"},
            },
            child_closures={close_child.id: merged_sibling.id},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    # New child created
    children = service.list_children(epic.id)
    titles = [c.title for c in children]
    assert "Fresh work" in titles

    # DRAFT child rescoped
    assert service.get(draft_child.id).title == "Rescoped title"

    # READY child NOT rescoped
    assert service.get(ready_child.id).title == "In-flight"

    # DRAFT child closed
    assert service.get(close_child.id).state == State.CLOSED


def test_e2e_new_child_missing_title(settings, service, monkeypatch, caplog):
    """Agent returns new_children entry with empty title — skipped with warning."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Bad children.",
            new_children=[{"title": "", "body": "some body"}],
        ),
    )

    _run_epic_reeval(epic.id, settings)

    children = service.list_children(epic.id)
    assert len(children) == 0
    assert "missing non-empty 'title'" in caplog.text


def test_e2e_new_child_missing_body(settings, service, monkeypatch, caplog):
    """Agent returns new_children entry with empty body — skipped with warning."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Bad children.",
            new_children=[{"title": "A title", "body": ""}],
        ),
    )

    _run_epic_reeval(epic.id, settings)

    children = service.list_children(epic.id)
    assert len(children) == 0
    assert "missing non-empty 'body'" in caplog.text


def test_e2e_rescope_missing_both_fields(settings, service, monkeypatch, caplog):
    """Agent returns child_rescopes entry with neither title nor body — skipped."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("A child", "body", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Bad rescope.",
            child_rescopes={child.id: {"title": "", "body": ""}},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    assert service.get(child.id).title == "A child"  # unchanged
    assert "has no non-empty 'title' or 'body'" in caplog.text


def test_e2e_new_child_not_a_dict(settings, service, monkeypatch, caplog):
    """Agent returns new_children with a non-dict entry — Pydantic
    catches this before the worker runs, so the re-evaluation fails
    with a validation error rather than crashing. The worker's
    except-block logs the failure."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult.model_validate(
            {
                "decision": "keep_open",
                "note": "Malformed.",
                "new_children": ["not a dict"],
            }
        ),
    )

    _run_epic_reeval(epic.id, settings)

    # Pydantic validation error is caught by the worker's except-block
    assert "re-evaluation failed" in caplog.text


def test_e2e_child_rescopes_not_a_dict(settings, service, monkeypatch, caplog):
    """Agent returns child_rescopes with a non-dict value — Pydantic
    catches this before the worker runs, so the re-evaluation fails
    with a validation error rather than crashing."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("A child", "body", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult.model_validate(
            {
                "decision": "keep_open",
                "note": "Malformed.",
                "child_rescopes": {child.id: "not a dict"},
            }
        ),
    )

    _run_epic_reeval(epic.id, settings)

    # Pydantic validation error is caught by the worker's except-block
    assert "re-evaluation failed" in caplog.text


def test_e2e_reconciliation_failure_does_not_crash(settings, service, monkeypatch):
    """When a child reconciliation operation raises, the exception is
    caught and logged — the re-evaluation does not crash."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Will fail", "body", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Will trigger failure.",
            child_rescopes={child.id: {"title": "New title"}},
        ),
    )

    # Force svc.set_title to raise an exception to simulate a service failure
    monkeypatch.setattr(
        "robotsix_mill.core.service.TicketService.set_title",
        lambda self, tid, title: (_ for _ in ()).throw(
            RuntimeError("simulated failure")
        ),
    )

    # Must not raise — the worker catches and logs
    _run_epic_reeval(epic.id, settings)


def test_e2e_closure_bad_id_type(settings, service, monkeypatch, caplog):
    """Agent returns child_closures with a non-string entry — Pydantic
    catches this before the worker runs, so the re-evaluation fails
    with a validation error rather than crashing."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult.model_validate(
            {
                "decision": "keep_open",
                "note": "Bad closure.",
                "child_closures": [12345],
            }
        ),
    )

    _run_epic_reeval(epic.id, settings)

    # Pydantic validation error is caught by the worker's except-block
    assert "re-evaluation failed" in caplog.text


def test_e2e_closure_nonexistent_child(settings, service, monkeypatch, caplog):
    """Agent returns child_closures with a non-existent child ID —
    logged as warning, does not crash."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Closing nonexistent.",
            child_closures=["nonexistent-id"],
        ),
    )

    _run_epic_reeval(epic.id, settings)

    assert "not found" in caplog.text


def test_e2e_all_fields_none_backward_compatible(settings, service, monkeypatch):
    """When new fields are None (backward-compatible), the worker
    handles them gracefully."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="All good.",
            new_children=None,
            child_rescopes=None,
            child_closures=None,
        ),
    )

    # Must not raise
    _run_epic_reeval(epic.id, settings)
    assert service.get(epic.id).state == State.EPIC_OPEN


def test_e2e_rescope_updates_body(settings, service, monkeypatch):
    """Agent returns child_rescopes with a new body for a DRAFT child.
    Assert the body is updated."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    child = service.create("Keep title", "old body", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(
            decision="keep_open",
            note="Updating body.",
            child_rescopes={child.id: {"body": "new strategic body"}},
        ),
    )

    _run_epic_reeval(epic.id, settings)

    assert service.get(child.id).title == "Keep title"  # unchanged
    assert service.workspace(child).read_description() == "new strategic body"


# -----------------------------------------------------------------------
# Epic status auto-close tests (AC 1-2, 5-6)
# -----------------------------------------------------------------------


async def test_hook_fires_on_closed_for_epic_parent(ctx, service, monkeypatch):
    """Worker hook: when a child reaches CLOSED and has an epic parent,
    _spawn_epic_reeval is called with the correct epic ID."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing._spawn_epic_reeval", fake_spawn
    )

    class CloseStage(Stage):
        name = "retrospect"
        input_state = State.DONE

        def run(self, _t, _c):
            return Outcome(State.CLOSED, "closed via retrospect")

    monkeypatch.setitem(registry.STAGES, "retrospect", CloseStage())

    epic = service.create("My Epic", "Big goal", kind=TicketKind.EPIC)
    child = service.create("Child", "do the thing", parent_id=epic.id)
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.DONE,
    ):
        service.transition(child.id, st)

    await _process_ticket_inner(child.id, ctx)

    assert called_with == [epic.id]


async def test_hook_fires_on_answered_for_epic_parent(ctx, service, monkeypatch):
    """Worker hook: when a child reaches ANSWERED and has an epic parent,
    _spawn_epic_reeval is called with the correct epic ID."""
    called_with: list = []

    def fake_spawn(epic_id, _ctx):
        called_with.append(epic_id)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.processing._spawn_epic_reeval", fake_spawn
    )

    class AnswerStage(Stage):
        name = "answer"
        input_state = State.ASKED

        def run(self, _t, _c):
            return Outcome(State.ANSWERED, "inquiry answered")

    monkeypatch.setitem(registry.STAGES, "answer", AnswerStage())

    epic = service.create("My Epic", "Big goal", kind=TicketKind.EPIC)
    child = service.create(
        "Child", "do the thing", parent_id=epic.id, kind=TicketKind.INQUIRY
    )

    await _process_ticket_inner(child.id, ctx)

    assert called_with == [epic.id]


def test_idempotent_already_closed_epic_noop(settings, service, monkeypatch):
    """Calling _run_epic_reeval on an already EPIC_CLOSED epic is a no-op:
    no exception, no state change, and the agent is never invoked."""

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    service.transition(epic.id, State.EPIC_CLOSED)

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("agent should not be called for an already-closed epic")

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        should_not_be_called,
    )

    # Must not raise
    _run_epic_reeval(epic.id, settings)

    # State must remain EPIC_CLOSED
    assert service.get(epic.id).state == State.EPIC_CLOSED


def test_closure_triggers_from_child_closed(settings, service, monkeypatch):
    """End-to-end: epic with 2 children where one is DONE and the other
    transitions to CLOSED via the worker hook. The agent is monkeypatched
    to return decision="close". Assert epic transitions to EPIC_CLOSED
    and the note starts with '[auto-closed]'."""
    from robotsix_mill.agents.epic_status import EpicStatusResult

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_status.run_epic_status_agent",
        lambda **kw: EpicStatusResult(decision="close", note="All children terminal."),
    )

    epic = service.create("My Epic", "Build the thing", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", "part 1", parent_id=epic.id)
    c2 = service.create("Child 2", "part 2", parent_id=epic.id)

    service.transition(c1.id, State.DONE)
    service.transition(c2.id, State.CLOSED)

    _run_epic_reeval(epic.id, settings)

    epic_after = service.get(epic.id)
    assert epic_after.state == State.EPIC_CLOSED
    last_note = service.history(epic.id)[-1].note
    assert last_note.startswith("[auto-closed]")
    assert "All children terminal" in last_note


# -----------------------------------------------------------------------
# Orphaned-epic safety-net sweep (_maybe_sweep_orphaned_epic)
# -----------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from robotsix_mill.runtime.worker import Worker  # noqa: E402


def _sweep_self(ctx):
    return SimpleNamespace(_epic_sweep_seen={}, ctx=ctx)


def _close_children(service, epic_id):
    for c in service.list_children(epic_id):
        for st in (
            State.READY,
            State.IMPLEMENT_COMPLETE,
            State.HUMAN_MR_APPROVAL,
            State.DONE,
            State.CLOSED,
        ):
            try:
                service.transition(c.id, st)
            except Exception:
                pass


def test_sweep_reevaluates_orphaned_all_terminal_epic(ctx, service, monkeypatch):
    """An EPIC_OPEN epic whose children are ALL terminal gets a sweep re-eval."""
    spawned: list = []
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core._spawn_epic_reeval",
        lambda epic_id, _c: spawned.append(epic_id),
    )
    epic = service.create("Epic", "goal", kind=TicketKind.EPIC)
    service.create("c1", "x", parent_id=epic.id)
    service.create("c2", "y", parent_id=epic.id)
    _close_children(service, epic.id)

    fake = _sweep_self(ctx)
    Worker._maybe_sweep_orphaned_epic(fake, service.get(epic.id), service)
    assert spawned == [epic.id]

    # Idempotent: a second sweep over the same terminal child set does NOT
    # re-spawn (no re-billing a healthy epic every poll).
    Worker._maybe_sweep_orphaned_epic(fake, service.get(epic.id), service)
    assert spawned == [epic.id]


def test_sweep_skips_epic_with_open_child(ctx, service, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core._spawn_epic_reeval",
        lambda epic_id, _c: spawned.append(epic_id),
    )
    epic = service.create("Epic", "goal", kind=TicketKind.EPIC)
    c1 = service.create("c1", "x", parent_id=epic.id)
    service.create("c2", "y", parent_id=epic.id)  # left in DRAFT (not terminal)
    for st in (
        State.READY,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
        State.DONE,
        State.CLOSED,
    ):
        try:
            service.transition(c1.id, st)
        except Exception:
            pass

    Worker._maybe_sweep_orphaned_epic(_sweep_self(ctx), service.get(epic.id), service)
    assert spawned == []  # one child still open → not swept


def test_sweep_skips_childless_epic(ctx, service, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core._spawn_epic_reeval",
        lambda epic_id, _c: spawned.append(epic_id),
    )
    epic = service.create("Epic", "goal", kind=TicketKind.EPIC)
    Worker._maybe_sweep_orphaned_epic(_sweep_self(ctx), service.get(epic.id), service)
    assert spawned == []

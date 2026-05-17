import pytest

from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage
from robotsix_mill.core.states import State
from robotsix_mill.runtime.worker import process_ticket


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


async def test_stub_pauses_chain(ctx, service):
    """A still-stub stage (review) raises NotImplementedError: the chain
    pauses, leaving the ticket in that state (not FAILED)."""
    t = service.create("x")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.IN_REVIEW)
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.IN_REVIEW


async def test_working_stages_chain_to_terminal(ctx, service, monkeypatch):
    """Two cooperating fake stages drive draft -> ready -> in_review and
    then stop at the (still-stub) review stage."""

    class FakeRefine(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            return Outcome(State.READY, "refined")

    class FakeImplement(Stage):
        name = "implement"
        input_state = State.READY

        def run(self, _ticket, _ctx):
            return Outcome(State.IN_REVIEW, "implemented")

    monkeypatch.setitem(registry.STAGES, "refine", FakeRefine())
    monkeypatch.setitem(registry.STAGES, "implement", FakeImplement())

    t = service.create("x")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.IN_REVIEW
    states = [e.state for e in service.history(t.id)]
    assert State.READY in states and State.IN_REVIEW in states


async def test_failing_stage_marks_failed(ctx, service, monkeypatch):
    class Boom(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _ticket, _ctx):
            raise RuntimeError("boom")

    monkeypatch.setitem(registry.STAGES, "refine", Boom())
    t = service.create("x")
    await process_ticket(t.id, ctx)
    reloaded = service.get(t.id)
    assert reloaded.state is State.FAILED
    assert "boom" in service.history(t.id)[-1].note

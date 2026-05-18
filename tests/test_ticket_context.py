"""Tests for the ticket_context module and cost attribution wiring.

Covers the ContextVar bridge, _extract_cost, and end-to-end attribution
from CostInstrumentedOpenRouterModel to TicketService.add_cost.
"""

import pytest

from robotsix_mill.agents.ticket_context import (
    active_ticket_id,
    notify_cost,
    set_cost_callback,
)
from robotsix_mill.agents.openrouter_cost import (
    _extract_cost,
    CostInstrumentedOpenRouterModel,
    record_openrouter_cost,
)


# --- ContextVar wiring ---

def test_active_ticket_id_default_is_none():
    assert active_ticket_id.get() is None


def test_active_ticket_id_set_and_reset():
    token = active_ticket_id.set("ticket-123")
    assert active_ticket_id.get() == "ticket-123"
    active_ticket_id.reset(token)
    assert active_ticket_id.get() is None


def test_notify_cost_noop_without_callback():
    """notify_cost does not raise when no callback is registered."""
    notify_cost("any-id", 0.05)  # must not raise


def test_notify_cost_calls_registered_callback():
    calls = []

    def cb(tid: str, cost: float):
        calls.append((tid, cost))

    set_cost_callback(cb)
    notify_cost("t-1", 0.0123)
    notify_cost("t-2", 0.0456)
    assert calls == [("t-1", 0.0123), ("t-2", 0.0456)]

    # Reset for subsequent tests
    set_cost_callback(None)


# --- _extract_cost ---

def test_extract_cost_from_model_extra():
    class Usage:
        model_extra = {"cost": 0.0234}

    class Response:
        usage = Usage()

    assert _extract_cost(Response()) == 0.0234


def test_extract_cost_from_direct_cost_attr():
    class Usage:
        model_extra = {}
        cost = 0.0567

    class Response:
        usage = Usage()

    assert _extract_cost(Response()) == 0.0567


def test_extract_cost_none_when_no_usage():
    class Response:
        usage = None

    assert _extract_cost(Response()) is None


def test_extract_cost_none_when_no_cost():
    class Usage:
        model_extra = {}
        cost = None

    class Response:
        usage = Usage()

    assert _extract_cost(Response()) is None


def test_extract_cost_none_when_unparseable():
    class Usage:
        model_extra = {"cost": "not-a-number"}

    class Response:
        usage = Usage()

    # float("not-a-number") would raise, but our extractor catches it
    # Actually float("not-a-number") raises ValueError.
    # Let's test with something that would fail.
    result = _extract_cost(Response())
    # float("not-a-number") raises ValueError → caught → returns None
    assert result is None


# --- CostInstrumentedOpenRouterModel attribution ---


class FakeCompletions:
    """A fake _completions_create that returns a synthetic response."""

    def __init__(self, cost=0.0123, model="test/model"):
        self._cost = cost
        self._model = model

    async def __call__(self, *args, **kwargs):
        class Usage:
            model_extra = {"cost": self._cost}
            prompt_tokens = 10
            completion_tokens = 20

        class Response:
            usage = Usage()
            model = self._model

        return Response()


@pytest.mark.asyncio
async def test_model_attribution_when_contextvar_set(monkeypatch):
    """When active_ticket_id is set, _completions_create calls notify_cost
    with the right ticket id and cost."""
    calls = []

    def cb(tid, cost):
        calls.append((tid, cost))

    set_cost_callback(cb)
    token = active_ticket_id.set("tid-1")

    try:
        # Patch _completions_create on the superclass to avoid real API calls
        model = CostInstrumentedOpenRouterModel.__new__(
            CostInstrumentedOpenRouterModel
        )
        # We only test the logic in _completions_create by patching super
        fake_super = FakeCompletions(cost=0.042)
        monkeypatch.setattr(
            model.__class__.__bases__[0],
            "_completions_create",
            fake_super,
        )
        # Also ensure record_openrouter_cost is a no-op (no OTel)
        monkeypatch.setattr(
            "robotsix_mill.agents.openrouter_cost.record_openrouter_cost",
            lambda r: None,
        )

        await model._completions_create()

        assert len(calls) == 1
        assert calls[0] == ("tid-1", 0.042)
    finally:
        active_ticket_id.reset(token)
        set_cost_callback(None)


@pytest.mark.asyncio
async def test_model_no_attribution_when_contextvar_none(monkeypatch):
    """When active_ticket_id is None, _completions_create does NOT call
    notify_cost."""
    calls = []

    def cb(tid, cost):
        calls.append((tid, cost))

    set_cost_callback(cb)
    # active_ticket_id defaults to None — not set

    model = CostInstrumentedOpenRouterModel.__new__(
        CostInstrumentedOpenRouterModel
    )
    fake_super = FakeCompletions(cost=0.099)
    monkeypatch.setattr(
        model.__class__.__bases__[0],
        "_completions_create",
        fake_super,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.openrouter_cost.record_openrouter_cost",
        lambda r: None,
    )

    await model._completions_create()

    assert len(calls) == 0


# --- End-to-end: contextvar + TicketService.add_cost ---


def test_e2e_contextvar_to_add_cost(service):
    """Full wiring: set active_ticket_id, register service.add_cost as
    callback, call notify_cost → cost_usd is incremented."""
    t = service.create("E2E cost test")
    assert t.cost_usd == 0.0

    set_cost_callback(service.add_cost)
    token = active_ticket_id.set(t.id)

    try:
        notify_cost(t.id, 0.0075)
        notify_cost(t.id, 0.0025)

        reloaded = service.get(t.id)
        assert reloaded.cost_usd == pytest.approx(0.01)
    finally:
        active_ticket_id.reset(token)
        set_cost_callback(None)

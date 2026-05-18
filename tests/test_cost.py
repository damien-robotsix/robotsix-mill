from contextvars import copy_context

import pytest

from robotsix_mill.agents.openrouter_cost import (
    _accumulate_ticket_cost,
    _inject_usage_include,
    record_openrouter_cost,
)
from robotsix_mill.agents.ticket_context import (
    active_ticket_id,
    active_ticket_service,
)


# --- _inject_usage_include (pure dict logic, no deps) -------------------

def test_inject_via_kwargs():
    ms: dict = {}
    _inject_usage_include((), {"model_settings": ms})
    assert ms["extra_body"]["usage"]["include"] is True


def test_inject_preserves_existing_extra_body():
    ms = {"extra_body": {"plugins": [{"id": "web"}]}}
    _inject_usage_include((), {"model_settings": ms})
    assert ms["extra_body"]["plugins"] == [{"id": "web"}]  # not trampled
    assert ms["extra_body"]["usage"]["include"] is True


def test_inject_positional_model_settings():
    ms: dict = {}
    _inject_usage_include(("msgs", False, ms, "params"), {})
    assert ms["extra_body"]["usage"]["include"] is True


def test_inject_noop_when_no_settings():
    _inject_usage_include((), {})  # must not raise


# --- record_openrouter_cost guards (hermetic) --------------------------

def test_record_noop_without_usage_or_cost():
    class NoUsage:
        usage = None

    record_openrouter_cost(NoUsage())  # no raise

    class U:
        model_extra: dict = {}
        cost = None

    class R:
        usage = U()

    record_openrouter_cost(R())  # no cost → no raise


def test_record_sets_span_attrs(monkeypatch):
    ot = pytest.importorskip("opentelemetry.trace")  # needs [tracing]
    captured: dict = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            captured[k] = v

    monkeypatch.setattr(ot, "get_current_span", lambda: Span())

    class U:
        model_extra = {"cost": 0.0123}
        prompt_tokens = 10
        completion_tokens = 20

    class R:
        usage = U()
        model = "deepseek/deepseek-v4-pro"

    record_openrouter_cost(R())
    assert captured["gen_ai.usage.cost"] == 0.0123
    assert "langfuse.observation.cost_details" in captured
    assert captured["gen_ai.usage.input_tokens"] == 10
    assert captured["gen_ai.usage.output_tokens"] == 20
    assert captured["gen_ai.provider.name"] == "openrouter"


# --- _accumulate_ticket_cost (contextvar → add_cost) -------------------

def make_response(cost: float):
    """Build a response object shaped like an OpenRouter completion
    so _get_cost_from_response extracts *cost*."""

    class Usage:
        model_extra = {"cost": cost}

    class Resp:
        usage = Usage()

    return Resp()


class _FakeTicketService:
    """Records the last (ticket_id, amount) passed to add_cost."""

    def __init__(self):
        self.last_id: str | None = None
        self.last_amount: float | None = None
        self.calls: int = 0

    def add_cost(self, ticket_id: str, amount: float) -> None:
        self.last_id = ticket_id
        self.last_amount = amount
        self.calls += 1


def test_accumulate_noop_when_no_contextvars():
    """When neither contextvar is set, _accumulate_ticket_cost is a
    silent no-op (no crash, no service call)."""
    # Run in a clean context with no contextvars set.
    ctx = copy_context()
    ctx.run(_accumulate_ticket_cost, make_response(0.0050))
    # Must not raise


def test_accumulate_calls_add_cost_with_correct_id_and_amount(monkeypatch):
    """When active_ticket_id and active_ticket_service are both set,
    _accumulate_ticket_cost extracts the cost from the response and
    calls TicketService.add_cost with the right ticket id and amount."""
    fake_svc = _FakeTicketService()

    def _run():
        active_ticket_id.set("ticket-abc")
        active_ticket_service.set(fake_svc)
        _accumulate_ticket_cost(make_response(0.0073))

    ctx = copy_context()
    ctx.run(_run)

    assert fake_svc.last_id == "ticket-abc"
    assert fake_svc.last_amount == pytest.approx(0.0073)
    assert fake_svc.calls == 1


def test_accumulate_noop_when_no_cost_on_response(monkeypatch):
    """When the response carries no usage.cost, add_cost is not called
    even though contextvars are set."""

    class NoCostUsage:
        model_extra = {}  # no "cost" key

    class Resp:
        usage = NoCostUsage()

    fake_svc = _FakeTicketService()

    def _run():
        active_ticket_id.set("ticket-xyz")
        active_ticket_service.set(fake_svc)
        _accumulate_ticket_cost(Resp())

    ctx = copy_context()
    ctx.run(_run)

    assert fake_svc.calls == 0


def test_accumulate_respects_multiple_calls(monkeypatch):
    """Multiple calls accumulate to the same ticket id."""
    fake_svc = _FakeTicketService()

    def _run():
        active_ticket_id.set("ticket-multi")
        active_ticket_service.set(fake_svc)
        _accumulate_ticket_cost(make_response(0.0010))
        _accumulate_ticket_cost(make_response(0.0020))
        _accumulate_ticket_cost(make_response(0.0030))

    ctx = copy_context()
    ctx.run(_run)

    assert fake_svc.calls == 3
    # Last call details
    assert fake_svc.last_id == "ticket-multi"
    assert fake_svc.last_amount == pytest.approx(0.0030)

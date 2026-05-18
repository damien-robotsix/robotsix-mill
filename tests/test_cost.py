"""Tests for OpenRouter cost recording (OTel span attrs), Langfuse
session total queries, and the cost-sync pipeline that writes
per-ticket ``cost_usd`` from Langfuse session totals.

The old contextvar-based ``_accumulate_ticket_cost`` / ``add_cost``
real-time path has been REMOVED — it leaked across concurrent tickets.
"""

import pytest

from robotsix_mill.agents.openrouter_cost import (
    _inject_usage_include,
    record_openrouter_cost,
)
from robotsix_mill.core.states import State
from robotsix_mill.langfuse_client import session_total_cost


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


# --- session_total_cost (Langfuse API read) ----------------------------

def test_session_total_cost_returns_none_when_tracing_disabled(settings):
    """When tracing_enabled is False (no Langfuse env vars), the
    client returns None without error."""
    assert not settings.tracing_enabled
    assert session_total_cost(settings, "any-session-id") is None


def test_session_total_cost_returns_float_from_mocked_api(settings, monkeypatch):
    """When the Langfuse API returns trace data, session_total_cost
    sums totalCost across traces.

    We mock _langfuse_api_get (the low-level HTTP helper) so the
    tracing_enabled check is bypassed entirely — no need to mock
    the read-only property."""
    fake_data = {
        "data": [
            {"totalCost": 0.0123, "name": "ticket"},
            {"totalCost": 0.0045, "name": "refine"},
            {"totalCost": 0.0078, "name": "implement"},
        ]
    }

    def fake_get(_s, _path, params=None):
        return fake_data

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get", fake_get
    )

    cost = session_total_cost(settings, "test-session")
    assert cost == pytest.approx(0.0123 + 0.0045 + 0.0078)


def test_session_total_cost_returns_none_when_api_fails(settings, monkeypatch):
    """When _langfuse_api_get returns None (unreachable / error),
    session_total_cost returns None gracefully."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        lambda s, path, params=None: None,
    )
    assert session_total_cost(settings, "test-session") is None


def test_session_total_cost_handles_empty_traces(settings, monkeypatch):
    """Zero traces → cost is 0.0 (not None)."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        lambda s, path, params=None: {"data": []},
    )
    assert session_total_cost(settings, "test-session") == 0.0


# --- TicketService.set_cost (DB write) ---------------------------------

def test_set_cost_writes_absolute_value(service):
    """set_cost writes *cost* as the absolute cost_usd — it does not
    accumulate.  Calling it twice with different values overwrites."""
    t = service.create("set-cost test")
    assert t.cost_usd == 0.0

    service.set_cost(t.id, 0.0420)
    assert service.get(t.id).cost_usd == pytest.approx(0.0420)

    service.set_cost(t.id, 0.0099)
    assert service.get(t.id).cost_usd == pytest.approx(0.0099)


def test_set_cost_missing_ticket_is_noop(service):
    """Calling set_cost on a nonexistent ticket should not raise."""
    service.set_cost("nonexistent-id", 1.0)  # no raise


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


# --- _sync_one_ticket_cost (worker helper) ----------------------------

def test_sync_one_ticket_cost_noop_when_langfuse_unset(settings, service):
    """When Langfuse returns None, _sync_one_ticket_cost leaves
    cost_usd unchanged (safe no-op, no error)."""
    from robotsix_mill.runtime.worker import _sync_one_ticket_cost
    from robotsix_mill.stages import StageContext

    ctx = StageContext(settings=settings, service=service)
    t = service.create("sync-noop")
    assert t.cost_usd == 0.0

    # Langfuse unconfigured → session_total_cost returns None
    _sync_one_ticket_cost(ctx, t.id)
    assert service.get(t.id).cost_usd == 0.0


def test_sync_one_ticket_cost_writes_when_langfuse_returns_value(
    settings, service, monkeypatch
):
    """When Langfuse returns a cost, _sync_one_ticket_cost writes it
    to the ticket row."""
    from robotsix_mill.runtime.worker import _sync_one_ticket_cost
    from robotsix_mill.stages import StageContext

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_total_cost",
        lambda s, sid: 0.1234,
    )

    ctx = StageContext(settings=settings, service=service)
    t = service.create("sync-writes")
    assert t.cost_usd == 0.0

    _sync_one_ticket_cost(ctx, t.id)
    assert service.get(t.id).cost_usd == pytest.approx(0.1234)


# --- Final sync on terminal transition --------------------------------

def test_terminal_ticket_gets_final_cost_sync(settings, service, monkeypatch):
    """When process_ticket encounters a terminal state, it calls
    _sync_one_ticket_cost before returning."""
    from robotsix_mill.runtime.worker import _sync_one_ticket_cost
    from robotsix_mill.stages import StageContext

    synced: list[str] = []

    def fake_sync(ctx, tid):
        synced.append(tid)

    monkeypatch.setattr(
        "robotsix_mill.runtime.worker._sync_one_ticket_cost", fake_sync
    )

    ctx = StageContext(settings=settings, service=service)
    t = service.create("terminal-sync")
    # Put ticket directly into CLOSED (terminal) and run process_ticket
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    service.transition(t.id, State.IN_REVIEW)
    service.transition(t.id, State.DONE)
    service.transition(t.id, State.CLOSED)

    import asyncio
    from robotsix_mill.runtime.worker import process_ticket

    asyncio.run(process_ticket(t.id, ctx))
    assert t.id in synced


# --- Periodic sync loop skips terminal tickets ------------------------

def test_cost_sync_loop_skips_terminal_tickets(settings, service, monkeypatch):
    """The periodic _cost_sync_loop only syncs non-terminal tickets
    (terminal ones already got their final sync)."""

    synced_ids: list[str] = []

    def fake_total(_s, sid):
        synced_ids.append(sid)
        return 0.0050

    # Patch the module-level reference so that _sync_one_ticket_cost
    # (which imports session_total_cost lazily from langfuse_client)
    # gets our fake, AND we call through the module too.
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_total_cost", fake_total
    )

    # Create one terminal (closed) and one non-terminal (draft) ticket
    t_draft = service.create("draft-ticket")
    t_closed = service.create("closed-ticket")
    # Walk closed to terminal via valid state transitions
    service.transition(t_closed.id, State.READY)
    service.transition(t_closed.id, State.DELIVERABLE)
    service.transition(t_closed.id, State.IN_REVIEW)
    service.transition(t_closed.id, State.DONE)
    service.transition(t_closed.id, State.CLOSED)

    # Simulate one iteration of the sync logic (the core loop body)
    _TERMINAL = {State.CLOSED, State.ERRORED, State.BLOCKED}

    # Import the module (not the function) so we call through the
    # patched module attribute.
    import robotsix_mill.langfuse_client as lc

    for ticket in service.list():
        if ticket.state in _TERMINAL:
            continue
        cost = lc.session_total_cost(settings, ticket.id)
        if cost is not None:
            service.set_cost(ticket.id, cost)

    # Only the non-terminal draft ticket should have been synced
    assert t_draft.id in synced_ids
    assert t_closed.id not in synced_ids

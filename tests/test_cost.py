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


# --- session_cost: read-time cached resolver (replaces persisted cost)

def test_session_cost_returns_zero_when_unconfigured(settings):
    """No Langfuse → 0.0 (never None — callers don't special-case)."""
    from robotsix_mill.langfuse_client import session_cost, _cost_cache
    _cost_cache.clear()
    assert session_cost(settings, "sid-a") == 0.0


def test_session_cost_caches_within_ttl(settings, monkeypatch):
    """Second call within TTL does NOT re-hit Langfuse."""
    from robotsix_mill import langfuse_client as lc
    lc._cost_cache.clear()
    calls = []
    monkeypatch.setattr(
        lc, "session_total_cost",
        lambda s, sid: (calls.append(sid), 0.25)[1],
    )
    assert lc.session_cost(settings, "sid-b") == 0.25
    assert lc.session_cost(settings, "sid-b") == 0.25  # cached
    assert calls == ["sid-b"]  # only one underlying lookup


def test_session_cost_refreshes_after_ttl(settings, monkeypatch):
    """Past the TTL the value is re-fetched."""
    from robotsix_mill import langfuse_client as lc
    lc._cost_cache.clear()
    seq = iter([0.10, 0.99])
    monkeypatch.setattr(lc, "session_total_cost", lambda s, sid: next(seq))
    t = [1000.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: t[0])
    assert lc.session_cost(settings, "sid-c") == 0.10
    t[0] += lc._COST_TTL_SECONDS + 1
    assert lc.session_cost(settings, "sid-c") == 0.99


def test_session_cost_serves_stale_on_transient_failure(settings, monkeypatch):
    """If Langfuse fails (None) but we have a cached value, serve it
    rather than flipping the board to $0."""
    from robotsix_mill import langfuse_client as lc
    lc._cost_cache.clear()
    state = {"v": 0.42}
    monkeypatch.setattr(lc, "session_total_cost", lambda s, sid: state["v"])
    t = [500.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: t[0])
    assert lc.session_cost(settings, "sid-d") == 0.42
    state["v"] = None  # Langfuse now failing
    t[0] += lc._COST_TTL_SECONDS + 1  # force past cache
    assert lc.session_cost(settings, "sid-d") == 0.42  # last known

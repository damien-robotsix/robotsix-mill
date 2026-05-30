"""Tests for OpenRouter cost recording (OTel span attrs), Langfuse
session total queries, and the cost-sync pipeline that writes
per-ticket ``cost_usd`` from Langfuse session totals.

The old contextvar-based ``_accumulate_ticket_cost`` / ``add_cost``
real-time path has been REMOVED — it leaked across concurrent tickets.
"""

import pytest

from robotsix_mill.agents.openrouter_cost import (
    _PINNED_PROVIDER,
    _inject_provider_pin,
    _inject_usage_include,
    record_openrouter_cost,
)
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


# --- _inject_provider_pin (pin DeepSeek to keep prompt cache warm) ------


def test_provider_pin_set_for_deepseek():
    ms: dict = {}
    _inject_provider_pin((), {"model_settings": ms}, "deepseek/deepseek-v4-pro")
    assert ms["extra_body"]["provider"] == {
        "only": [_PINNED_PROVIDER],
        "allow_fallbacks": False,
    }


def test_provider_pin_skipped_for_non_deepseek():
    ms: dict = {}
    _inject_provider_pin((), {"model_settings": ms}, "openai/gpt-4o-mini")
    assert "provider" not in ms.get("extra_body", {})


def test_provider_pin_respects_caller_override():
    ms = {"extra_body": {"provider": {"order": ["Novita"]}}}
    _inject_provider_pin((), {"model_settings": ms}, "deepseek/deepseek-v4-pro")
    assert ms["extra_body"]["provider"] == {"order": ["Novita"]}  # untouched


def test_provider_pin_noop_when_no_settings():
    _inject_provider_pin((), {}, "deepseek/deepseek-v4-pro")  # must not raise


# --- DeepSeek reasoning round-trip 400 → transient retry ----------------
# Pinned to DeepSeek, deepseek-v4-pro can intermittently 400 demanding the
# prior turn's reasoning_content. We classify that as transient and retry
# (to learn if it's intermittent) rather than ship an unproven fix.


def test_deepseek_reasoning_roundtrip_400_is_transient():
    from robotsix_mill.agents.retry import is_transient

    class ModelHTTPError(Exception):
        def __init__(self):
            self.status_code = 400
            super().__init__(
                "status_code: 400, body: The reasoning_content in the "
                "thinking mode must be passed back to the API."
            )

    assert is_transient(ModelHTTPError()) is True


def test_other_400_stays_non_transient():
    from robotsix_mill.agents.retry import is_transient

    class ModelHTTPError(Exception):
        def __init__(self):
            self.status_code = 400
            super().__init__("status_code: 400, body: invalid model name")

    assert is_transient(ModelHTTPError()) is False


# --- _map_messages renames reasoning -> reasoning_content for deepseek ---


def _run_map(model, messages):
    import asyncio

    from pydantic_ai.models import ModelRequestParameters

    return asyncio.run(model._map_messages(messages, ModelRequestParameters()))


def test_deepseek_renames_reasoning_to_reasoning_content():
    pytest.importorskip("pydantic_ai.providers.openrouter")
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from robotsix_mill.agents.openrouter_cost import CostInstrumentedOpenRouterModel

    m = CostInstrumentedOpenRouterModel(
        "deepseek/deepseek-v4-pro", provider=OpenRouterProvider(api_key="x")
    )
    resp = ModelResponse(
        parts=[
            ThinkingPart(id="reasoning", content="thoughts", provider_name=m.system),
            TextPart(content="answer"),
        ]
    )
    asst = next(x for x in _run_map(m, [resp]) if x.get("role") == "assistant")
    assert asst.get("reasoning_content") == "thoughts"
    assert "reasoning" not in asst


def test_non_deepseek_keeps_reasoning_field():
    pytest.importorskip("pydantic_ai.providers.openrouter")
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from robotsix_mill.agents.openrouter_cost import CostInstrumentedOpenRouterModel

    m = CostInstrumentedOpenRouterModel(
        "openai/gpt-4o-mini", provider=OpenRouterProvider(api_key="x")
    )
    resp = ModelResponse(
        parts=[
            ThinkingPart(id="reasoning", content="t", provider_name=m.system),
            TextPart(content="a"),
        ]
    )
    asst = next(x for x in _run_map(m, [resp]) if x.get("role") == "assistant")
    assert "reasoning_content" not in asst


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
    # No cache attributes set when prompt_tokens_details is absent.
    assert "gen_ai.usage.cache_read_input_tokens" not in captured
    assert "gen_ai.usage.reasoning_tokens" not in captured


def test_record_sets_cache_span_attrs(monkeypatch):
    """OpenRouter cache and reasoning token details are surfaced on the
    OTel span when present in the usage object."""
    ot = pytest.importorskip("opentelemetry.trace")
    captured: dict = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            captured[k] = v

    monkeypatch.setattr(ot, "get_current_span", lambda: Span())

    class PromptDetails:
        cached_tokens = 470400
        cache_creation_input_tokens = 30000

    class CompletionDetails:
        reasoning_tokens = 500

    class U:
        model_extra = {"cost": 0.025}
        prompt_tokens = 500000
        completion_tokens = 16000
        prompt_tokens_details = PromptDetails()
        completion_tokens_details = CompletionDetails()

    class R:
        usage = U()
        model = "deepseek/deepseek-v4-pro"

    record_openrouter_cost(R())
    assert captured["gen_ai.usage.cache_read_input_tokens"] == 470400
    assert captured["gen_ai.usage.cache_creation_input_tokens"] == 30000
    assert captured["gen_ai.usage.reasoning_tokens"] == 500


def test_record_cache_attrs_dict_shape(monkeypatch):
    """prompt_tokens_details and completion_tokens_details may be dicts
    (depending on the provider/model)."""
    ot = pytest.importorskip("opentelemetry.trace")
    captured: dict = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            captured[k] = v

    monkeypatch.setattr(ot, "get_current_span", lambda: Span())

    class U:
        model_extra = {"cost": 0.01}
        prompt_tokens = 100
        completion_tokens = 50
        prompt_tokens_details = {"cached_tokens": 90, "cache_creation_input_tokens": 10}
        completion_tokens_details = {"reasoning_tokens": 5}

    class R:
        usage = U()
        model = "test-model"

    record_openrouter_cost(R())
    assert captured["gen_ai.usage.cache_read_input_tokens"] == 90
    assert captured["gen_ai.usage.cache_creation_input_tokens"] == 10
    assert captured["gen_ai.usage.reasoning_tokens"] == 5


def test_record_cache_partial_details_no_crash(monkeypatch):
    """When prompt_tokens_details exists but lacks some keys, no crash."""
    ot = pytest.importorskip("opentelemetry.trace")
    captured: dict = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            captured[k] = v

    monkeypatch.setattr(ot, "get_current_span", lambda: Span())

    class U:
        model_extra = {"cost": 0.01}
        prompt_tokens = 100
        completion_tokens = 50
        # Only cached_tokens, no cache_creation_input_tokens
        prompt_tokens_details = {"cached_tokens": 75}
        # completion_tokens_details absent entirely

    class R:
        usage = U()
        model = "test-model"

    record_openrouter_cost(R())
    assert captured["gen_ai.usage.cache_read_input_tokens"] == 75
    assert "gen_ai.usage.cache_creation_input_tokens" not in captured
    assert "gen_ai.usage.reasoning_tokens" not in captured


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

    def fake_get(_s, _path, params=None, repo_config=None):
        return fake_data

    monkeypatch.setattr("robotsix_mill.langfuse_client._langfuse_api_get", fake_get)

    cost = session_total_cost(settings, "test-session")
    assert cost == pytest.approx(0.0123 + 0.0045 + 0.0078)


def test_session_total_cost_returns_none_when_api_fails(settings, monkeypatch):
    """When _langfuse_api_get returns None (unreachable / error),
    session_total_cost returns None gracefully."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        lambda s, path, params=None, repo_config=None: None,
    )
    assert session_total_cost(settings, "test-session") is None


def test_session_total_cost_handles_empty_traces(settings, monkeypatch):
    """Zero traces → cost is 0.0 (not None)."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        lambda s, path, params=None, repo_config=None: {"data": []},
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
        lc,
        "session_total_cost",
        lambda s, sid, repo_config=None: (calls.append(sid), 0.25)[1],
    )
    assert lc.session_cost(settings, "sid-b") == 0.25
    assert lc.session_cost(settings, "sid-b") == 0.25  # cached
    assert calls == ["sid-b"]  # only one underlying lookup


def test_session_cost_refreshes_after_ttl(settings, monkeypatch):
    """Past the TTL the value is re-fetched."""
    from robotsix_mill import langfuse_client as lc

    lc._cost_cache.clear()
    seq = iter([0.10, 0.99])
    monkeypatch.setattr(
        lc, "session_total_cost", lambda s, sid, repo_config=None: next(seq)
    )
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
    monkeypatch.setattr(
        lc, "session_total_cost", lambda s, sid, repo_config=None: state["v"]
    )
    t = [500.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: t[0])
    assert lc.session_cost(settings, "sid-d") == 0.42
    state["v"] = None  # Langfuse now failing
    t[0] += lc._COST_TTL_SECONDS + 1  # force past cache
    assert lc.session_cost(settings, "sid-d") == 0.42  # last known


# --- fetch_session_summary: grouped summary with warnings/errors -------


def test_fetch_session_summary_returns_none_when_tracing_disabled(settings):
    from robotsix_mill.langfuse_client import fetch_session_summary

    assert not settings.tracing_enabled
    assert fetch_session_summary(settings, "any-session-id") is None


def test_fetch_session_summary_empty_traces(settings, monkeypatch):
    from robotsix_mill.langfuse_client import fetch_session_summary

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        lambda s, path, params=None, repo_config=None: {"data": []},
    )
    result = fetch_session_summary(settings, "sid")
    assert result == "(no Langfuse traces found for this session)"


def test_fetch_session_summary_groups_by_stage(settings, monkeypatch):
    """3 traces across 2 stages → ``## By stage`` with correct subtotals."""
    from robotsix_mill.langfuse_client import fetch_session_summary

    trace_list = {
        "data": [
            {
                "id": "t1",
                "name": "ticket",
                "totalCost": 0.01,
                "latency": 1.0,
                "observations": [{}],
            },
            {
                "id": "t2",
                "name": "implement",
                "totalCost": 0.03,
                "latency": 3.0,
                "observations": [{}, {}],
            },
            {
                "id": "t3",
                "name": "implement",
                "totalCost": 0.02,
                "latency": 2.0,
                "observations": [],
            },
        ]
    }

    # Per-trace detail calls return empty observations (no errors)
    def fake_get(_s, path, params=None, repo_config=None):
        if "/api/public/traces/" in path and path != "/api/public/traces":
            return {"observations": []}
        return trace_list

    monkeypatch.setattr("robotsix_mill.langfuse_client._langfuse_api_get", fake_get)

    result = fetch_session_summary(settings, "sid")
    assert result is not None
    assert "traces=3  total_cost=$0.0600  total_latency=6.0s" in result
    assert "## By stage" in result
    assert "- implement: $0.0500  5.0s  obs=2" in result
    assert "- ticket: $0.0100  1.0s  obs=1" in result
    # No errors → no Warnings/Errors section
    assert "## Warnings/Errors" not in result


def test_fetch_session_summary_warnings_errors(settings, monkeypatch):
    """Trace detail has ERROR-level observations → ``## Warnings/Errors``."""
    from robotsix_mill.langfuse_client import fetch_session_summary

    trace_list = {
        "data": [
            {
                "id": "t1",
                "name": "ticket",
                "totalCost": 0.01,
                "latency": 1.0,
                "observations": [],
            },
            {
                "id": "t2",
                "name": "implement",
                "totalCost": 0.03,
                "latency": 3.0,
                "observations": [],
            },
        ]
    }

    detail_map = {
        "t1": {"observations": []},
        "t2": {
            "observations": [
                {"level": "ERROR", "statusMessage": "tool call failed: EOF"},
                {"level": "WARNING", "statusMessage": "retry 1/3"},
            ]
        },
    }

    def fake_get(_s, path, params=None, repo_config=None):
        # Per-trace detail
        for tid in detail_map:
            if path == f"/api/public/traces/{tid}":
                return detail_map[tid]
        return trace_list

    monkeypatch.setattr("robotsix_mill.langfuse_client._langfuse_api_get", fake_get)

    result = fetch_session_summary(settings, "sid")
    assert result is not None
    assert "## By stage" in result
    assert "## Warnings/Errors" in result
    assert "- implement [ERROR] tool call failed: EOF" in result
    assert "- implement [WARNING] retry 1/3" in result


def test_fetch_session_summary_per_trace_fetch_fails_gracefully(settings, monkeypatch):
    """When fetch_trace_detail returns None, the trace still appears in
    ``## By stage`` but contributes no warnings/errors."""
    from robotsix_mill.langfuse_client import fetch_session_summary

    trace_list = {
        "data": [
            {
                "id": "t1",
                "name": "ticket",
                "totalCost": 0.01,
                "latency": 1.0,
                "observations": [],
            },
            {
                "id": "t2",
                "name": "implement",
                "totalCost": 0.03,
                "latency": 3.0,
                "observations": [],
            },
        ]
    }

    def fake_get(_s, path, params=None, repo_config=None):
        # t1 detail succeeds, t2 detail fails
        if path == "/api/public/traces/t1":
            return {"observations": [{"level": "ERROR", "statusMessage": "boom"}]}
        if path == "/api/public/traces/t2":
            return None  # simulates HTTP failure
        return trace_list

    monkeypatch.setattr("robotsix_mill.langfuse_client._langfuse_api_get", fake_get)

    result = fetch_session_summary(settings, "sid")
    assert result is not None
    # Both stages still appear
    assert "- ticket:" in result
    assert "- implement:" in result
    # Only t1's error shows up
    assert "## Warnings/Errors" in result
    assert "ticket [ERROR] boom" in result
    # t2's error should NOT appear (its detail fetch failed)
    assert "implement [ERROR]" not in result


def test_fetch_session_summary_warnings_capped_at_20(settings, monkeypatch):
    """More than 20 warnings/errors → truncated with a note."""
    from robotsix_mill.langfuse_client import fetch_session_summary

    # One trace with 25 ERROR observations
    many_obs = [{"level": "ERROR", "statusMessage": f"err {i}"} for i in range(25)]

    trace_list = {
        "data": [
            {
                "id": "t1",
                "name": "implement",
                "totalCost": 1.0,
                "latency": 10.0,
                "observations": [],
            },
        ]
    }

    def fake_get(_s, path, params=None, repo_config=None):
        if path == "/api/public/traces/t1":
            return {"observations": many_obs}
        return trace_list

    monkeypatch.setattr("robotsix_mill.langfuse_client._langfuse_api_get", fake_get)

    result = fetch_session_summary(settings, "sid")
    assert result is not None
    assert "## Warnings/Errors" in result
    # Should have exactly 20 error lines + 1 truncation note
    warning_lines = [
        line for line in result.split("\n") if line.startswith("- implement [ERROR]")
    ]
    assert len(warning_lines) == 20
    assert "(+5 more warnings/errors not shown)" in result

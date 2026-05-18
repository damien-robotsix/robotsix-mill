import pytest

from robotsix_mill.agents.openrouter_cost import (
    _inject_usage_include,
    record_openrouter_cost,
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

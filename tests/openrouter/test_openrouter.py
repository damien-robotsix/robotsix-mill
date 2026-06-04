"""OpenRouter transport layer — cost extraction, usage.include, transient."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from robotsix_llmio.openrouter.model import (
    PROVIDER_NAME,
    _get_cost_from_response,
    _inject_usage_include,
    _resolve_model_settings,
    record_openrouter_cost,
)
from robotsix_llmio.openrouter.transient import (
    is_openrouter_transient,
    is_openrouter_upstream_error,
)


def test_get_cost_from_usage_attr():
    resp = SimpleNamespace(usage=SimpleNamespace(cost=0.0123, model_extra=None))
    assert _get_cost_from_response(resp) == 0.0123


def test_get_cost_from_model_extra():
    resp = SimpleNamespace(usage=SimpleNamespace(model_extra={"cost": 0.5}))
    assert _get_cost_from_response(resp) == 0.5


def test_get_cost_none_when_absent():
    assert _get_cost_from_response(SimpleNamespace(usage=None)) is None
    assert (
        _get_cost_from_response(
            SimpleNamespace(usage=SimpleNamespace(cost=None, model_extra=None))
        )
        is None
    )


def test_inject_usage_include_sets_flag():
    ms: dict = {}
    _inject_usage_include((), {"model_settings": ms})
    assert ms["extra_body"]["usage"]["include"] is True


def test_inject_usage_include_preserves_existing_extra_body():
    ms = {"extra_body": {"provider": {"only": ["X"]}}}
    _inject_usage_include((), {"model_settings": ms})
    assert ms["extra_body"]["provider"]["only"] == ["X"]
    assert ms["extra_body"]["usage"]["include"] is True


def test_resolve_model_settings_from_args_position():
    ms = {"k": 1}
    assert _resolve_model_settings(("messages", False, ms, "params"), {}) is ms


def test_upstream_error_detected():
    class ValidationError(Exception):
        pass

    e = ValidationError("finish_reason expected one of ... got 'error'")
    assert is_openrouter_upstream_error(e) is True
    assert is_openrouter_transient(e) is True


def test_plain_validation_error_not_upstream():
    class ValidationError(Exception):
        pass

    assert is_openrouter_upstream_error(ValidationError("bad schema")) is False


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_noop_when_cost_none(mock_get_span):
    resp = SimpleNamespace(usage=None)
    assert record_openrouter_cost(resp) is None
    mock_get_span.assert_not_called()


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_noop_when_span_none(mock_get_span):
    mock_get_span.return_value = None
    resp = SimpleNamespace(usage=SimpleNamespace(cost=0.01, model_extra=None))
    assert record_openrouter_cost(resp) is None
    mock_get_span.assert_called_once()


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_always_set_attributes(mock_get_span):
    span = MagicMock()
    mock_get_span.return_value = span
    resp = SimpleNamespace(usage=SimpleNamespace(cost=0.02, model_extra=None))
    record_openrouter_cost(resp)
    span.set_attribute.assert_any_call("gen_ai.usage.cost", 0.02)
    span.set_attribute.assert_any_call(
        "langfuse.observation.cost_details", json.dumps({"total": 0.02})
    )
    span.set_attribute.assert_any_call("gen_ai.operation.name", "chat")
    span.set_attribute.assert_any_call("gen_ai.provider.name", PROVIDER_NAME)
    span.set_attribute.assert_any_call("gen_ai.system", PROVIDER_NAME)
    span.set_attribute.assert_any_call(
        "langfuse.observation.metadata.provider", PROVIDER_NAME
    )


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_model_and_token_attributes(mock_get_span):
    span = MagicMock()
    mock_get_span.return_value = span
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            cost=0.03,
            model_extra=None,
            prompt_tokens=10,
            completion_tokens=20,
        ),
        model="x/y",
    )
    record_openrouter_cost(resp)
    span.set_attribute.assert_any_call("gen_ai.request.model", "x/y")
    span.set_attribute.assert_any_call("gen_ai.usage.input_tokens", 10)
    span.set_attribute.assert_any_call("gen_ai.usage.output_tokens", 20)


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_cached_tokens_dict_details(mock_get_span):
    span = MagicMock()
    mock_get_span.return_value = span
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            cost=0.04,
            model_extra=None,
            prompt_tokens_details={
                "cached_tokens": 5,
                "cache_creation_input_tokens": 3,
            },
        ),
    )
    record_openrouter_cost(resp)
    span.set_attribute.assert_any_call("gen_ai.usage.cache_read_input_tokens", 5)
    span.set_attribute.assert_any_call("gen_ai.usage.cache_creation_input_tokens", 3)


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_reasoning_tokens_attr_details(mock_get_span):
    span = MagicMock()
    mock_get_span.return_value = span
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            cost=0.05,
            model_extra=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
        ),
    )
    record_openrouter_cost(resp)
    span.set_attribute.assert_any_call("gen_ai.usage.reasoning_tokens", 7)


@patch("robotsix_llmio.openrouter.model.get_recording_span")
def test_record_cost_absent_optional_fields_skipped(mock_get_span):
    span = MagicMock()
    mock_get_span.return_value = span
    resp = SimpleNamespace(usage=SimpleNamespace(cost=0.06, model_extra=None))
    record_openrouter_cost(resp)
    recorded = {c.args[0] for c in span.set_attribute.call_args_list}
    assert "gen_ai.usage.cost" in recorded
    assert "langfuse.observation.cost_details" in recorded
    assert "gen_ai.operation.name" in recorded
    assert "gen_ai.provider.name" in recorded
    assert "gen_ai.system" in recorded
    assert "langfuse.observation.metadata.provider" in recorded
    assert "gen_ai.request.model" not in recorded
    assert "gen_ai.usage.input_tokens" not in recorded
    assert "gen_ai.usage.output_tokens" not in recorded
    assert "gen_ai.usage.cache_read_input_tokens" not in recorded
    assert "gen_ai.usage.reasoning_tokens" not in recorded

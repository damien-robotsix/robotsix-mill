"""OpenRouter transport layer — cost extraction, usage.include, transient."""

from __future__ import annotations

from types import SimpleNamespace

from robotsix_llmio.openrouter.model import (
    _get_cost_from_response,
    _inject_usage_include,
    _resolve_model_settings,
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

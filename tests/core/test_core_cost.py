"""Core per-call cost recording — offline unit tests.

``core/cost.py`` exposes two public symbols that stamp/flush cost onto the
active OpenTelemetry span. Both are no-ops without a real OTel SDK, so they are
exercised here entirely through ``monkeypatch`` seams over the span lookup and
the lazy ``opentelemetry.trace`` import — no ``[tracing]`` extra, no SDK
provider setup, no network.

Patch-target notes:
- ``record_cost`` reads the span via ``get_recording_span``, which ``cost.py``
  pulled into its own namespace (``from ._otel import get_recording_span``), so
  the consumer name ``robotsix_llmio.core.cost.get_recording_span`` is patched.
- ``flush_current_provider`` lazy-imports ``opentelemetry.trace`` inside the
  body, so ``opentelemetry.trace.get_tracer_provider`` is patched on the real
  module.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from robotsix_llmio.core.cost import flush_current_provider, record_cost


class _FakeSpan:
    """Recording span fake: ``set_attribute`` accumulates into ``attributes``."""

    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


# --- §1 record_cost ----------------------------------------------------------


def test_record_cost_happy_path_with_provider(monkeypatch):
    """A float cost + recording span + provider stamps all four cost/operation
    attributes plus the provider metadata attribute."""
    span = _FakeSpan()
    monkeypatch.setattr(
        "robotsix_llmio.core.cost.get_recording_span", lambda: span
    )

    record_cost(object(), lambda _resp: 0.0123, provider="openrouter")

    assert span.attributes["gen_ai.usage.cost"] == 0.0123
    assert span.attributes["gen_ai.operation.name"] == "chat"
    details = span.attributes["langfuse.observation.cost_details"]
    assert json.loads(details) == {"total": 0.0123}
    assert span.attributes["langfuse.observation.metadata.provider"] == "openrouter"


def test_record_cost_without_provider(monkeypatch):
    """With no provider, the cost/operation attributes are stamped but the
    provider metadata attribute is NOT set."""
    span = _FakeSpan()
    monkeypatch.setattr(
        "robotsix_llmio.core.cost.get_recording_span", lambda: span
    )

    record_cost(object(), lambda _resp: 0.0123)

    assert span.attributes["gen_ai.usage.cost"] == 0.0123
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert json.loads(
        span.attributes["langfuse.observation.cost_details"]
    ) == {"total": 0.0123}
    assert "langfuse.observation.metadata.provider" not in span.attributes


def test_record_cost_no_op_without_recording_span(monkeypatch):
    """When no span is recording, the function returns ``None`` without
    stamping anything onto the (unused) span fake."""
    span = _FakeSpan()
    monkeypatch.setattr(
        "robotsix_llmio.core.cost.get_recording_span", lambda: None
    )

    result = record_cost(object(), lambda _resp: 0.0123, provider="openrouter")

    assert result is None
    assert span.attributes == {}


def test_record_cost_no_op_when_cost_is_none(monkeypatch):
    """When ``get_cost`` returns ``None``, the span lookup is never consulted
    and no attributes are set."""

    def _exploding_span():
        raise AssertionError("get_recording_span must not be called when cost is None")

    monkeypatch.setattr(
        "robotsix_llmio.core.cost.get_recording_span", _exploding_span
    )

    result = record_cost(object(), lambda _resp: None, provider="openrouter")

    assert result is None


# --- §2 flush_current_provider -----------------------------------------------


def _patch_tracer_provider(monkeypatch, provider):
    """Patch ``opentelemetry.trace.get_tracer_provider`` to return *provider*."""
    monkeypatch.setattr(
        "opentelemetry.trace.get_tracer_provider", lambda: provider
    )


def test_flush_current_provider_invokes_flush(monkeypatch):
    """A provider exposing ``force_flush`` has it invoked exactly once."""
    calls: list[int] = []
    provider = types.SimpleNamespace(force_flush=lambda: calls.append(1))
    _patch_tracer_provider(monkeypatch, provider)

    flush_current_provider()

    assert calls == [1]


def test_flush_current_provider_no_force_flush(monkeypatch):
    """A provider without ``force_flush`` is a silent no-op."""
    provider = types.SimpleNamespace()  # no force_flush attribute
    _patch_tracer_provider(monkeypatch, provider)

    # Must not raise.
    flush_current_provider()


def test_flush_current_provider_import_error(monkeypatch):
    """When ``from opentelemetry import trace`` raises ``ImportError``, the
    call returns silently."""
    # Setting the module entry to ``None`` makes ``from opentelemetry import
    # trace`` raise ImportError; monkeypatch auto-restores sys.modules.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    # Must not raise.
    flush_current_provider()


def test_flush_current_provider_swallows_runtime_and_os_error(monkeypatch):
    """``RuntimeError`` and ``OSError`` from ``force_flush`` are swallowed."""

    def _raise_runtime():
        raise RuntimeError("loop closed")

    def _raise_os():
        raise OSError("socket gone")

    _patch_tracer_provider(
        monkeypatch, types.SimpleNamespace(force_flush=_raise_runtime)
    )
    flush_current_provider()  # must not raise

    _patch_tracer_provider(
        monkeypatch, types.SimpleNamespace(force_flush=_raise_os)
    )
    flush_current_provider()  # must not raise


def test_flush_current_provider_propagates_other_errors(monkeypatch):
    """An exception type other than ``RuntimeError``/``OSError`` propagates."""

    def _raise_value():
        raise ValueError("programming error")

    _patch_tracer_provider(
        monkeypatch, types.SimpleNamespace(force_flush=_raise_value)
    )

    with pytest.raises(ValueError):
        flush_current_provider()

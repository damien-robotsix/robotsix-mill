"""Record per-call cost onto the active OpenTelemetry span.

Standard OTel only — no dependency on any particular tracing backend. A no-op
when OpenTelemetry isn't installed or no span is recording, so importing this
never forces the [tracing] extra. The ``langfuse.observation.cost_details``
attribute is emitted opportunistically (it is just a span attribute and is
ignored by backends that don't consume it).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ._otel import get_recording_span


def record_cost(response: Any, get_cost: Callable[[Any], float | None]) -> None:
    """Pull the USD cost out of *response* via *get_cost* and stamp it onto the
    current span using gen_ai semantic-convention attributes."""
    cost = get_cost(response)
    if cost is None:
        return
    span = get_recording_span()
    if span is None:
        return
    span.set_attribute("gen_ai.usage.cost", cost)
    span.set_attribute("gen_ai.operation.name", "chat")
    # Langfuse cost rollup (harmless span attribute for other backends).
    span.set_attribute("langfuse.observation.cost_details", json.dumps({"total": cost}))


def flush_current_provider() -> None:
    """Best-effort force-flush of the active OTel TracerProvider.

    Called around retry backoffs so spans export before a long sleep. No-op
    when OTel is absent or the provider can't flush.
    """
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
    except ImportError:
        return
    try:
        provider = otel_trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if flush is not None:
            flush()
    except Exception:  # pragma: no cover — never let flushing break a call
        pass

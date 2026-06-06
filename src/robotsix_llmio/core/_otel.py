"""Internal OpenTelemetry helpers — not part of the public API.

These exist to eliminate duplicated span-guard boilerplate across modules.
"""

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.trace import Span


def get_recording_span() -> "Span | None":
    """Return the current OTel span if recording, else None. No-op without OTel."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
    except ImportError:
        return None
    span = otel_trace.get_current_span()
    return span if (span is not None and span.is_recording()) else None


def get_tracer(name: str) -> Any | None:
    """Return an OTel tracer for *name*, or ``None`` when OpenTelemetry is not
    installed. The tracer late-binds to whatever provider is active when a span
    is started, so it's safe to fetch before tracing is configured."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
    except ImportError:
        return None
    return otel_trace.get_tracer(name)


@contextlib.contextmanager
def start_span(
    tracer: Any | None, name: str, attributes: dict[str, Any] | None = None
) -> Iterator[Any | None]:
    """Start a current span on *tracer*, set *attributes* (skipping ``None``
    values), and yield it. A no-op yielding ``None`` when *tracer* is ``None``
    (OTel absent); a non-recording span when no SDK provider is configured."""
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        yield span

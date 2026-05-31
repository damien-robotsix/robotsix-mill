"""Internal OpenTelemetry helpers — not part of the public API.

These exist to eliminate duplicated span-guard boilerplate across modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.trace import Span


def _get_recording_span() -> "Span | None":
    """Return the current OTel span if recording, else None. No-op without OTel."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
    except ImportError:
        return None
    span = otel_trace.get_current_span()
    return span if (span is not None and span.is_recording()) else None

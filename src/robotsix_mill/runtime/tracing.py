"""Optional OpenTelemetry tracing to Langfuse via OTLP/HTTP.

Zero imports from ``opentelemetry.*``, ``langfuse``, or ``pydantic_ai.agent``
at module level — everything is lazy behind ``_ensure_tracing()``.

When ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are both set, we
configure a global ``TracerProvider`` with an ``OTLPSpanExporter`` pointing
to Langfuse's OTLP endpoint, call ``Agent.instrument_all()`` so every
pydantic-ai agent run is automatically recorded, and expose context
managers for root ticket spans and pipeline stage spans.

When the env vars are absent, every function is a cheap no-op.
"""

from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from typing import Iterator

from ..config import Settings

_tracing_ready: bool | None = None  # tri-state: None=unchecked, True/False


def _tracing_enabled() -> bool:
    """Check env vars without importing anything."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def _ensure_tracing() -> None:
    """Lazily configure the global OTel tracer provider and instrument
    pydantic-ai agents.  Idempotent — subsequent calls are no-ops."""
    global _tracing_ready
    if _tracing_ready is not None:
        return

    if not _tracing_enabled():
        _tracing_ready = False
        return

    # --- heavy imports: gated behind the env-var check ---
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base_url = os.environ.get(
        "LANGFUSE_BASE_URL", "https://cloud.langfuse.com"
    ).rstrip("/")
    endpoint = f"{base_url}/api/public/otel/v1/traces"

    from base64 import b64encode as _b64encode

    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]

    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers={
            "Authorization": "Basic "
            + _b64encode(f"{public_key}:{secret_key}".encode()).decode(),
        },
    )

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: "robotsix-mill"}),
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    from pydantic_ai.agent import Agent

    Agent.instrument_all()

    _tracing_ready = True


def init(settings: Settings) -> None:
    """Backward-compatible entry point — no-op (everything is lazy now).

    Called during API startup for side-effect parity with the old
    Langfuse SDK. Does *not* trigger OTel imports.
    """
    pass


def flush_tracing() -> None:
    """Force-flush any pending spans.  Call at worker shutdown.

    No-op when tracing is off (env vars absent).
    """
    if _tracing_ready is not True:
        return
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()  # type: ignore[union-attr]


@contextmanager
def start_ticket_root_span(ticket_id: str) -> Iterator[None]:
    """Open a root OTel span named ``"ticket"`` with ``session.id`` attribute.

    Usage::

        with start_ticket_root_span(ticket_id):
            ...  # pipeline stages run here as children
    """
    _ensure_tracing()
    if not _tracing_ready:
        with nullcontext():
            yield
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("robotsix-mill")
    with tracer.start_as_current_span(
        "ticket",
        attributes={"session.id": ticket_id},
    ):
        yield


@contextmanager
def trace_stage(stage_name: str) -> Iterator[None]:
    """Create a child span of whatever span is currently active.

    Usage::

        with trace_stage("refine"):
            agent.run_sync(...)
    """
    _ensure_tracing()
    if not _tracing_ready:
        with nullcontext():
            yield
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("robotsix-mill")
    with tracer.start_as_current_span(stage_name):
        yield

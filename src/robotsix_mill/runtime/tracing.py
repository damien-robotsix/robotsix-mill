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

import contextvars
import os
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Iterator

from ..config import Settings

_tracing_ready: bool | None = None  # tri-state: None=unchecked, True/False

# The session id (ticket id / audit id) currently in scope. A
# context-var, not a parent span: pydantic-ai sub-agent runs (explore,
# web_research, test, rebase) start their OWN pydantic-ai trace, so the
# parent "ticket" span doesn't reliably propagate `session.id` to them.
# A SpanProcessor stamps this onto EVERY span at creation instead, so
# every trace — main or sub-agent — carries the session from the start.
# contextvars are copied into asyncio tasks and asyncio.to_thread, so
# this survives the agents' internal threading.
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mill_session_id", default=None
)


def make_session_id(kind: str) -> str:
    """Build a Langfuse session id: ``<kind>-<UTC-ts>-<uuid8>``.

    Use for non-ticket-driven flows (audit, health, agent-check,
    trace-health, deep-review).  Ticket-driven flows pass the ticket id
    directly to ``start_ticket_root_span`` — the ticket id is already a
    self-unique ``<ts>-<slug>-<hash>`` and serves as its own session id.
    """
    return (
        f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-"
        f"{uuid.uuid4().hex[:8]}"
    )


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

    from opentelemetry.sdk.trace import SpanProcessor

    class _SessionStampProcessor(SpanProcessor):
        """Stamp ``session.id`` (+ Langfuse's alias) onto every span at
        creation from the in-scope context-var. This makes the session
        association independent of span nesting, so pydantic-ai
        sub-agent runs (which open their own trace) are attributed to
        the same Langfuse session as the ticket/audit that spawned
        them — instead of appearing as orphan, untagged traces."""

        def on_start(self, span, parent_context=None):  # noqa: ANN001
            sid = _current_session.get()
            if sid:
                span.set_attribute("session.id", sid)
                # Langfuse also accepts this explicit alias.
                span.set_attribute("langfuse.session.id", sid)

        def on_end(self, span):  # noqa: ANN001
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis: int = 30000):
            return True

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: "robotsix-mill"}),
    )
    # on_start stamp first, then the batch exporter.
    provider.add_span_processor(_SessionStampProcessor())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    from pydantic_ai.agent import Agent

    Agent.instrument_all()

    _tracing_ready = True



def current_session() -> str | None:
    """Return the Langfuse session id currently in scope, or ``None``.

    This is the single public access point for the session context-var.
    No other module imports ``_current_session`` directly.
    """
    return _current_session.get()


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
def start_ticket_root_span(
    ticket_id: str,
    stage_name: str,
    extra_attributes: dict[str, str] | None = None,
) -> Iterator[None]:
    """Open a root OTel span for one stage of a ticket, named after the
    stage (e.g. ``"refine"``, ``"implement"``) with ``session.id``
    attribute set to the ticket id.

    Langfuse uses the OTel root span's name as the trace's display name.
    Before this took a stage_name, every trace was just titled ``ticket``
    in the Langfuse UI, which made the deep-review trace picker show a
    long list of identically-named rows. Naming the root span after the
    stage makes traces self-describing at a glance.

    ``extra_attributes`` — optional dict of additional span attributes
    to merge into the root span (e.g. ``{"source_trace_id": "..."}``).

    Usage::

        with start_ticket_root_span(ticket_id, "refine"):
            ...  # the refine stage runs here as the root span itself
    """
    _ensure_tracing()
    if not _tracing_ready:
        with nullcontext():
            yield
        return

    from opentelemetry import trace

    # Set the session context-var FIRST so the SpanProcessor stamps it
    # on the root span and every (sub-agent) span opened within — even
    # ones that start their own pydantic-ai trace.
    token = _current_session.set(ticket_id)
    try:
        tracer = trace.get_tracer("robotsix-mill")
        attrs: dict[str, str] = {"session.id": ticket_id}
        if extra_attributes:
            attrs.update(extra_attributes)
        with tracer.start_as_current_span(
            stage_name,
            attributes=attrs,
        ):
            yield
    finally:
        _current_session.reset(token)


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

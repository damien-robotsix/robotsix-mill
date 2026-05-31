"""Optional OpenTelemetry → Langfuse trace export — single- or multi-tenant.

Wires the Langfuse "login": an OTLP exporter per Langfuse project plus
``Agent.instrument_all()`` so every pydantic-ai model call — across all
providers — emits a span. Those spans already carry per-call cost (the provider
models stamp ``gen_ai.usage.cost`` / ``langfuse.observation.cost_details`` via
:mod:`robotsix_llmio.core.cost`), so registering an exporter is all it takes to
get per-provider traces *and* cost in Langfuse.

- **Single-tenant:** call :func:`setup_langfuse_tracing` once (credentials default
  to ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL``).
- **Multi-tenant:** call it once per project (passing each project's keys), then
  wrap work in :func:`langfuse_project` to route that work's spans to the right
  project. One process, many Langfuse projects — routed by a ``langfuse.public_key``
  span attribute stamped from context and a per-project filtered exporter.

Group a run's spans under one trace/session with :func:`langfuse_session`, or open
an explicit root span (with trace-level input/output) via :func:`start_trace`.

Opt-in and graceful: a no-op (returns ``False``) without credentials, so calling
it is always safe. Requires the ``tracing`` extra (``opentelemetry-sdk`` +
``opentelemetry-exporter-otlp-proto-http``).
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import os
import uuid
from typing import Any, Iterator

from ._otel import get_tracer, start_span

_DEFAULT_BASE_URL = "https://cloud.langfuse.com"

# The installed SDK TracerProvider (set once), the project public keys that
# already have an exporter, and the default project (first registered) used to
# route spans when no ``langfuse_project`` override is active.
_provider: Any = None
_registered_keys: set[str] = set()
_default_public_key: str | None = None

# Per-context routing: session id and target project, stamped onto every span.
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "robotsix_llmio_langfuse_session", default=None
)
_current_public_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "robotsix_llmio_langfuse_public_key", default=None
)


def _langfuse_otlp_endpoint(base_url: str) -> str:
    """The Langfuse OTLP traces endpoint for a base URL."""
    return f"{base_url.rstrip('/')}/api/public/otel/v1/traces"


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    """Build the ``Basic <base64>`` Authorization header value Langfuse expects."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def _active_public_key() -> str | None:
    """The project a span should route to: the contextual override if set, else
    the first registered project (the single-tenant default)."""
    return _current_public_key.get() or _default_public_key


def setup_langfuse_tracing(
    *,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    service_name: str = "robotsix-llmio",
) -> bool:
    """Register a Langfuse project for trace export and start instrumentation.

    Call once for single-tenant (credentials default to the ``LANGFUSE_*`` env
    vars), or once per project for multi-tenant routing (then use
    :func:`langfuse_project`). Idempotent per public key. Returns ``True`` when
    the project is registered, ``False`` when credentials are absent (no-op).
    """
    global _provider, _default_public_key

    public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return False
    if public_key in _registered_keys:
        return True
    base_url = base_url or os.environ.get("LANGFUSE_BASE_URL") or _DEFAULT_BASE_URL

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # One-time global setup: provider, the stamp processor, instrumentation.
    if _provider is None:
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider(
                resource=Resource.create({SERVICE_NAME: service_name})
            )
            trace.set_tracer_provider(provider)

        class _StampProcessor(SpanProcessor):
            """Stamp ``session.id`` + target ``langfuse.public_key`` onto every
            span from the active session/project context, so Langfuse groups the
            run and the filtered exporters can route it."""

            def on_start(self, span, parent_context=None):  # type: ignore[no-untyped-def]
                sid = _current_session.get()
                if sid:
                    span.set_attribute("session.id", sid)
                    span.set_attribute("langfuse.session.id", sid)
                pk = _active_public_key()
                if pk:
                    span.set_attribute("langfuse.public_key", pk)

            def on_end(self, span):  # type: ignore[no-untyped-def]
                pass

            def shutdown(self):  # type: ignore[no-untyped-def]
                pass

            def force_flush(self, timeout_millis: int = 30000):  # type: ignore[no-untyped-def]
                return True

        provider.add_span_processor(_StampProcessor())

        from pydantic_ai import Agent

        Agent.instrument_all()
        _provider = provider
        _default_public_key = public_key

    class _FilteredBatchSpanProcessor(BatchSpanProcessor):
        """Forward a span to this project's exporter only when the span's
        ``langfuse.public_key`` matches — the multi-tenant routing seam."""

        def __init__(self, exporter, *, target_public_key):  # type: ignore[no-untyped-def]
            super().__init__(exporter)
            self._target = target_public_key

        def on_end(self, span):  # type: ignore[no-untyped-def]
            attrs = span.attributes or {}
            if attrs.get("langfuse.public_key") != self._target:
                return  # belongs to a different project
            super().on_end(span)

    exporter = OTLPSpanExporter(
        endpoint=_langfuse_otlp_endpoint(base_url),
        headers={"Authorization": _basic_auth_header(public_key, secret_key)},
    )
    _provider.add_span_processor(
        _FilteredBatchSpanProcessor(exporter, target_public_key=public_key)
    )
    _registered_keys.add(public_key)
    return True


@contextlib.contextmanager
def langfuse_session(session_id: str) -> Iterator[None]:
    """Group all spans produced in this block under *session_id* in Langfuse."""
    token = _current_session.set(session_id)
    try:
        yield
    finally:
        _current_session.reset(token)


@contextlib.contextmanager
def langfuse_project(public_key: str) -> Iterator[None]:
    """Route spans produced in this block to the registered Langfuse project
    with *public_key* (multi-tenant). Spans whose project isn't registered are
    silently dropped by the filtered exporters."""
    token = _current_public_key.set(public_key)
    try:
        yield
    finally:
        _current_public_key.reset(token)


def current_session() -> str | None:
    """The session id active in the current context, or ``None``."""
    return _current_session.get()


def make_session_id(kind: str) -> str:
    """A unique session id of the form ``<kind>-<hex>`` for :func:`langfuse_session`."""
    return f"{kind}-{uuid.uuid4().hex}"


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


class TraceSpan:
    """Handle to a root span: set trace-level input/output, read the trace id."""

    def __init__(self, span: Any) -> None:
        self._span = span

    @property
    def trace_id(self) -> str | None:
        if self._span is None:
            return None
        ctx = self._span.get_span_context()
        tid = getattr(ctx, "trace_id", 0)
        return format(tid, "032x") if tid else None

    def set_input(self, value: Any) -> None:
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute("langfuse.observation.input", _to_text(value))

    def set_output(self, value: Any) -> None:
        if self._span is not None and self._span.is_recording():
            self._span.set_attribute("langfuse.observation.output", _to_text(value))


@contextlib.contextmanager
def start_trace(
    name: str,
    *,
    session_id: str | None = None,
    project: str | None = None,
) -> Iterator[TraceSpan]:
    """Open a root span *name* (the trace), optionally grouped under *session_id*
    and routed to *project* (a registered Langfuse public key). Use the yielded
    :class:`TraceSpan` to set trace-level input/output. Lets you group arbitrary
    work — multiple agent runs and non-agent steps — under one trace."""
    with contextlib.ExitStack() as stack:
        if session_id is not None:
            stack.enter_context(langfuse_session(session_id))
        if project is not None:
            stack.enter_context(langfuse_project(project))
        span = stack.enter_context(start_span(get_tracer("robotsix_llmio.tracing"), name))
        yield TraceSpan(span)


def flush_tracing(timeout_millis: int = 10_000) -> None:
    """Force-flush pending spans so they ship before the process exits (or so a
    test can read them back). No-op without OTel / a flushable provider."""
    try:
        from opentelemetry import trace
    except ImportError:
        return
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is None:
        return
    try:
        flush(timeout_millis=timeout_millis)
    except Exception:  # pragma: no cover — flushing must never raise
        pass

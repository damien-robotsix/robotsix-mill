"""Optional OpenTelemetry → Langfuse trace export (ported from robotsix-mill).

Wires the Langfuse "login": an OTLP exporter to a Langfuse project plus
``Agent.instrument_all()`` so every pydantic-ai model call — across all
providers — emits a span. Those spans already carry per-call cost (the provider
models stamp ``gen_ai.usage.cost`` / ``langfuse.observation.cost_details`` via
:mod:`robotsix_llmio.core.cost`), so configuring the exporter is all it takes to
get per-provider traces *and* cost in Langfuse.

Opt-in and fully graceful: :func:`setup_langfuse_tracing` is a no-op (returns
``False``) unless ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` are set (or
passed explicitly), so importing/calling it is always safe. Requires the
``tracing`` extra (``opentelemetry-sdk`` + ``opentelemetry-exporter-otlp-proto-http``).

Single-tenant by design: one Langfuse project per process. (robotsix-mill's
multi-repo span routing stays in mill.)
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import os
from typing import Iterator

_DEFAULT_BASE_URL = "https://cloud.langfuse.com"

_configured = False
# Session id stamped onto every span produced inside a ``langfuse_session``
# block, so Langfuse groups the run's spans under one session/trace.
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "robotsix_llmio_langfuse_session", default=None
)


def _langfuse_otlp_endpoint(base_url: str) -> str:
    """The Langfuse OTLP traces endpoint for a base URL."""
    return f"{base_url.rstrip('/')}/api/public/otel/v1/traces"


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    """Build the ``Basic <base64>`` Authorization header value Langfuse expects."""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def setup_langfuse_tracing(
    *,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    service_name: str = "robotsix-llmio",
) -> bool:
    """Configure OTel to export pydantic-ai spans (with cost) to Langfuse.

    Credentials default to ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
    ``LANGFUSE_BASE_URL`` (base URL defaults to Langfuse Cloud). Returns ``True``
    when tracing is active, ``False`` when credentials are absent (no-op).
    Idempotent: safe to call more than once; only the first call wires exporters.
    """
    global _configured
    if _configured:
        return True

    public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return False
    base_url = base_url or os.environ.get("LANGFUSE_BASE_URL") or _DEFAULT_BASE_URL

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(
        endpoint=_langfuse_otlp_endpoint(base_url),
        headers={"Authorization": _basic_auth_header(public_key, secret_key)},
    )

    class _SessionStampProcessor(SpanProcessor):
        """Stamp ``session.id`` (+ Langfuse alias) onto every span from the
        active ``langfuse_session`` context, so Langfuse groups the run."""

        def on_start(self, span, parent_context=None):  # type: ignore[no-untyped-def]
            sid = _current_session.get()
            if sid:
                span.set_attribute("session.id", sid)
                span.set_attribute("langfuse.session.id", sid)

        def on_end(self, span):  # type: ignore[no-untyped-def]
            pass

        def shutdown(self):  # type: ignore[no-untyped-def]
            pass

        def force_flush(self, timeout_millis: int = 30000):  # type: ignore[no-untyped-def]
            return True

    # Reuse an already-installed SDK provider if the consumer set one up; else
    # create and install ours. (The default global provider is a proxy, not an
    # SDK ``TracerProvider``.)
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: service_name})
        )
        trace.set_tracer_provider(provider)
    provider.add_span_processor(_SessionStampProcessor())
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Auto-instrument every pydantic-ai agent run so model calls emit spans.
    from pydantic_ai import Agent

    Agent.instrument_all()

    _configured = True
    return True


@contextlib.contextmanager
def langfuse_session(session_id: str) -> Iterator[None]:
    """Group all spans produced in this block under *session_id* in Langfuse.

    No-op effect on behaviour when tracing isn't configured — it just sets a
    context variable the session-stamp processor reads.
    """
    token = _current_session.set(session_id)
    try:
        yield
    finally:
        _current_session.reset(token)


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

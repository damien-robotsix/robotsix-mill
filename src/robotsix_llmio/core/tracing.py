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
from typing import Any, Callable, Iterator

from ._otel import get_recording_span as get_recording_span
from ._otel import get_tracer, start_span

_DEFAULT_BASE_URL = "https://cloud.langfuse.com"

# The installed SDK TracerProvider (set once), the registered projects (public
# key -> {base_url, project_id}, also the dedup set), and the default project
# (first registered) used to route spans when no ``langfuse_project`` override
# is active.
_provider: Any = None
_projects: dict[str, dict[str, Any]] = {}
_default_public_key: str | None = None
_shutdown_requested = False

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
    project_id: str | None = None,
    service_name: str = "robotsix-llmio",
    on_export_result: Callable[[str, bool, str | None], None] | None = None,
) -> bool:
    """Register a Langfuse project for trace export and start instrumentation.

    Call once for single-tenant (credentials default to the ``LANGFUSE_*`` env
    vars), or once per project for multi-tenant routing (then use
    :func:`langfuse_project`). *project_id* (or ``LANGFUSE_PROJECT_ID``) is
    optional and only used to build web-UI links via :func:`langfuse_trace_url`.
    Idempotent per public key. Returns ``True`` when the project is registered,
    ``False`` when credentials are absent (no-op).

    *on_export_result* is an optional health hook: when provided, every span
    export attempt for THIS project invokes it as
    ``on_export_result(public_key, ok, error)`` — ``ok=False`` with an error
    string on an exception or a non-success OTLP result, ``ok=True`` with
    ``None`` on success. Consumers use it to surface "Langfuse export broken"
    in their own UI and to auto-clear the flag once exports recover. The hook
    must not raise; exceptions from it are swallowed so it can never break the
    export path.
    """
    global _provider, _default_public_key

    public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
    if not (public_key and secret_key):
        return False
    project_id = project_id or os.environ.get("LANGFUSE_PROJECT_ID")
    if public_key in _projects:
        # Backfill a project id learned on a later call (for langfuse_trace_url).
        if project_id and not _projects[public_key].get("project_id"):
            _projects[public_key]["project_id"] = project_id
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

    _endpoint = _langfuse_otlp_endpoint(base_url)
    _headers = {"Authorization": _basic_auth_header(public_key, secret_key)}
    if on_export_result is None:
        exporter: Any = OTLPSpanExporter(endpoint=_endpoint, headers=_headers)
    else:
        from opentelemetry.sdk.trace.export import SpanExportResult

        class _ReportingExporter(OTLPSpanExporter):
            """Wrap the OTLP exporter so each export attempt reports its
            outcome to *on_export_result* — letting a consumer surface
            "Langfuse export broken" and auto-clear it on recovery, without
            ever breaking the export path (a raising hook is swallowed)."""

            def __init__(self, *a, _pk: str, _hook, **kw):  # type: ignore[no-untyped-def]
                super().__init__(*a, **kw)
                self._pk = _pk
                self._hook = _hook

            def _report(self, ok: bool, error: str | None) -> None:
                try:
                    self._hook(self._pk, ok, error)
                except Exception:  # noqa: BLE001 — a health hook must never break export
                    pass

            def export(self, spans):  # type: ignore[no-untyped-def]
                try:
                    result = super().export(spans)
                except Exception as e:  # noqa: BLE001
                    self._report(False, f"{type(e).__name__}: {e}")
                    return SpanExportResult.FAILURE
                self._report(
                    result == SpanExportResult.SUCCESS,
                    None
                    if result == SpanExportResult.SUCCESS
                    else "OTLP export returned FAILURE",
                )
                return result

        exporter = _ReportingExporter(
            endpoint=_endpoint,
            headers=_headers,
            _pk=public_key,
            _hook=on_export_result,
        )
    _provider.add_span_processor(
        _FilteredBatchSpanProcessor(exporter, target_public_key=public_key)
    )
    _projects[public_key] = {"base_url": base_url, "project_id": project_id}
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


def langfuse_trace_url(trace_id: str, *, public_key: str | None = None) -> str | None:
    """Build the Langfuse web-UI URL for *trace_id*, or ``None`` when the
    project's id isn't known. Uses *public_key*'s project, or the active/default
    one. A project id is only available when it was passed to
    :func:`setup_langfuse_tracing` (or set via ``LANGFUSE_PROJECT_ID``)."""
    pk = public_key or _active_public_key()
    info = _projects.get(pk) if pk else None
    if not info or not trace_id:
        return None
    project_id = info.get("project_id")
    if not project_id:
        return None
    return f"{info['base_url'].rstrip('/')}/project/{project_id}/traces/{trace_id}"


def install_signal_handlers() -> None:
    """Flush pending traces on ``SIGTERM`` / ``SIGINT`` before the process exits.

    Installs handlers that flush once and then ``SystemExit(0)``. No-op when not
    on the main thread (where signal handlers can't be installed). Call from your
    process entry point if you run long-lived workers."""
    import signal

    def _handler(signum: int, frame: Any) -> None:
        global _shutdown_requested
        if _shutdown_requested:
            return
        _shutdown_requested = True
        flush_tracing()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:  # pragma: no cover — not in the main thread
        pass


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
        span = stack.enter_context(
            start_span(get_tracer("robotsix_llmio.tracing"), name)
        )
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

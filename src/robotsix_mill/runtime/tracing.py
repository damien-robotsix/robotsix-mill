"""Optional OpenTelemetry tracing to Langfuse via OTLP/HTTP.

Zero imports from ``opentelemetry.*``, ``langfuse``, or ``pydantic_ai.agent``
at module level — everything is lazy behind ``_ensure_tracing()``.

When per-repo Langfuse credentials are available via ``RepoConfig``
(stamped onto ``Secrets`` at startup), we configure a global
``TracerProvider`` with an ``OTLPSpanExporter`` pointing to Langfuse's
OTLP endpoint, call ``Agent.instrument_all()`` so every pydantic-ai
agent run is automatically recorded, and expose context managers for
root ticket spans and pipeline stage spans.

When the credentials are absent, every function is a cheap no-op.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Iterator

from ..config import RepoConfig, get_secrets

_tracing_ready: bool | None = None  # tri-state: None=unchecked, True/False

_shutdown_requested: bool = False  # set by signal handlers to prevent double-flush

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


def _tracing_enabled(repo_config: RepoConfig | None = None) -> bool:
    """Check credentials without importing anything heavy.

    When *repo_config* is provided, its langfuse keys are checked;
    otherwise the global :class:`Secrets` singleton is used as a
    fallback for backward compatibility during the transition to
    per-repo credentials.
    """
    if repo_config is not None:
        return bool(
            repo_config.langfuse_public_key
            and repo_config.langfuse_secret_key
        )
    return bool(
        get_secrets().langfuse_public_key
        and get_secrets().langfuse_secret_key
    )


def _ensure_tracing(repo_config: RepoConfig | None = None) -> None:
    """Lazily configure the global OTel tracer provider and instrument
    pydantic-ai agents.  Idempotent — subsequent calls are no-ops.

    When *repo_config* is provided, its langfuse credentials are used;
    otherwise the global :class:`Secrets` singleton is used as a
    fallback for backward compatibility during the transition to
    per-repo credentials.
    """
    global _tracing_ready
    if _tracing_ready is not None:
        return

    if not _tracing_enabled(repo_config):
        _tracing_ready = False
        return

    # --- heavy imports: gated behind the env-var check ---
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if repo_config is not None:
            base_url = (repo_config.langfuse_base_url or "https://cloud.langfuse.com").rstrip("/")
            public_key = repo_config.langfuse_public_key
            secret_key = repo_config.langfuse_secret_key
            project_name = repo_config.langfuse_project_name
        else:
            secrets = get_secrets()
            base_url = (secrets.langfuse_base_url or "https://cloud.langfuse.com").rstrip("/")
            public_key = secrets.langfuse_public_key
            secret_key = secrets.langfuse_secret_key
            project_name = None

        endpoint = f"{base_url}/api/public/otel/v1/traces"

        from base64 import b64encode as _b64encode

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

        resource_attrs: dict[str, str] = {SERVICE_NAME: "robotsix-mill"}
        if project_name:
            resource_attrs["langfuse.project.name"] = project_name
        provider = TracerProvider(
            resource=Resource.create(resource_attrs),
        )
        # on_start stamp first, then the batch exporter.
        provider.add_span_processor(_SessionStampProcessor())
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        from pydantic_ai.agent import Agent

        Agent.instrument_all()

        _tracing_ready = True
    except ImportError:
        _tracing_ready = False



def current_session() -> str | None:
    """Return the Langfuse session id currently in scope, or ``None``.

    This is the single public access point for the session context-var.
    No other module imports ``_current_session`` directly.
    """
    return _current_session.get()


def flush_tracing(timeout: int = 10_000) -> None:
    """Force-flush any pending spans.  Call at worker shutdown.

    *timeout*: milliseconds to wait for the flush (passed to
    ``provider.force_flush(timeout_millis=...)``).  Default 10 s.

    No-op when tracing is off (env vars absent).
    """
    if _tracing_ready is not True:
        return
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=timeout)  # type: ignore[union-attr]


def install_signal_handlers() -> None:
    """Register handlers for SIGTERM and SIGINT that flush pending traces
    before the process exits.

    Each handler sets a module-level ``_shutdown_requested`` flag so
    double-\\^C or repeated signals don't deadlock on a slow flush.
    After the flush the handler raises ``SystemExit(0)``.

    All imports are lazy — no OTel symbols at module level.
    """
    import signal

    def _handler(signum: int, frame: object) -> None:
        global _shutdown_requested
        if _shutdown_requested:
            return  # already flushing; avoid re-entrant calls
        _shutdown_requested = True
        flush_tracing()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        pass  # not in main thread (e.g. under TestClient)


@contextmanager
def start_ticket_root_span(
    ticket_id: str,
    stage_name: str,
    extra_attributes: dict[str, str] | None = None,
    repo_config: RepoConfig | None = None,
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
    _ensure_tracing(repo_config)
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
def trace_stage(stage_name: str, repo_config: RepoConfig | None = None) -> Iterator[None]:
    """Create a child span of whatever span is currently active.

    Usage::

        with trace_stage("refine"):
            agent.run_sync(...)
    """
    _ensure_tracing(repo_config)
    if not _tracing_ready:
        with nullcontext():
            yield
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("robotsix-mill")
    with tracer.start_as_current_span(stage_name):
        yield

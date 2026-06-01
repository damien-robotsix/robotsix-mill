"""Optional OpenTelemetry tracing to Langfuse via OTLP/HTTP.

Zero imports from ``opentelemetry.*``, ``langfuse``, or ``pydantic_ai.agent``
at module level — everything is lazy behind ``_ensure_tracing()``.

When per-repo Langfuse credentials are available via ``RepoConfig``
(stamped onto ``Secrets`` at startup), we configure a global
``TracerProvider`` with an ``OTLPSpanExporter`` pointing to Langfuse's
OTLP endpoint, call
``Agent.instrument_all(InstrumentationSettings(event_mode='logs', version=1))``
so every pydantic-ai agent run is automatically recorded — message
content (prompts, tool calls, responses) is emitted as separate OTel
``LogRecord`` events under the GenAI semantic conventions rather than
being packed into span attributes, and expose context managers for
root ticket spans and pipeline stage spans.

When the credentials are absent, every function is a cheap no-op.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Iterator

from ..config import RepoConfig, get_secrets

log = logging.getLogger(__name__)

# Tri-state init flag for the global TracerProvider (one per process).
# Per-repo exporters are then registered lazily under the SAME provider —
# see _registered_keys + _FilteredBatchSpanProcessor below.
_provider_ready: bool | None = None  # None=unchecked, True=installed, False=disabled

# Set of Langfuse public_keys for which an exporter has been wired in.
# Used to keep _ensure_tracing idempotent per-repo without short-circuiting
# the whole function the way a single global flag did.
_registered_keys: set[str] = set()

# The TracerProvider we built and own. Held module-level so subsequent
# per-repo registrations can call ``add_span_processor`` on it directly
# rather than going through ``trace.get_tracer_provider()`` — the latter
# can return a ``ProxyTracerProvider`` (no ``add_span_processor`` method)
# when something else hit OTel's one-shot ``set_tracer_provider`` guard
# before us. Set inside the one-time init block in ``_ensure_tracing``.
_provider: object | None = None

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

# The Langfuse public_key for the repo whose stage is currently running.
# Stamped onto every span at start; _FilteredBatchSpanProcessor reads it
# at on_end to route the span to the matching repo's exporter, so traces
# for repo A never get billed to repo B's Langfuse project.
_current_pk: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mill_langfuse_pk", default=None
)


# Ring buffer of recent Langfuse export failures — surfaced to the UI
# via /langfuse-status so the operator notices when traces aren't
# making it through. (Without this, OTel's BatchSpanProcessor logs
# at DEBUG and the worker continues silently.)
_export_failures: list[dict] = []
_EXPORT_FAILURE_CAP = 20
_export_lock = __import__("threading").Lock()


def record_export_failure(
    *, project: str, error: str, status: int | None = None
) -> None:
    """Append a Langfuse export-failure entry; capped at the most
    recent ``_EXPORT_FAILURE_CAP`` items."""
    from datetime import datetime as _dt, timezone as _tz

    entry = {
        "at": _dt.now(_tz.utc).isoformat(),
        "project": project,
        "error": (error or "")[:500],
        "status": status,
    }
    with _export_lock:
        _export_failures.append(entry)
        if len(_export_failures) > _EXPORT_FAILURE_CAP:
            del _export_failures[: len(_export_failures) - _EXPORT_FAILURE_CAP]


def get_export_failures() -> list[dict]:
    """Return a snapshot of recent Langfuse export failures."""
    with _export_lock:
        return list(_export_failures)


def clear_export_failures_for(project: str) -> None:
    """Drop failure entries for *project*. Called from the exporter
    when a SUCCESS comes back so the UI's red badge clears on its
    own as soon as Langfuse recovers — without this the badge sticks
    until an operator hits POST /langfuse-status/clear and they end
    up seeing stale errors long after the export path is healthy."""
    with _export_lock:
        _export_failures[:] = [
            e for e in _export_failures if e.get("project") != project
        ]


def clear_export_failures() -> None:
    """Reset the failure log (e.g. after the operator acknowledges)."""
    with _export_lock:
        _export_failures.clear()


def _check_rejected_generation(span) -> None:  # noqa: ANN001
    """Annotate per-model-call spans where pydantic-ai's structured-
    output validator threw before ``gen_ai.output.messages`` was set.

    The model produced output tokens (paid for, visible in
    ``gen_ai.usage.output_tokens``) but the response was rejected so
    Langfuse renders empty output. Without this annotation the
    operator has no signal of the silent failure.

    Gated on ``gen_ai.operation.name == "chat"`` and the presence of
    ``gen_ai.input.messages`` so AGENT-orchestration spans (which
    aggregate child outputs and never carry their own output.messages)
    don't get false-positive warnings.

    Mutates ``span._attributes`` directly — OpenTelemetry's
    ``BoundedAttributes`` is a regular ``MutableMapping`` subclass and
    accepts writes even on already-ended spans.
    """
    attrs = span.attributes or {}
    is_per_call_span = attrs.get("gen_ai.operation.name") == "chat" and attrs.get(
        "gen_ai.input.messages"
    )
    if not is_per_call_span:
        return
    try:
        out_tokens = int(attrs.get("gen_ai.usage.output_tokens") or 0)
    except TypeError, ValueError:
        out_tokens = 0
    if out_tokens <= 0 or attrs.get("gen_ai.output.messages"):
        return
    msg = (
        f"model produced {out_tokens} output token(s) but no "
        "gen_ai.output.messages was set — pydantic-ai likely "
        "rejected the response (structured-output validation "
        "or schema mismatch). Check the parent run for an "
        "UnexpectedModelBehavior / output-retry failure."
    )
    try:
        span._attributes["langfuse.observation.status_message"] = msg
        span._attributes["langfuse.observation.level"] = "WARNING"
    except Exception:  # noqa: BLE001 — never break exporter
        pass


def make_session_id(kind: str) -> str:
    """Build a Langfuse session id: ``<kind>-<UTC-ts>-<uuid8>``.

    Use for non-ticket-driven flows (audit, health, agent-check,
    trace-health, deep-review).  Ticket-driven flows pass the ticket id
    directly to ``start_ticket_root_span`` — the ticket id is already a
    self-unique ``<ts>-<slug>-<hash>`` and serves as its own session id.
    """
    return f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _build_langfuse_url(
    entity_id: str,
    entity_type: str,
    repo_config: RepoConfig | None = None,
) -> str | None:
    """Build a Langfuse web-UI URL for a session or trace.

    When *repo_config* is provided, its ``langfuse_base_url`` and
    ``langfuse_project_name`` are used; otherwise the global
    :class:`Secrets` singleton is consulted.

    Returns ``None`` when any required ingredient is missing.
    """
    if repo_config is not None:
        base = (repo_config.langfuse_base_url or "https://cloud.langfuse.com").rstrip(
            "/"
        )
        project_id = repo_config.langfuse_project_name
    else:
        secrets = get_secrets()
        base = (secrets.langfuse_base_url or "https://cloud.langfuse.com").rstrip("/")
        project_id = secrets.langfuse_project_name or secrets.langfuse_project_id
    if entity_id and base and project_id:
        return f"{base}/project/{project_id}/{entity_type}/{entity_id}"
    return None


def langfuse_trace_url(
    trace_id: str, repo_config: RepoConfig | None = None
) -> str | None:
    """Build the Langfuse web-UI URL for a trace.

    Delegates to :func:`_build_langfuse_url` with ``entity_type="traces"``.
    Returns ``None`` when the base URL, project identifier, or trace ID
    is missing.
    """
    return _build_langfuse_url(trace_id, "traces", repo_config=repo_config)


def _tracing_enabled(repo_config: RepoConfig | None = None) -> bool:
    """Check credentials without importing anything heavy.

    When *repo_config* is provided, its langfuse keys are checked;
    otherwise the global :class:`Secrets` singleton is used as a
    fallback for backward compatibility during the transition to
    per-repo credentials.
    """
    if repo_config is not None:
        return bool(repo_config.langfuse_public_key and repo_config.langfuse_secret_key)
    return bool(get_secrets().langfuse_public_key and get_secrets().langfuse_secret_key)


def _ensure_tracing(repo_config: RepoConfig | None = None) -> None:
    """Lazily configure the global OTel tracer provider and register a
    Langfuse exporter for *repo_config*'s project.

    Two-phase idempotence: the global :class:`TracerProvider` is set up
    on the FIRST call (any repo); subsequent calls with a NEW repo only
    add another filtered exporter to the same provider, so traces are
    routed per-repo via the ``langfuse.public_key`` span attribute
    stamped by :class:`_SessionStampProcessor`.

    When *repo_config* is ``None``, the global :class:`Secrets`
    singleton's langfuse keys are used (single-repo / legacy mode).
    """
    global _provider, _provider_ready
    # Only short-circuit on the global-disable flag when the caller has
    # no per-repo credentials to offer (repo_config is None).  When a
    # RepoConfig IS provided, always re-evaluate _tracing_enabled even
    # if a previous call with a different (or absent) repo config
    # disabled tracing — per-repo credentials may be valid.
    if _provider_ready is False and repo_config is None:
        return
    if not _tracing_enabled(repo_config):
        # Only poison the global flag when the caller genuinely has no
        # repo-level creds to offer AND global Secrets are absent.
        # Per-repo calls with missing creds skip silently without
        # disabling other repos.
        if _provider_ready is None and repo_config is None:
            _provider_ready = False
        return

    # Resolve credentials for THIS call.
    if repo_config is not None:
        base_url = (
            repo_config.langfuse_base_url or "https://cloud.langfuse.com"
        ).rstrip("/")
        public_key = repo_config.langfuse_public_key
        secret_key = repo_config.langfuse_secret_key
        project_name = repo_config.langfuse_project_name
    else:
        secrets = get_secrets()
        base_url = (secrets.langfuse_base_url or "https://cloud.langfuse.com").rstrip(
            "/"
        )
        public_key = secrets.langfuse_public_key
        secret_key = secrets.langfuse_secret_key
        project_name = None

    # Already registered for this Langfuse project? Nothing to do.
    if public_key in _registered_keys:
        return

    # --- heavy imports: gated behind the env-var check ---
    try:
        # Bulky message content (prompts, tool calls, responses) is
        # carried by OTel LogRecord events under event_mode='logs'
        # (configured below) rather than span attributes, so the
        # default OTel attribute size limits are sufficient.

        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from base64 import b64encode as _b64encode

        from opentelemetry.sdk.trace.export import SpanExportResult

        class _ReportingExporter(OTLPSpanExporter):
            """OTLPSpanExporter wrapper that records failures so the
            UI can surface "Langfuse export broken" without the
            operator having to read worker logs."""

            def __init__(self, *args, project_label: str = "", **kw):
                super().__init__(*args, **kw)
                self._project_label = project_label

            def export(self, spans):  # noqa: ANN001
                try:
                    result = super().export(spans)
                except Exception as e:  # noqa: BLE001
                    record_export_failure(
                        project=self._project_label,
                        error=f"{type(e).__name__}: {e}",
                    )
                    log.warning(
                        "Langfuse export raised for %s: %s",
                        self._project_label,
                        e,
                    )
                    return SpanExportResult.FAILURE
                if result != SpanExportResult.SUCCESS:
                    record_export_failure(
                        project=self._project_label,
                        error="OTLP export returned FAILURE — "
                        "see worker logs for details",
                    )
                    log.warning(
                        "Langfuse export FAILURE for %s",
                        self._project_label,
                    )
                else:
                    # Auto-clear stale errors for this project once a
                    # batch lands successfully — Langfuse outages are
                    # transient and the badge should self-heal.
                    clear_export_failures_for(self._project_label)
                return result

        endpoint = f"{base_url}/api/public/otel/v1/traces"
        exporter = _ReportingExporter(
            endpoint=endpoint,
            project_label=project_name or public_key,
            headers={
                "Authorization": "Basic "
                + _b64encode(f"{public_key}:{secret_key}".encode()).decode(),
            },
        )

        # --- one-time global provider setup -----------------------------
        if _provider is None:

            class _SessionStampProcessor(SpanProcessor):
                """Stamp ``session.id`` (+ Langfuse alias) and the
                in-scope ``langfuse.public_key`` onto every span at
                creation, from contextvars. Independent of span nesting
                so pydantic-ai sub-agent runs — which open their own
                trace — are still attributed to the right session AND
                routed to the right repo's Langfuse project at export
                time by :class:`_FilteredBatchSpanProcessor`."""

                def on_start(self, span, parent_context=None):  # noqa: ANN001
                    sid = _current_session.get()
                    if sid:
                        span.set_attribute("session.id", sid)
                        span.set_attribute("langfuse.session.id", sid)
                    pk = _current_pk.get()
                    if pk:
                        # Read at on_end by _FilteredBatchSpanProcessor.
                        span.set_attribute("langfuse.public_key", pk)

                def on_end(self, span):  # noqa: ANN001
                    pass

                def shutdown(self):
                    pass

                def force_flush(self, timeout_millis: int = 30000):
                    return True

            resource_attrs: dict[str, str] = {SERVICE_NAME: "robotsix-mill"}
            provider = TracerProvider(
                resource=Resource.create(resource_attrs),
            )
            provider.add_span_processor(_SessionStampProcessor())
            trace.set_tracer_provider(provider)

            from pydantic_ai.agent import Agent, InstrumentationSettings

            # event_mode='logs' emits each pydantic-ai message (system
            # prompt, user turn, tool call, model response) as a separate
            # OTel LogRecord under the GenAI semantic conventions, rather
            # than packing the whole chat into span attributes. Avoids
            # the attribute-size truncation that the earlier strategy of
            # stamping message content into span attributes suffered
            # from, and gives Langfuse one observation per message for
            # cleaner rendering.
            Agent.instrument_all(InstrumentationSettings(event_mode="logs", version=1))
            _provider = provider
            _provider_ready = True

        # --- register this repo's filtered exporter ---------------------
        class _FilteredBatchSpanProcessor(BatchSpanProcessor):
            """Forward spans to a Langfuse project's OTLP endpoint only
            when their ``langfuse.public_key`` attribute matches —
            otherwise drop. Multiple instances coexist under the same
            global TracerProvider so each repo's traces land in its own
            Langfuse project.

            Also annotates per-model-call spans where pydantic-ai's
            structured-output validator silently rejected a response —
            see :func:`_check_rejected_generation`."""

            def __init__(self, exp, *, target_public_key: str):
                super().__init__(exp)
                self._target_pk = target_public_key

            def on_end(self, span):  # noqa: ANN001
                attrs = span.attributes or {}
                if attrs.get("langfuse.public_key") != self._target_pk:
                    return
                _check_rejected_generation(span)
                super().on_end(span)

        # Use the provider WE built (stored at init), not the OTel
        # global lookup — the latter can return a ``ProxyTracerProvider``
        # (no ``add_span_processor``) when something else races our
        # ``set_tracer_provider`` call. ``_provider`` is set inside the
        # one-time init block above.
        target_provider = (
            _provider if _provider is not None else trace.get_tracer_provider()
        )
        if not hasattr(target_provider, "add_span_processor"):
            log.warning(
                "tracing: TracerProvider lacks add_span_processor — "
                "skipping exporter registration for project %s "
                "(type=%s). Traces for this repo will not be exported.",
                project_name or public_key,
                type(target_provider).__name__,
            )
            _registered_keys.add(public_key)  # don't retry forever
            del project_name
            return
        target_provider.add_span_processor(
            _FilteredBatchSpanProcessor(exporter, target_public_key=public_key)
        )
        _registered_keys.add(public_key)
        # project_name is informational; routing is by public_key.
        del project_name
    except ImportError:
        _provider_ready = False


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
    if _provider_ready is not True:
        return
    from opentelemetry import trace

    provider = _provider if _provider is not None else trace.get_tracer_provider()
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


class _RootIO:
    """Setter handle yielded by :func:`start_ticket_root_span`.

    Use ``.set_input(...)`` / ``.set_output(...)`` to attach
    human-readable input + output payloads to the root span. Both are
    optional and any value is JSON-stringified (with a length cap to
    avoid blowing the OTel attribute size budget). Langfuse reads
    ``langfuse.observation.input`` / ``output`` and renders them on the
    trace at the top level — exactly the "global view" of the run.

    No-op when tracing is disabled or no span is currently recording.
    Callers that ignore the yielded value (existing ``with
    start_ticket_root_span(...):`` blocks without an ``as`` clause)
    continue to work unchanged.
    """

    _MAX_LEN = 8000  # OTel attribute soft cap to keep batches shippable

    def __init__(self, span):
        self._span = span

    @property
    def trace_id(self) -> str | None:
        """Return the 32-char hex trace id for the current OTel span.

        Returns ``None`` when ``self._span`` is ``None`` (tracing off).
        """
        if self._span is None:
            return None
        from opentelemetry.trace import format_trace_id

        trace_id_int = self._span.get_span_context().trace_id
        return format_trace_id(trace_id_int)

    def _serialize(self, value) -> str:
        if isinstance(value, str):
            s = value
        else:
            import json as _json

            try:
                s = _json.dumps(value, default=str, ensure_ascii=False)
            except TypeError, ValueError:
                s = str(value)
        if len(s) > self._MAX_LEN:
            s = s[: self._MAX_LEN] + "… (truncated)"
        return s

    def set_input(self, value) -> None:
        if self._span is None or not self._span.is_recording():
            return
        self._span.set_attribute("langfuse.observation.input", self._serialize(value))

    def set_output(self, value) -> None:
        if self._span is None or not self._span.is_recording():
            return
        self._span.set_attribute("langfuse.observation.output", self._serialize(value))


class _NoopRootIO:
    """Drop-in for ``_RootIO`` when tracing is disabled — accepts the
    same calls and silently discards them."""

    @property
    def trace_id(self) -> None:
        return None

    def set_input(self, value) -> None:  # noqa: D401, ARG002
        pass

    def set_output(self, value) -> None:  # noqa: D401, ARG002
        pass


@contextmanager
def start_ticket_root_span(
    ticket_id: str,
    stage_name: str,
    extra_attributes: dict[str, str] | None = None,
    repo_config: RepoConfig | None = None,
) -> Iterator["_RootIO | _NoopRootIO"]:
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

    Yields a :class:`_RootIO` setter the caller can use to attach
    trace-level input/output payloads (rendered at the top of the trace
    in Langfuse). Callers that ignore the yielded value continue to
    work unchanged::

        with start_ticket_root_span(ticket_id, "refine") as root:
            root.set_input({"title": …, "draft": …})
            ...  # stage runs
            root.set_output(result)
    """
    _ensure_tracing(repo_config)
    if not _provider_ready:
        with nullcontext():
            yield _NoopRootIO()
        return

    from opentelemetry import trace

    # Resolve the public_key for routing: per-repo first, fall back to
    # the global secrets pk (single-repo / legacy mode). Set BOTH the
    # session and pk context-vars FIRST so the SpanProcessor stamps them
    # on the root span and every (sub-agent) span opened within — even
    # ones that start their own pydantic-ai trace.
    if repo_config is not None and repo_config.langfuse_public_key:
        pk = repo_config.langfuse_public_key
    else:
        pk = get_secrets().langfuse_public_key or ""

    session_token = _current_session.set(ticket_id)
    pk_token = _current_pk.set(pk or None)
    try:
        tracer = trace.get_tracer("robotsix-mill")
        attrs: dict[str, str] = {"session.id": ticket_id}
        if extra_attributes:
            attrs.update(extra_attributes)
        with tracer.start_as_current_span(
            stage_name,
            attributes=attrs,
        ) as span:
            yield _RootIO(span)
    finally:
        _current_pk.reset(pk_token)
        _current_session.reset(session_token)


@contextmanager
def trace_stage(
    stage_name: str, repo_config: RepoConfig | None = None
) -> Iterator[None]:
    """Create a child span of whatever span is currently active.

    Usage::

        with trace_stage("refine"):
            agent.run_sync(...)
    """
    _ensure_tracing(repo_config)
    if not _provider_ready:
        with nullcontext():
            yield
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("robotsix-mill")
    with tracer.start_as_current_span(stage_name):
        yield


@contextmanager
def force_traces_to_mill(repo_config: RepoConfig) -> Iterator[None]:
    """Override ``_current_pk`` so every span inside the block is stamped
    with mill's own ``langfuse.public_key`` — not whatever per-repo key
    the current loop iteration happens to target.

    Usage::

        with force_traces_to_mill(mill_config):
            # spans here are routed to the robotsix-mill Langfuse project
            ...
    """
    _ensure_tracing(repo_config=repo_config)
    pk_token = _current_pk.set(repo_config.langfuse_public_key)
    try:
        yield
    finally:
        _current_pk.reset(pk_token)

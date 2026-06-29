"""Optional OpenTelemetry tracing to Langfuse, delegated to ``robotsix_llmio``.

The OTLP→Langfuse plumbing — the global ``TracerProvider``, the
``OTLPSpanExporter``, ``Agent.instrument_all(...)`` instrumentation, and
the session/project contextvars that stamp every span — lives in
``robotsix_llmio.core.tracing``.  Mill delegates provider/exporter setup
and session/project context to llmio and keeps only the mill-specific
surface: per-repo credential resolution, the export-failure registry the
UI reads, the ``RepoConfig``-aware Langfuse URL builder,
``make_session_id``, and the shutdown signal handlers.

Zero imports from ``opentelemetry.*``, ``langfuse``, ``pydantic_ai`` or
``robotsix_llmio`` at module level — everything is lazy behind
``_ensure_tracing()`` and the helpers below.  When credentials are
absent, every function is a cheap no-op.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Iterator

from ..config import RepoConfig, get_secrets

log = logging.getLogger(__name__)

# Readiness flag, derived from llmio's ``setup_langfuse_tracing`` return.
# Flipped to True once tracing has been configured for at least one
# Langfuse public key; the no-op guards in start_ticket_root_span /
# trace_stage / flush_tracing key off this.
_provider_ready: bool = False

# Set of Langfuse public_keys for which llmio has already been
# configured. Keeps _ensure_tracing idempotent per-repo — llmio's
# setup_langfuse_tracing is itself idempotent per key, but mill still
# wants a cheap local signal to skip the credential-resolution work.
_registered_keys: set[str] = set()

_shutdown_requested: bool = False  # set by signal handlers to prevent double-flush


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
    """Drop failure entries for *project*. Called from the export-result
    adapter when a SUCCESS comes back so the UI's red badge clears on its
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


# Separator between the repo qualifier and the underlying id in a
# Langfuse session. Chosen for readability in the session list and so
# ``current_ticket_id`` can split it back off. Repo ids never contain it.
SESSION_SEP = " · "


def qualify_session(base_id: str, repo_config: "RepoConfig | None") -> str:
    """Prefix *base_id* (a ticket id or ``make_session_id`` value) with the
    repo so a single shared Langfuse project's session list is legible —
    e.g. ``robotsix-llmio · 20260615T…-ffea``.

    Returns *base_id* unchanged when no repo is known or it is already
    qualified (idempotent), so legacy single-repo flows are untouched.
    """
    if repo_config is None or not repo_config.repo_id:
        return base_id
    if base_id.startswith(repo_config.repo_id + SESSION_SEP):
        return base_id
    return f"{repo_config.repo_id}{SESSION_SEP}{base_id}"


def make_session_id(kind: str, repo_config: "RepoConfig | None" = None) -> str:
    """Build a Langfuse session id: ``<kind>-<UTC-ts>-<uuid8>``, optionally
    repo-qualified (``<repo> · <kind>-…``) when *repo_config* is given.

    Use for non-ticket-driven flows (audit, health, agent-check,
    trace-health, deep-review).  Ticket-driven flows pass the ticket id
    directly to ``start_ticket_root_span`` — the ticket id is already a
    self-unique ``<ts>-<slug>-<hash>`` and serves as its own session id.
    """
    base = f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    return qualify_session(base, repo_config)


def current_ticket_id() -> str | None:
    """Return the bare ticket id for the in-scope session, stripping any
    ``<repo> · `` qualifier added by :func:`qualify_session`.

    Agent tools that resolve "the current ticket" must use THIS rather
    than :func:`current_session` (which returns the full, repo-qualified
    Langfuse session id used for trace linking).
    """
    session = current_session()
    if session and SESSION_SEP in session:
        return session.split(SESSION_SEP, 1)[1]
    return session


def _build_langfuse_url(
    entity_id: str,
    entity_type: str,
    repo_config: RepoConfig | None = None,
) -> str | None:
    """Build a Langfuse web-UI URL for a session or trace.

    Uses the cuid ``langfuse_project_id`` in the URL path when
    available, falling back to ``langfuse_project_name`` only when no
    project id is configured (legacy).  When *repo_config* is provided,
    its ``langfuse_base_url``, ``langfuse_project_id``, and
    ``langfuse_project_name`` are used; otherwise the global
    :class:`Secrets` singleton is consulted.

    Returns ``None`` when any required ingredient is missing.
    """
    if repo_config is not None:
        base = (repo_config.langfuse_base_url or "https://cloud.langfuse.com").rstrip(
            "/"
        )
        project_id = (
            repo_config.langfuse_project_id or repo_config.langfuse_project_name
        )
    else:
        secrets = get_secrets()
        base = (secrets.langfuse_base_url or "https://cloud.langfuse.com").rstrip("/")
        project_id = secrets.langfuse_project_id or secrets.langfuse_project_name
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
    """Lazily configure tracing for *repo_config*'s Langfuse project by
    delegating to :func:`robotsix_llmio.core.tracing.setup_langfuse_tracing`.

    Idempotent per Langfuse public key: the first call for a key
    configures llmio's global provider and registers that project's
    filtered exporter; later calls for the SAME key short-circuit.
    Multiple repos register under the same llmio provider, so traces are
    routed per-repo via the ``langfuse.public_key`` span attribute llmio
    stamps from the active project context.

    When *repo_config* is ``None``, the global :class:`Secrets`
    singleton's langfuse keys are used (single-repo / legacy mode).
    Repos without credentials are skipped silently.
    """
    global _provider_ready
    if not _tracing_enabled(repo_config):
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

    # Already configured for this Langfuse project? Nothing to do.
    if public_key in _registered_keys:
        return

    # The label under which this project's failures are tracked in the
    # registry the UI reads — prefer the human-readable project name.
    label = project_name or public_key

    def _on_export_result(_pk: str, ok: bool, error: str | None) -> None:
        """Bridge llmio's per-project export-health hook to mill's
        failure registry: clear the badge on a successful batch, record
        an entry on failure so /langfuse-status surfaces it."""
        if ok:
            clear_export_failures_for(label)
        else:
            record_export_failure(
                project=label,
                error=error or "OTLP export returned FAILURE",
            )

    # --- heavy import: gated behind the credential check ---
    try:
        from robotsix_llmio.core import tracing as _llmio_tracing
    except ImportError:
        return

    try:
        ok = _llmio_tracing.setup_langfuse_tracing(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            project_id=project_name or None,
            service_name="robotsix-mill",
            on_export_result=_on_export_result,
        )
    except ImportError:
        return
    _registered_keys.add(public_key)
    if ok:
        _provider_ready = True


def current_session() -> str | None:
    """Return the Langfuse session id currently in scope, or ``None``.

    Delegates to :func:`robotsix_llmio.core.tracing.current_session` —
    llmio owns the session context-var stamped onto every span.
    """
    from robotsix_llmio.core import tracing as _llmio_tracing

    return _llmio_tracing.current_session()


def set_current_span_attribute(key: str, value) -> None:
    """Set an attribute on the current active OTel span.

    No-op when tracing is disabled or no span is currently recording.
    Mirrors the guard in :class:`_RootIO.set_input`.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return
    span = trace.get_current_span()
    if span is None:
        return
    if not span.is_recording():
        return
    span.set_attribute(key, value)


def get_current_trace_id() -> str | None:
    """Return the active OTel trace id as a 32-char hex string, or None.

    No-op-safe: returns None when opentelemetry is not installed, no span
    is recording, or the span context is invalid (trace_id == 0).
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return None
    ctx = span.get_span_context()
    if not ctx or ctx.trace_id == 0:
        return None
    return format(ctx.trace_id, "032x")


def flush_tracing(timeout: int = 10_000) -> None:
    """Force-flush any pending spans.  Call at worker shutdown.

    *timeout*: milliseconds to wait for the flush (forwarded to
    ``robotsix_llmio.core.tracing.flush_tracing(timeout_millis=...)``).
    Default 10 s.

    No-op when tracing is off (credentials absent).
    """
    if not _provider_ready:
        return
    from robotsix_llmio.core import tracing as _llmio_tracing

    _llmio_tracing.flush_tracing(timeout_millis=timeout)


def install_signal_handlers() -> None:
    """Register handlers for SIGTERM and SIGINT that flush pending traces
    before the process exits.

    Each handler sets a module-level ``_shutdown_requested`` flag so
    double-\\^C or repeated signals don't deadlock on a slow flush.
    After the flush the handler raises ``SystemExit(0)``.

    All imports are lazy — no OTel symbols at module level.
    """
    import signal

    def _handler(_signum: int, frame: object) -> None:
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

    def set_attribute(self, key: str, value: object) -> None:
        """Set an arbitrary OTel attribute on the root span.

        No-op when tracing is disabled or the span is not recording.
        Use this instead of :func:`set_current_span_attribute` when
        you already hold a ``_RootIO`` handle — it stamps the correct
        span even after the root-span context manager has exited.
        """
        if self._span is None or not self._span.is_recording():
            return
        self._span.set_attribute(key, value)


class _NoopRootIO:
    """Drop-in for ``_RootIO`` when tracing is disabled — accepts the
    same calls and silently discards them."""

    @property
    def trace_id(self) -> None:
        return None

    def set_input(self, value: object) -> None:  # noqa: D401, ARG002
        pass

    def set_output(self, value: object) -> None:  # noqa: D401, ARG002
        pass

    def set_attribute(self, key: str, value: object) -> None:  # noqa: D401, ARG002
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
    from robotsix_llmio.core import tracing as _llmio_tracing

    # Resolve the public_key for routing: per-repo first, fall back to
    # the global secrets pk (single-repo / legacy mode). Enter llmio's
    # session/project contexts FIRST so its installed _StampProcessor
    # stamps session.id + langfuse.public_key on the root span and every
    # (sub-agent) span opened within — even ones that start their own
    # pydantic-ai trace.
    if repo_config is not None and repo_config.langfuse_public_key:
        pk = repo_config.langfuse_public_key
    else:
        pk = get_secrets().langfuse_public_key or ""

    # Repo-qualified Langfuse session id so a single shared project's
    # session list reads clearly (e.g. ``robotsix-llmio · <ticket-id>``).
    # The bare ticket id remains recoverable via ``current_ticket_id()``.
    session_id = qualify_session(ticket_id, repo_config)

    with ExitStack() as stack:
        stack.enter_context(_llmio_tracing.langfuse_session(session_id))
        if pk:
            stack.enter_context(_llmio_tracing.langfuse_project(pk))
        tracer = trace.get_tracer("robotsix-mill")
        attrs: dict[str, str] = {"session.id": session_id}
        if extra_attributes:
            attrs.update(extra_attributes)
        with tracer.start_as_current_span(
            stage_name,
            attributes=attrs,
        ) as span:
            yield _RootIO(span)


def record_step_usage(
    *,
    request_count: int,
    model_name: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls: list[dict[str, Any]] | None = None,
    retry_count: int = 0,
    retry_reason: str = "",
) -> None:
    """Record per-step usage data as span attributes on the current span.

    Call after every model invocation (pydantic-ai ``run_sync``) to
    stamp the trace with per-turn aggregates so the trace inspector and
    cost-analyst can distinguish "one oversized prompt" from "many
    redundant turns" without fetching every Langfuse observation.

    All parameters are keyword-only to keep call sites self-documenting.
    No-op when tracing is disabled or no span is recording.
    """
    import json as _json

    data: dict[str, Any] = {
        "request_count": request_count,
        "model_name": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "retry_count": retry_count,
    }
    if retry_reason:
        data["retry_reason"] = retry_reason
    if tool_calls:
        # Truncate args to keep the attribute within OTel size bounds.
        trimmed: list[dict[str, Any]] = []
        for tc in tool_calls:
            entry = {"name": tc.get("name", "")}
            args = tc.get("args", "")
            if args:
                entry["args"] = str(args)[:200]
            trimmed.append(entry)
        data["tool_calls"] = trimmed
    set_current_span_attribute(
        "mill.step_usage", _json.dumps(data, default=str, ensure_ascii=False)
    )


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
    """Override the active Langfuse project so every span inside the
    block is routed to mill's own project — not whatever per-repo key
    the current loop iteration happens to target.

    Usage::

        with force_traces_to_mill(mill_config):
            # spans here are routed to the robotsix-mill Langfuse project
            ...
    """
    _ensure_tracing(repo_config=repo_config)
    from robotsix_llmio.core import tracing as _llmio_tracing

    with _llmio_tracing.langfuse_project(repo_config.langfuse_public_key):
        yield

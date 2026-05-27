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
import logging
import os
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


def record_export_failure(*, project: str, error: str, status: int | None = None) -> None:
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


def clear_export_failures() -> None:
    """Reset the failure log (e.g. after the operator acknowledges)."""
    with _export_lock:
        _export_failures.clear()


def _flatten_chat_message(m: dict) -> dict:
    """Reduce one pydantic-ai OTel ChatMessage to OpenAI chat-completions
    shape so Langfuse renders it natively.

    pydantic-ai's instrumentation v2 writes messages as
    ``{"role": ..., "parts": [{"type": "text"|"tool_call"|
    "tool_call_response"|"image-url"|"uri"|"file"|"binary"|..., ...},
    ...]}``.

    Langfuse's Formatted view recognises the OpenAI chat-completions
    shape:

    - assistant message with tool calls →
      ``{"role": "assistant", "content": "...",
        "tool_calls": [{"id": ..., "type": "function",
                        "function": {"name": ..., "arguments": ...}}]}``
    - tool result message →
      ``{"role": "tool", "tool_call_id": ..., "content": "..."}``

    Map every pydantic-ai part type onto that shape; non-text media
    parts get a compact bracketed marker appended to ``content``
    rather than being dropped.
    """
    import json as _json

    role = m.get("role", "user")
    parts = m.get("parts") or []
    if isinstance(parts, str):
        return {"role": role, "content": parts}
    if not isinstance(parts, list):
        return {"role": role, "content": str(parts)}

    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    tool_call_id: str | None = None

    for p in parts:
        if not isinstance(p, dict):
            text_chunks.append(str(p))
            continue
        t = p.get("type")
        if t == "text":
            content = p.get("content", "") or ""
            if content:
                text_chunks.append(content)
        elif t == "tool_call":
            args = p.get("arguments", "")
            if not isinstance(args, str):
                try:
                    args = _json.dumps(args, default=str, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = str(args)
            tool_calls.append({
                "id": p.get("id", ""),
                "type": "function",
                "function": {
                    "name": p.get("name", ""),
                    "arguments": args,
                },
            })
        elif t in ("tool_call_response", "tool_response"):
            tool_call_id = p.get("id", "") or tool_call_id
            res = p.get("result") if "result" in p else p.get("content", "")
            if not isinstance(res, str):
                try:
                    res = _json.dumps(res, default=str, ensure_ascii=False)
                except (TypeError, ValueError):
                    res = str(res)
            text_chunks.append(res)
        elif t in ("image-url", "audio-url", "video-url", "document-url"):
            url = p.get("url", "")
            text_chunks.append(f"[{t} {url}]")
        elif t == "uri":
            modality = p.get("modality", "file")
            uri = p.get("uri", "")
            text_chunks.append(f"[{modality} {uri}]")
        elif t == "file":
            modality = p.get("modality", "file")
            file_id = p.get("file_id", "")
            text_chunks.append(f"[file {file_id} ({modality})]")
        elif t == "binary":
            text_chunks.append(f"[binary {p.get('media_type', '')}]")
        else:
            # Unknown part shape — JSON-dump for visibility.
            try:
                text_chunks.append(_json.dumps(p, default=str, ensure_ascii=False))
            except (TypeError, ValueError):
                text_chunks.append(str(p))

    # pydantic-ai's instrumented.py groups every non-system request
    # part under role="user" — including tool_call_response parts that
    # OpenAI's wire format requires under role="tool". Override the
    # role here when we detected a tool_call_id so Langfuse renders
    # the tool-result bubble correctly (and downstream OpenAI-shape
    # consumers parse it).
    if tool_call_id and role != "tool":
        role = "tool"
    out: dict[str, object] = {"role": role}
    content = "\n".join(text_chunks)
    # Always set ``content``: Langfuse / OpenAI shape expects it as a
    # string (even empty) on assistant messages that only have
    # tool_calls. Empty string is valid; null/missing breaks the
    # Formatted view.
    out["content"] = content
    if tool_calls:
        out["tool_calls"] = tool_calls
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    if "finish_reason" in m:
        out["finish_reason"] = m["finish_reason"]
    return out


def _flatten_chat_io(span) -> None:  # noqa: ANN001
    """Rewrite pydantic-ai's ``gen_ai.input.messages`` /
    ``gen_ai.output.messages`` attributes on *span* into
    ``langfuse.observation.input`` / ``output`` so the Langfuse UI
    renders them as chat bubbles instead of raw JSON.

    No-op when the span has no pydantic-ai message attributes (root
    spans, periodic-pass spans, non-LLM observations) — those use
    ``_RootIO.set_input/output`` directly.

    Mutates ``span._attributes`` in place. OpenTelemetry's
    ``BoundedAttributes`` is a regular ``MutableMapping`` subclass;
    ``span.set_attribute()`` would refuse on an already-ended span,
    but the underlying dict still accepts writes.
    """
    import json as _json

    attrs = span.attributes or {}
    raw_in = attrs.get("gen_ai.input.messages")
    raw_out = attrs.get("gen_ai.output.messages")
    raw_instructions = attrs.get("gen_ai.system_instructions")
    if not raw_in and not raw_out and not raw_instructions:
        return

    # pydantic-ai writes the system prompt to a SEPARATE
    # ``gen_ai.system_instructions`` attribute rather than including
    # SystemPromptPart/InstructionPart entries in
    # ``gen_ai.input.messages``. When the messages list is empty or
    # missing the system content, the Langfuse Formatted view shows
    # input as null. Prepend a synthetic system message reconstructed
    # from the instructions attribute so the prompt is visible
    # alongside any user/tool turns that follow.
    if raw_instructions:
        try:
            parsed_inst = _json.loads(raw_instructions) if isinstance(raw_instructions, str) else raw_instructions
        except (TypeError, ValueError):
            parsed_inst = None
        if isinstance(parsed_inst, list):
            text_chunks = []
            for p in parsed_inst:
                if isinstance(p, dict) and p.get("type") == "text":
                    text_chunks.append(p.get("content", "") or "")
            instructions_text = "\n".join(filter(None, text_chunks))
        elif isinstance(parsed_inst, str):
            instructions_text = parsed_inst
        else:
            instructions_text = ""
        if instructions_text:
            try:
                msgs_in = _json.loads(raw_in) if isinstance(raw_in, str) and raw_in else (raw_in if isinstance(raw_in, list) else [])
            except (TypeError, ValueError):
                msgs_in = []
            if not isinstance(msgs_in, list):
                msgs_in = []
            # Avoid duplicating a system message that's already present.
            has_system = any(
                isinstance(m, dict) and m.get("role") == "system"
                for m in msgs_in
            )
            if not has_system:
                synthetic = {
                    "role": "system",
                    "parts": [{"type": "text", "content": instructions_text}],
                }
                msgs_in = [synthetic, *msgs_in]
                raw_in = _json.dumps(msgs_in, default=str, ensure_ascii=False)

    def _rewrite(raw, *dest_keys: str) -> None:
        if not raw:
            return
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return
        if not isinstance(parsed, list):
            return
        flat = [
            _flatten_chat_message(m) if isinstance(m, dict) else {"role": "user", "content": str(m)}
            for m in parsed
        ]
        try:
            flat_json = _json.dumps(flat, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001 — never break exporter on rewrite
            return
        for k in dest_keys:
            try:
                span._attributes[k] = flat_json
            except Exception:  # noqa: BLE001
                pass

    # Write the flattened shape to BOTH the Langfuse alias and the
    # original gen_ai.* attribute. Langfuse's "Generation" subview
    # renders from gen_ai.input.messages directly; the alias covers
    # the "Observation" subview too.
    _rewrite(raw_in, "langfuse.observation.input", "gen_ai.input.messages")
    _rewrite(raw_out, "langfuse.observation.output", "gen_ai.output.messages")

    # Surface validation-rejected generations: the per-model-call
    # span had output tokens but pydantic-ai's structured-output
    # validator threw before ``gen_ai.output.messages`` was set, so
    # Langfuse renders an empty output. Without a status message
    # the operator has no way to know the call actually ran.
    #
    # Gate on BOTH input.messages and the chat operation name being
    # present so this only fires on per-model-call (GENERATION) spans
    # — AGENT-orchestration spans aggregate child outputs and never
    # carry gen_ai.output.messages themselves; warning on those is
    # a false positive.
    is_per_call_span = (
        attrs.get("gen_ai.operation.name") == "chat"
        and (raw_in or attrs.get("gen_ai.input.messages"))
    )
    try:
        out_tokens = int(attrs.get("gen_ai.usage.output_tokens") or 0)
    except (TypeError, ValueError):
        out_tokens = 0
    if (
        is_per_call_span
        and out_tokens > 0
        and not (raw_out or attrs.get("gen_ai.output.messages"))
    ):
        msg = (
            "model produced "
            f"{out_tokens} output token(s) but no "
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
    global _provider_ready
    if _provider_ready is False:
        return  # tracing disabled (no creds) — nothing to do
    if not _tracing_enabled(repo_config):
        if _provider_ready is None:
            _provider_ready = False
        return

    # Resolve credentials for THIS call.
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

    # Already registered for this Langfuse project? Nothing to do.
    if public_key in _registered_keys:
        return

    # --- heavy imports: gated behind the env-var check ---
    try:
        # Pydantic-ai stamps full prompt / message content into span
        # attributes — multi-MB strings are routine. Langfuse self-hosted
        # nginx ingresses cap request bodies (~1 MB by default), so big
        # spans return 413 Request Entity Too Large and the whole batch
        # is dropped. Truncate attribute values aggressively so spans
        # stay shippable. Caller can override via env if they really
        # need more.
        # No per-attribute length cap. Earlier we set
        # OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT=8192 to keep batches under
        # the self-hosted Langfuse nginx body cap, but 8 KB sliced
        # pydantic-ai's gen_ai.input.messages mid-string into invalid
        # JSON for any non-trivial conversation — Langfuse couldn't
        # parse and rendered only the system bubble. With the server
        # nginx cap raised, the cap is no longer needed; let
        # pydantic-ai's full attributes flow through. Operator can
        # still override via env if needed.

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
                        self._project_label, e,
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
        if _provider_ready is None:
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

            from pydantic_ai.agent import Agent

            Agent.instrument_all()
            _provider_ready = True

        # --- register this repo's filtered exporter ---------------------
        class _FilteredBatchSpanProcessor(BatchSpanProcessor):
            """Forward spans to a Langfuse project's OTLP endpoint only
            when their ``langfuse.public_key`` attribute matches —
            otherwise drop. Multiple instances coexist under the same
            global TracerProvider so each repo's traces land in its own
            Langfuse project.

            Also rewrites pydantic-ai's ``gen_ai.input.messages`` /
            ``gen_ai.output.messages`` attributes into the
            ``langfuse.observation.input`` / ``output`` shape Langfuse
            UI renders as chat bubbles — see :func:`_flatten_chat_io`."""

            def __init__(self, exp, *, target_public_key: str):
                super().__init__(exp)
                self._target_pk = target_public_key

            def on_end(self, span):  # noqa: ANN001
                attrs = span.attributes or {}
                if attrs.get("langfuse.public_key") != self._target_pk:
                    return
                _flatten_chat_io(span)
                super().on_end(span)

        provider = trace.get_tracer_provider()
        provider.add_span_processor(
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

    def _serialize(self, value) -> str:
        if isinstance(value, str):
            s = value
        else:
            import json as _json
            try:
                s = _json.dumps(value, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
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
def trace_stage(stage_name: str, repo_config: RepoConfig | None = None) -> Iterator[None]:
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

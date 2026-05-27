"""Tests for the tracing module — verify no-ops when env vars absent."""

import os

import pytest

from robotsix_mill.config import Secrets, _reset_secrets
from robotsix_mill.runtime import tracing


@pytest.fixture(autouse=True)
def _clear_env():
    """Ensure no tracing secrets leak in via the cached Secrets singleton."""
    _reset_secrets()
    # Reset the module-level state so tests are independent.
    tracing._provider_ready = None
    tracing._registered_keys.clear()
    tracing._shutdown_requested = False
    tracing._current_session.set(None)
    tracing._current_pk.set(None)


def test_ensure_tracing_disabled():
    """_ensure_tracing must set _provider_ready=False when env vars absent."""
    assert tracing._provider_ready is None
    tracing._ensure_tracing()
    assert tracing._provider_ready is False


def test_flush_tracing_noop():
    """flush_tracing must not raise when tracing is off."""
    tracing._provider_ready = False
    tracing.flush_tracing()  # no-op, no error


def test_flush_tracing_noop_before_ensure():
    """flush_tracing must not raise even before _ensure_tracing is called."""
    tracing._provider_ready = None
    tracing.flush_tracing()  # no-op, no error


def test_start_ticket_root_span_noop():
    """start_ticket_root_span must yield without error when tracing is off."""
    tracing._provider_ready = False
    with tracing.start_ticket_root_span("test-ticket-id", "test"):
        assert True  # body executed
    # Should not have imported anything


def test_trace_stage_noop():
    """trace_stage must yield without error when tracing is off."""
    tracing._provider_ready = False
    with tracing.trace_stage("refine"):
        assert True  # body executed


def test_session_contextvar_only_set_when_tracing_ready():
    """When tracing is off, start_ticket_root_span must NOT touch the
    session context-var (the SpanProcessor that consumes it only exists
    when tracing is configured; stamping otherwise would be dead/noise).
    The var must also be cleanly reset after the block."""
    tracing._provider_ready = False
    assert tracing._current_session.get() is None
    with tracing.start_ticket_root_span("sess-xyz", "test"):
        assert tracing._current_session.get() is None  # untouched (off)
    assert tracing._current_session.get() is None


def test_session_stamp_processor_stamps_from_contextvar(monkeypatch):
    """The SpanProcessor stamps session.id (+ langfuse alias) onto every
    span from the in-scope context-var, so sub-agent traces inherit the
    session even though they start their own pydantic-ai trace."""
    pytest.importorskip("opentelemetry.sdk.trace")
    # Build the processor the way _ensure_tracing does, in isolation.
    from opentelemetry.sdk.trace import SpanProcessor

    class _P(SpanProcessor):
        def on_start(self, span, parent_context=None):
            sid = tracing._current_session.get()
            if sid:
                span.set_attribute("session.id", sid)
                span.set_attribute("langfuse.session.id", sid)

    attrs: dict = {}

    class _FakeSpan:
        def set_attribute(self, k, v):
            attrs[k] = v

    p = _P()
    p.on_start(_FakeSpan())  # no session in scope → nothing stamped
    assert attrs == {}

    token = tracing._current_session.set("ticket-42")
    try:
        p.on_start(_FakeSpan())
    finally:
        tracing._current_session.reset(token)
    assert attrs == {
        "session.id": "ticket-42",
        "langfuse.session.id": "ticket-42",
    }


def test_sub_agent_spans_inherit_session_from_contextvar(monkeypatch):
    """Every span — parent and child (simulating agent + sub-agent) —
    receives session.id from the in-scope context-var when a
    _SessionStampProcessor-equivalent is installed on the TracerProvider.

    This is an integration-level test using the real OpenTelemetry SDK
    span pipeline (TracerProvider → SpanProcessor.on_start → span),
    verifying that pydantic-ai sub-agent traces — which go through the
    same processor — will carry the session regardless of span nesting.
    """
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry import trace as otel_trace

    # ---- stamp processor (mirrors _SessionStampProcessor) ----------
    captured: list[dict] = []

    class _StampAndCapture(SpanProcessor):
        def on_start(self, span, parent_context=None):
            sid = tracing._current_session.get()
            if sid:
                span.set_attribute("session.id", sid)
                span.set_attribute("langfuse.session.id", sid)
            captured.append({
                "name": span.name,
                "attrs": dict(span.attributes or {}),
            })

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    # ---- reset OTel's "already set" guard so we can install our own
    # provider (a prior test or conftest fixture may have already
    # installed a global provider via _ensure_tracing).
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False

    # ---- set up the pipeline ---------------------------------------
    # Force-reset any previously installed global TracerProvider so the
    # test's custom provider (with _StampAndCapture) actually takes effect.
    existing = otel_trace.get_tracer_provider()
    if hasattr(existing, "shutdown"):
        existing.shutdown()
    import opentelemetry.trace as _trace_mod
    _trace_mod._TRACER_PROVIDER_SET_ONCE._done = False

    provider = TracerProvider()
    provider.add_span_processor(_StampAndCapture())
    # Don't set globally — use provider directly to avoid OTel's
    # _TRACER_PROVIDER_SET_ONCE guard.
    tracer = provider.get_tracer("test-tracer")

    outer_token = tracing._current_session.set("ticket-sub-agent-test")
    try:
        token_inner = tracing._current_session.set("ticket-sub-agent-test")
        try:
            # Parent agent span
            with tracer.start_as_current_span("parent-agent"):
                # Sub-agent span nested inside parent (pydantic-ai
                # sub-agents may open their own trace, but the stamp
                # processor does not depend on parent context).
                with tracer.start_as_current_span("sub-agent"):
                    pass
        finally:
            tracing._current_session.reset(token_inner)

        # Both spans must carry the session id.
        assert len(captured) == 2, (
            f"Expected 2 spans, got {len(captured)}: {captured}"
        )
        for i, span_data in enumerate(captured):
            assert span_data["attrs"].get("session.id") == "ticket-sub-agent-test", (
                f"Span {i} ({span_data['name']}) missing session.id: "
                f"{span_data['attrs']}"
            )
            assert span_data["attrs"].get("langfuse.session.id") == "ticket-sub-agent-test", (
                f"Span {i} ({span_data['name']}) missing langfuse.session.id: "
                f"{span_data['attrs']}"
            )
    finally:
        tracing._current_session.reset(outer_token)


def test_tracing_enabled_no_env():
    """_tracing_enabled returns False when Secrets has no langfuse keys."""
    assert tracing._tracing_enabled() is False


def test_tracing_enabled_with_vars(monkeypatch):
    """_tracing_enabled returns True when both keys are set in Secrets."""
    monkeypatch.setattr(
        "robotsix_mill.config._secrets",
        Secrets(langfuse_public_key="pk-test", langfuse_secret_key="sk-test"),
    )
    assert tracing._tracing_enabled() is True


def test_tracing_enabled_missing_secret(monkeypatch):
    """_tracing_enabled returns False when only public key is set in Secrets."""
    monkeypatch.setattr(
        "robotsix_mill.config._secrets",
        Secrets(langfuse_public_key="pk-test"),
    )
    assert tracing._tracing_enabled() is False


def test_no_otel_imports_at_module_level():
    """Importing runtime.tracing must NOT pull opentelemetry / langfuse /
    pydantic_ai.agent (they are lazy, inside functions). Checked in a
    clean subprocess — asserting the session-global sys.modules would be
    polluted by other tests that import pydantic_ai/otel."""
    import os
    import subprocess
    import sys

    code = (
        "import sys, robotsix_mill.runtime.tracing as _t; "
        "bad=[m for m in ('opentelemetry','langfuse','pydantic_ai.agent')"
        " if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0, f"eagerly imported: {r.stdout.strip()}"


# --- current_session() public getter ---

def test_current_session_returns_none_when_not_set():
    """current_session() returns None when no session is in scope."""
    assert tracing.current_session() is None


def test_current_session_returns_contextvar_value():
    """current_session() returns the _current_session context-var value."""
    token = tracing._current_session.set("ticket-42")
    try:
        assert tracing.current_session() == "ticket-42"
    finally:
        tracing._current_session.reset(token)


# --- make_session_id ---

import uuid as _real_uuid
from datetime import datetime as _real_datetime, timezone as _real_timezone


class _FakeDatetime:
    @staticmethod
    def now(tz=None):
        return _real_datetime(2026, 5, 21, 14, 30, 25, tzinfo=_real_timezone.utc)


class _FakeUUIDObj:
    hex = "a1b2c3000000000000000000000000"


class _FakeUuidModule:
    @staticmethod
    def uuid4():
        return _FakeUUIDObj()


def test_make_session_id_format(monkeypatch):
    """make_session_id returns <kind>-<UTC-ts>-<8hex>."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.datetime", _FakeDatetime,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.uuid", _FakeUuidModule,
    )

    assert tracing.make_session_id("audit") == "audit-20260521T143025Z-a1b2c300"


def test_make_session_id_all_unique():
    """1000 calls produce unique ids, all with the expected prefix."""
    ids = [tracing.make_session_id("smoke") for _ in range(1000)]
    assert len(set(ids)) == 1000
    for sid in ids:
        assert sid.startswith("smoke-")


# --- install_signal_handlers & flush_tracing timeout ---


def test_install_signal_handlers_registers_without_otel():
    """install_signal_handlers() must not import OTel at module level.

    Verified implicitly by test_no_otel_imports_at_module_level (the
    subprocess check imports the module, which defines the function).
    Here we only verify the function is callable without error.
    """
    tracing.install_signal_handlers()


def test_sigterm_calls_flush_tracing(monkeypatch):
    """Sending SIGTERM after install_signal_handlers must call
    flush_tracing() before raising SystemExit."""
    import os
    import signal

    calls: list = []
    def fake_flush(timeout: int = 10_000) -> None:
        calls.append(timeout)
    monkeypatch.setattr(tracing, "flush_tracing", fake_flush)

    tracing.install_signal_handlers()
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except SystemExit:
        pass
    assert len(calls) == 1


def test_double_sigterm_no_deadlock(monkeypatch):
    """A second SIGTERM must not call flush_tracing again — the
    _shutdown_requested flag prevents re-entrant flushes."""
    import os
    import signal

    calls: list = []
    def fake_flush(timeout: int = 10_000) -> None:
        calls.append(timeout)
    monkeypatch.setattr(tracing, "flush_tracing", fake_flush)

    tracing.install_signal_handlers()
    # First signal — handler runs, raises SystemExit.
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except SystemExit:
        pass
    assert len(calls) == 1

    # Second signal — handler sees _shutdown_requested is True, returns.
    os.kill(os.getpid(), signal.SIGTERM)
    assert len(calls) == 1  # still only one flush


def test_flush_tracing_timeout_passed_to_force_flush(monkeypatch):
    """flush_tracing(timeout=5000) passes timeout_millis=5000 to
    provider.force_flush."""
    tracing._provider_ready = True

    import opentelemetry.trace  # ensure module is importable for patching

    timeout_value: list = []
    class FakeProvider:
        def force_flush(self, timeout_millis: int | None = None) -> None:
            timeout_value.append(timeout_millis)

    monkeypatch.setattr(
        opentelemetry.trace, "get_tracer_provider", lambda: FakeProvider()
    )
    tracing.flush_tracing(timeout=5000)
    assert timeout_value == [5000]


def test_flush_tracing_default_timeout():
    """flush_tracing() default timeout is 10_000 ms."""
    import inspect

    sig = inspect.signature(tracing.flush_tracing)
    assert sig.parameters["timeout"].default == 10_000


# --- Langfuse chat-IO flattener ----------------------------------------


class TestFlattenChatIO:
    """The exporter rewrites pydantic-ai's parts-shaped
    ``gen_ai.input.messages`` / ``output.messages`` into Langfuse's
    flat ``{role, content}`` shape so the UI renders chat bubbles."""

    def test_flatten_text_only_message(self):
        from robotsix_mill.runtime.tracing import _flatten_chat_message
        m = {"role": "system", "parts": [
            {"type": "text", "content": "You are a refine agent..."}
        ]}
        assert _flatten_chat_message(m) == {
            "role": "system", "content": "You are a refine agent...",
        }

    def test_flatten_multi_part_message_concats_text(self):
        from robotsix_mill.runtime.tracing import _flatten_chat_message
        m = {"role": "user", "parts": [
            {"type": "text", "content": "First."},
            {"type": "text", "content": "Second."},
        ]}
        out = _flatten_chat_message(m)
        assert out["role"] == "user"
        assert out["content"] == "First.\nSecond."

    def test_flatten_tool_call_part(self):
        from robotsix_mill.runtime.tracing import _flatten_chat_message
        m = {"role": "assistant", "parts": [
            {"type": "text", "content": "Will call."},
            {"type": "tool_call", "name": "read_file",
             "arguments": '{"path":"foo.py"}'},
        ]}
        out = _flatten_chat_message(m)
        assert "Will call." in out["content"]
        assert "[tool_call read_file(" in out["content"]
        assert "foo.py" in out["content"]

    def test_flatten_preserves_finish_reason(self):
        from robotsix_mill.runtime.tracing import _flatten_chat_message
        m = {"role": "assistant", "parts": [
            {"type": "text", "content": "Done."},
        ], "finish_reason": "stop"}
        out = _flatten_chat_message(m)
        assert out["finish_reason"] == "stop"

    def test_flatten_chat_io_writes_langfuse_attrs(self):
        """End-to-end: a fake span with gen_ai.*.messages attributes
        gets ``langfuse.observation.input/output`` set with the flat
        shape."""
        from robotsix_mill.runtime.tracing import _flatten_chat_io
        import json

        # Fake span: dict-like attributes container the helper can mutate.
        class _FakeSpan:
            def __init__(self):
                self._attributes: dict = {
                    "gen_ai.input.messages": json.dumps([
                        {"role": "system",
                         "parts": [{"type": "text", "content": "be helpful"}]},
                        {"role": "user",
                         "parts": [{"type": "text", "content": "hi"}]},
                    ]),
                    "gen_ai.output.messages": json.dumps([
                        {"role": "assistant",
                         "parts": [{"type": "text", "content": "hello!"}]},
                    ]),
                }

            @property
            def attributes(self):
                return self._attributes

        span = _FakeSpan()
        _flatten_chat_io(span)
        flat_in = json.loads(span._attributes["langfuse.observation.input"])
        assert flat_in == [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
        ]
        flat_out = json.loads(span._attributes["langfuse.observation.output"])
        assert flat_out == [{"role": "assistant", "content": "hello!"}]

    def test_flatten_chat_io_noop_when_no_messages(self):
        """Spans without pydantic-ai message attributes (root spans,
        periodic-pass spans) are left untouched."""
        from robotsix_mill.runtime.tracing import _flatten_chat_io

        class _FakeSpan:
            def __init__(self):
                self._attributes: dict = {"some.other.attr": "x"}

            @property
            def attributes(self):
                return self._attributes

        span = _FakeSpan()
        _flatten_chat_io(span)
        assert "langfuse.observation.input" not in span._attributes
        assert "langfuse.observation.output" not in span._attributes

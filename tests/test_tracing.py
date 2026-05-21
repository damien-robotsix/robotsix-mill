"""Tests for the tracing module — verify no-ops when env vars absent."""

import os

import pytest

from robotsix_mill.runtime import tracing


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Ensure no tracing env vars leak in."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    # Reset the module-level state so tests are independent.
    tracing._tracing_ready = None


def test_init_is_noop(settings):
    """init() must not trigger any imports or side effects."""
    tracing.init(settings)  # should not raise


def test_ensure_tracing_disabled():
    """_ensure_tracing must set _tracing_ready=False when env vars absent."""
    assert tracing._tracing_ready is None
    tracing._ensure_tracing()
    assert tracing._tracing_ready is False


def test_flush_tracing_noop():
    """flush_tracing must not raise when tracing is off."""
    tracing._tracing_ready = False
    tracing.flush_tracing()  # no-op, no error


def test_flush_tracing_noop_before_ensure():
    """flush_tracing must not raise even before _ensure_tracing is called."""
    tracing._tracing_ready = None
    tracing.flush_tracing()  # no-op, no error


def test_start_ticket_root_span_noop():
    """start_ticket_root_span must yield without error when tracing is off."""
    tracing._tracing_ready = False
    with tracing.start_ticket_root_span("test-ticket-id"):
        assert True  # body executed
    # Should not have imported anything


def test_trace_stage_noop():
    """trace_stage must yield without error when tracing is off."""
    tracing._tracing_ready = False
    with tracing.trace_stage("refine"):
        assert True  # body executed


def test_session_contextvar_only_set_when_tracing_ready():
    """When tracing is off, start_ticket_root_span must NOT touch the
    session context-var (the SpanProcessor that consumes it only exists
    when tracing is configured; stamping otherwise would be dead/noise).
    The var must also be cleanly reset after the block."""
    tracing._tracing_ready = False
    assert tracing._current_session.get() is None
    with tracing.start_ticket_root_span("sess-xyz"):
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


def test_tracing_enabled_no_env():
    """_tracing_enabled returns False when no vars set."""
    assert tracing._tracing_enabled() is False


def test_tracing_enabled_with_vars(monkeypatch):
    """_tracing_enabled returns True when both keys are set."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert tracing._tracing_enabled() is True


def test_tracing_enabled_missing_secret(monkeypatch):
    """_tracing_enabled returns False when only public key is set."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
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
    """make_session_id returns <kind>-<UTC-ts>-<6hex>."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.datetime", _FakeDatetime,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.uuid", _FakeUuidModule,
    )

    assert tracing.make_session_id("audit") == "audit-20260521T143025Z-a1b2c3"


def test_make_session_id_all_unique():
    """1000 calls produce unique ids, all with the expected prefix."""
    ids = [tracing.make_session_id("smoke") for _ in range(1000)]
    assert len(set(ids)) == 1000
    for sid in ids:
        assert sid.startswith("smoke-")

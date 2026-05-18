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

"""Langfuse trace-export setup — offline unit tests.

Covers the pure helpers, the credentials-absent no-op, session/project context
vars, the root-span handle, and the flush no-op. The full exporter wiring +
Langfuse round-trip (single- and multi-tenant) is exercised in
``tests/test_tracing_live.py`` (on-demand, gated by ``live``), so the offline
suite never installs a global TracerProvider.
"""

from __future__ import annotations

import base64

from robotsix_llmio.core import tracing
from robotsix_llmio.core.tracing import (
    _active_public_key,
    _basic_auth_header,
    _langfuse_otlp_endpoint,
    current_session,
    flush_tracing,
    install_signal_handlers,
    langfuse_project,
    langfuse_session,
    langfuse_trace_url,
    make_session_id,
    setup_langfuse_tracing,
    start_trace,
)


def test_otlp_endpoint_path():
    assert (
        _langfuse_otlp_endpoint("https://cloud.langfuse.com")
        == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    )
    # trailing slash tolerated
    assert (
        _langfuse_otlp_endpoint("https://lf.example.com/")
        == "https://lf.example.com/api/public/otel/v1/traces"
    )


def test_basic_auth_header_is_base64_public_secret():
    header = _basic_auth_header("pk-test", "sk-test")
    assert header.startswith("Basic ")
    assert base64.b64decode(header.split(" ", 1)[1]).decode() == "pk-test:sk-test"


def test_setup_is_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup_langfuse_tracing() is False


def test_setup_is_noop_with_only_one_key(monkeypatch):
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup_langfuse_tracing(public_key="pk-only") is False


# --- session + project routing context vars --------------------------------


def test_langfuse_session_sets_and_resets_contextvar():
    assert tracing._current_session.get() is None
    with langfuse_session("sess-1"):
        assert tracing._current_session.get() == "sess-1"
        with langfuse_session("sess-2"):  # nesting restores the outer value
            assert tracing._current_session.get() == "sess-2"
        assert tracing._current_session.get() == "sess-1"
    assert tracing._current_session.get() is None


def test_active_public_key_default_and_override(monkeypatch):
    # Default route is the first registered project; langfuse_project overrides.
    monkeypatch.setattr(tracing, "_default_public_key", "pk-default")
    assert _active_public_key() == "pk-default"
    with langfuse_project("pk-other"):
        assert _active_public_key() == "pk-other"
        with langfuse_project("pk-third"):
            assert _active_public_key() == "pk-third"
        assert _active_public_key() == "pk-other"
    assert _active_public_key() == "pk-default"


def test_active_public_key_none_when_no_default(monkeypatch):
    monkeypatch.setattr(tracing, "_default_public_key", None)
    assert _active_public_key() is None


def test_current_session_and_make_session_id():
    assert current_session() is None
    with langfuse_session("s-1"):
        assert current_session() == "s-1"
    sid = make_session_id("review")
    assert sid.startswith("review-") and len(sid) > len("review-")


# --- root-span handle + flush ----------------------------------------------


def test_start_trace_safe_without_provider():
    # No SDK provider in the offline suite → non-recording span → no-op handle.
    with start_trace("offline-trace", session_id="s", project="pk-x") as span:
        span.set_input({"a": 1})  # must not raise
        span.set_output("done")
        assert span.trace_id is None or isinstance(span.trace_id, str)


def test_flush_is_safe_noop_without_provider():
    flush_tracing()


# --- trace URL + signal handlers -------------------------------------------


def test_langfuse_trace_url_builds_from_registered_project(monkeypatch):
    monkeypatch.setattr(
        tracing,
        "_projects",
        {"pk-a": {"base_url": "https://lf.example.com", "project_id": "proj-123"}},
    )
    monkeypatch.setattr(tracing, "_default_public_key", "pk-a")
    # default project
    assert (
        langfuse_trace_url("abc123")
        == "https://lf.example.com/project/proj-123/traces/abc123"
    )
    # explicit project
    assert langfuse_trace_url("abc123", public_key="pk-a").endswith(
        "/project/proj-123/traces/abc123"
    )
    # unknown project -> None
    assert langfuse_trace_url("abc123", public_key="pk-missing") is None


def test_langfuse_trace_url_none_without_project_id(monkeypatch):
    monkeypatch.setattr(
        tracing,
        "_projects",
        {"pk-a": {"base_url": "https://x", "project_id": None}},
    )
    monkeypatch.setattr(tracing, "_default_public_key", "pk-a")
    assert langfuse_trace_url("abc") is None


def test_install_signal_handlers_is_safe():
    import signal

    orig_term = signal.getsignal(signal.SIGTERM)
    orig_int = signal.getsignal(signal.SIGINT)
    try:
        install_signal_handlers()  # must not raise; registers flush-on-signal
    finally:  # restore so we don't affect the rest of the test session
        signal.signal(signal.SIGTERM, orig_term)
        signal.signal(signal.SIGINT, orig_int)

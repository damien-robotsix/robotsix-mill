"""Langfuse trace-export setup — offline unit tests.

Covers the pure helpers, the credentials-absent no-op, the session context var,
and the flush no-op. The full exporter/provider wiring + Langfuse round-trip is
exercised in ``tests/test_tracing_live.py`` (on-demand, gated by ``live``), so
the offline suite never installs a global TracerProvider.
"""

from __future__ import annotations

import base64

from robotsix_llmio.core import tracing
from robotsix_llmio.core.tracing import (
    _basic_auth_header,
    _langfuse_otlp_endpoint,
    flush_tracing,
    langfuse_session,
    setup_langfuse_tracing,
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
    monkeypatch.setattr(tracing, "_configured", False)
    assert setup_langfuse_tracing() is False
    assert tracing._configured is False  # stays unconfigured


def test_setup_is_noop_with_only_one_key(monkeypatch):
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup_langfuse_tracing(public_key="pk-only") is False


def test_langfuse_session_sets_and_resets_contextvar():
    assert tracing._current_session.get() is None
    with langfuse_session("sess-1"):
        assert tracing._current_session.get() == "sess-1"
        with langfuse_session("sess-2"):  # nesting restores the outer value
            assert tracing._current_session.get() == "sess-2"
        assert tracing._current_session.get() == "sess-1"
    assert tracing._current_session.get() is None


def test_flush_is_safe_noop_without_provider():
    # No SDK provider installed in the offline suite → must not raise.
    flush_tracing()

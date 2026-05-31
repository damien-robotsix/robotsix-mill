"""Tests for the tracing module — verify no-ops when env vars absent."""

import os
from datetime import datetime as _real_datetime, timezone as _real_timezone

import pytest

from robotsix_mill.config import Secrets, _reset_secrets
from robotsix_mill.runtime import tracing


@pytest.fixture(autouse=True)
def _clear_env():
    """Ensure no tracing secrets leak in via the cached Secrets singleton."""
    _reset_secrets()
    # Reset the module-level state so tests are independent.
    tracing._provider_ready = None
    tracing._provider = None
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
            captured.append(
                {
                    "name": span.name,
                    "attrs": dict(span.attributes or {}),
                }
            )

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
        assert len(captured) == 2, f"Expected 2 spans, got {len(captured)}: {captured}"
        for i, span_data in enumerate(captured):
            assert span_data["attrs"].get("session.id") == "ticket-sub-agent-test", (
                f"Span {i} ({span_data['name']}) missing session.id: "
                f"{span_data['attrs']}"
            )
            assert (
                span_data["attrs"].get("langfuse.session.id") == "ticket-sub-agent-test"
            ), (
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
        "robotsix_mill.runtime.tracing.datetime",
        _FakeDatetime,
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.uuid",
        _FakeUuidModule,
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


class TestCheckRejectedGeneration:
    """Annotate model-call spans where pydantic-ai silently rejected
    the response (output tokens billed, but no gen_ai.output.messages
    landed because the structured-output validator threw)."""

    def test_warns_on_per_call_span_with_no_output(self):
        from robotsix_mill.runtime.tracing import _check_rejected_generation

        class _FakeSpan:
            def __init__(self):
                self._attributes: dict = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.input.messages": "[{}]",
                    "gen_ai.usage.output_tokens": 2636,
                }

            @property
            def attributes(self):
                return self._attributes

        span = _FakeSpan()
        _check_rejected_generation(span)
        assert span._attributes["langfuse.observation.level"] == "WARNING"
        assert (
            "pydantic-ai likely"
            in span._attributes["langfuse.observation.status_message"]
        )

    def test_no_warn_on_agent_span(self):
        from robotsix_mill.runtime.tracing import _check_rejected_generation

        class _FakeSpan:
            def __init__(self):
                self._attributes: dict = {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.usage.output_tokens": 22,
                }

            @property
            def attributes(self):
                return self._attributes

        span = _FakeSpan()
        _check_rejected_generation(span)
        assert "langfuse.observation.status_message" not in span._attributes

    def test_no_warn_when_output_messages_present(self):
        from robotsix_mill.runtime.tracing import _check_rejected_generation

        class _FakeSpan:
            def __init__(self):
                self._attributes: dict = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.input.messages": "[{}]",
                    "gen_ai.output.messages": "[{}]",
                    "gen_ai.usage.output_tokens": 5,
                }

            @property
            def attributes(self):
                return self._attributes

        span = _FakeSpan()
        _check_rejected_generation(span)
        assert "langfuse.observation.status_message" not in span._attributes


# --- _ensure_tracing per-repo resilience tests -------------------------


def test_ensure_tracing_recovers_after_no_global_creds(monkeypatch):
    """A prior failed global check (_provider_ready=False, repo_config=None
    with no global Secrets creds) must NOT block a subsequent per-repo
    call with a valid RepoConfig."""
    from robotsix_mill.config import RepoConfig

    # Simulate a prior failed global check.
    tracing._provider_ready = False

    valid_config = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk-valid",
        langfuse_secret_key="sk-valid",
    )

    # Should NOT short-circuit — the per-repo config has valid creds.
    tracing._ensure_tracing(repo_config=valid_config)
    # If we got here without the short-circuit returning, that's the pass.
    # _provider_ready should now be True (the heavy init succeeded… or
    # at minimum not stay False, since we imported OTel). The test's
    # _clear_env fixture ensures no real OTel modules are loaded before
    # us, so the heavy import block runs and sets _provider_ready to True.
    assert tracing._provider_ready is True, (
        "per-repo call with valid creds must proceed past the gate "
        "even after a prior global disable"
    )


def test_ensure_tracing_no_global_creds_does_not_poison_per_repo(monkeypatch):
    """_ensure_tracing() with no repo_config and no global Langfuse creds
    must NOT permanently set _provider_ready=False in a way that blocks
    subsequent per-repo calls."""
    from robotsix_mill.config import RepoConfig

    # First call: no repo_config, global Secrets has no creds.
    tracing._ensure_tracing()

    # The global-disable flag must be set (since we have no per-repo config).
    assert tracing._provider_ready is False

    # Now a per-repo call with valid creds must NOT be blocked.
    valid_config = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk-valid",
        langfuse_secret_key="sk-valid",
    )
    tracing._ensure_tracing(repo_config=valid_config)
    assert tracing._provider_ready is True, (
        "per-repo call with valid creds must proceed after a global "
        "disable — the gate must not short-circuit when repo_config "
        "is provided"
    )


def test_ensure_tracing_skips_repo_without_creds_without_poisoning(monkeypatch):
    """_ensure_tracing(repo_config=no_creds_config) for a repo without
    per-repo creds must skip silently WITHOUT setting _provider_ready
    to False globally."""
    from robotsix_mill.config import RepoConfig

    no_creds_config = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="",
        langfuse_secret_key="",
    )

    tracing._ensure_tracing(repo_config=no_creds_config)

    # _provider_ready must still be None (not poisoned to False) because
    # the caller had a repo_config — it just had no creds.
    assert tracing._provider_ready is None, (
        "per-repo call with no creds must NOT poison the global flag"
    )

    # A subsequent call with valid creds must proceed.
    valid_config = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk-valid",
        langfuse_secret_key="sk-valid",
    )
    tracing._ensure_tracing(repo_config=valid_config)
    assert tracing._provider_ready is True


# --- force_traces_to_mill -------------------------------------------------


MILL_CONFIG = __import__("robotsix_mill.config", fromlist=["RepoConfig"]).RepoConfig(
    repo_id="mill",
    board_id="mill-board",
    langfuse_project_name="robotsix-mill",
    langfuse_public_key="pk-mill",
    langfuse_secret_key="sk-mill",
)

OTHER_CONFIG = __import__("robotsix_mill.config", fromlist=["RepoConfig"]).RepoConfig(
    repo_id="other",
    board_id="other-board",
    langfuse_project_name="other-project",
    langfuse_public_key="pk-other",
    langfuse_secret_key="sk-other",
)


def test_force_traces_to_mill_calls_ensure_tracing(monkeypatch):
    """Entering force_traces_to_mill must call _ensure_tracing with the
    passed repo_config."""
    calls: list = []

    def fake_ensure(repo_config=None):
        calls.append(repo_config)

    monkeypatch.setattr(tracing, "_ensure_tracing", fake_ensure)
    with tracing.force_traces_to_mill(MILL_CONFIG):
        pass
    assert calls == [MILL_CONFIG]


def test_force_traces_to_mill_sets_current_pk_inside_block():
    """While inside the with block, _current_pk.get() must return the
    mill config's langfuse_public_key."""
    assert tracing._current_pk.get() is None  # precondition
    with tracing.force_traces_to_mill(MILL_CONFIG):
        assert tracing._current_pk.get() == "pk-mill"
    assert tracing._current_pk.get() is None  # restored


def test_force_traces_to_mill_restores_current_pk_on_normal_exit():
    """After the with block exits normally, _current_pk must be restored
    to its pre-entry value."""
    token = tracing._current_pk.set("pk-before")
    try:
        with tracing.force_traces_to_mill(MILL_CONFIG):
            assert tracing._current_pk.get() == "pk-mill"
        assert tracing._current_pk.get() == "pk-before"
    finally:
        tracing._current_pk.reset(token)


def test_force_traces_to_mill_restores_current_pk_on_exception():
    """If an exception is raised inside the with block, _current_pk must
    still be restored to its pre-entry value."""
    token = tracing._current_pk.set("pk-before")
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with tracing.force_traces_to_mill(MILL_CONFIG):
                assert tracing._current_pk.get() == "pk-mill"
                raise RuntimeError("boom")
        assert tracing._current_pk.get() == "pk-before"
    finally:
        tracing._current_pk.reset(token)


def test_force_traces_to_mill_nesting_is_safe():
    """Nesting force_traces_to_mill inside a per-repo context must
    restore the outer _current_pk on exit."""
    # Simulate an outer per-repo context (like start_ticket_root_span)
    token = tracing._current_pk.set("pk-other")
    try:
        with tracing.force_traces_to_mill(MILL_CONFIG):
            assert tracing._current_pk.get() == "pk-mill"
        # After exiting force_traces_to_mill, outer value restored
        assert tracing._current_pk.get() == "pk-other"
    finally:
        tracing._current_pk.reset(token)


def test_force_traces_to_mill_spans_carry_mill_public_key():
    """Integration test: spans created inside force_traces_to_mill() must
    carry langfuse.public_key from the mill config, verified via a
    minimal SpanProcessor on a real OTel TracerProvider."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry import trace as otel_trace

    # Reset OTel's "already set" guard.
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False

    captured: list[dict] = []

    class _StampAndCapture(SpanProcessor):
        """Stamp langfuse.public_key from the contextvar AND capture the
        resulting attributes — all in a single on_start so ordering
        between separate processors doesn't matter."""

        def on_start(self, span, parent_context=None):
            pk = tracing._current_pk.get()
            if pk:
                span.set_attribute("langfuse.public_key", pk)
            attrs = dict(span.attributes or {})
            captured.append({"name": span.name, "attrs": attrs})

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    provider = TracerProvider()
    provider.add_span_processor(_StampAndCapture())
    tracer = provider.get_tracer("test-tracer")

    with tracing.force_traces_to_mill(MILL_CONFIG):
        with tracer.start_as_current_span("meta-span"):
            with tracer.start_as_current_span("child-span"):
                pass

    assert len(captured) >= 2, f"Expected at least 2 spans, got {len(captured)}"
    for span_data in captured:
        assert span_data["attrs"].get("langfuse.public_key") == "pk-mill", (
            f"Span {span_data['name']} missing or wrong langfuse.public_key: "
            f"{span_data['attrs']}"
        )


# ---------------------------------------------------------------------------
# langfuse_trace_url tests
# ---------------------------------------------------------------------------


def test_langfuse_trace_url_with_repo_config(repo_config):
    """langfuse_trace_url builds the correct URL from a RepoConfig."""
    url = tracing.langfuse_trace_url("trace-abc123", repo_config=repo_config)
    assert url == (
        "https://cloud.langfuse.com/project/test-project/traces/trace-abc123"
    )


def test_langfuse_trace_url_repo_config_custom_base(repo_config):
    """langfuse_trace_url honours a custom base_url in RepoConfig."""
    repo_config.langfuse_base_url = "https://selfhosted.lf.example.com"
    url = tracing.langfuse_trace_url("trace-xyz", repo_config=repo_config)
    assert url == (
        "https://selfhosted.lf.example.com/project/test-project/traces/trace-xyz"
    )


def test_langfuse_trace_url_empty_base_url_fallback(repo_config):
    """langfuse_trace_url falls back to cloud.langfuse.com when
    repo_config.langfuse_base_url is empty."""
    repo_config.langfuse_base_url = ""
    url = tracing.langfuse_trace_url("trace-1", repo_config=repo_config)
    assert url == (
        "https://cloud.langfuse.com/project/test-project/traces/trace-1"
    )


def test_langfuse_trace_url_secrets_fallback(secrets_set):
    """langfuse_trace_url uses Secrets when no RepoConfig is given."""
    secrets_set(
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_project_name="secrets-project",
    )
    url = tracing.langfuse_trace_url("trace-456")
    assert url == (
        "https://cloud.langfuse.com/project/secrets-project/traces/trace-456"
    )


def test_langfuse_trace_url_secrets_project_name_preferred(secrets_set):
    """secrets fallback prefers langfuse_project_name over langfuse_project_id."""
    secrets_set(
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_project_name="name-project",
        langfuse_project_id="id-project",
    )
    url = tracing.langfuse_trace_url("trace-789")
    assert url == (
        "https://cloud.langfuse.com/project/name-project/traces/trace-789"
    )


def test_langfuse_trace_url_secrets_project_id_fallback(secrets_set):
    """secrets fallback uses langfuse_project_id when langfuse_project_name is absent."""
    secrets_set(
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_project_id="legacy-project-id",
    )
    url = tracing.langfuse_trace_url("trace-legacy")
    assert url == (
        "https://cloud.langfuse.com/project/legacy-project-id/traces/trace-legacy"
    )


def test_langfuse_trace_url_none_when_base_missing():
    """langfuse_trace_url returns None when no base URL is configured."""
    # No secrets set — base_url will be None, and the fallback
    # only applies via the "or" pattern, so with no RepoConfig
    # and empty secrets it should return None.
    url = tracing.langfuse_trace_url("trace-nope")
    assert url is None


def test_langfuse_trace_url_none_when_project_missing(secrets_set):
    """langfuse_trace_url returns None when a base URL is set but
    no project identifier is configured."""
    secrets_set(langfuse_base_url="https://cloud.langfuse.com")
    url = tracing.langfuse_trace_url("trace-missing-project")
    assert url is None


def test_langfuse_trace_url_trailing_slash_stripped(repo_config):
    """langfuse_trace_url strips trailing slashes from the base URL."""
    repo_config.langfuse_base_url = "https://cloud.langfuse.com/"
    url = tracing.langfuse_trace_url("trace-slash", repo_config=repo_config)
    assert url == (
        "https://cloud.langfuse.com/project/test-project/traces/trace-slash"
    )

"""Tests for the tracing module.

Mill delegates the OTLP→Langfuse provider/exporter/instrumentation and
the session/project context to ``robotsix_llmio.core.tracing``; these
tests verify the delegation (not the deleted internals) plus the
mill-specific surface that stays: the export-failure registry, the
``RepoConfig``-aware URL builder, ``make_session_id``, the
``_tracing_enabled`` matrix, and the shutdown signal handlers.
"""

import contextlib
import json
import os
from datetime import UTC
from datetime import datetime as _real_datetime

import pytest

from robotsix_mill.config import RepoConfig, _reset_secrets
from robotsix_mill.runtime import tracing


@pytest.fixture(autouse=True)
def _clear_env():
    """Reset the module-level state so tests are independent."""
    _reset_secrets()
    tracing._provider_ready = False
    tracing._registered_keys.clear()
    tracing._shutdown_requested = False
    tracing.clear_export_failures()


# --- small helpers for the delegation tests ----------------------------


@contextlib.contextmanager
def _record_cm(calls, kind, value):
    """A recording context manager standing in for an llmio
    ``langfuse_session`` / ``langfuse_project`` block."""
    calls.append((kind, value))
    yield


class _FakeSpan:
    def is_recording(self):
        return True

    def set_attribute(self, k, v):
        pass


class _FakeTracer:
    @contextlib.contextmanager
    def start_as_current_span(self, name, attributes=None):
        yield _FakeSpan()


def _llmio():
    import robotsix_llmio.core.tracing as _t

    return _t


# --- no-op behaviour when tracing is disabled --------------------------


def test_ensure_tracing_disabled_does_not_delegate(monkeypatch):
    """_ensure_tracing with no creds must NOT call llmio and must leave
    readiness False."""
    called = []
    monkeypatch.setattr(
        _llmio(), "setup_langfuse_tracing", lambda **kw: called.append(kw) or True
    )
    tracing._ensure_tracing()
    assert called == []
    assert tracing._provider_ready is False


def test_ensure_tracing_skips_repo_without_creds(monkeypatch):
    """A RepoConfig without langfuse creds is skipped silently."""
    called = []
    monkeypatch.setattr(
        _llmio(), "setup_langfuse_tracing", lambda **kw: called.append(kw) or True
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="",
        langfuse_secret_key="",
    )
    tracing._ensure_tracing(rc)
    assert called == []
    assert tracing._provider_ready is False


def test_flush_tracing_noop_when_not_ready(monkeypatch):
    """flush_tracing is a no-op (no llmio call) when tracing is off."""
    called = []
    monkeypatch.setattr(
        _llmio(), "flush_tracing", lambda timeout_millis=None: called.append("x")
    )
    tracing._provider_ready = False
    tracing.flush_tracing()  # no-op, no error
    assert called == []


def test_start_ticket_root_span_noop_when_disabled():
    """start_ticket_root_span yields a no-op handle when tracing is off."""
    tracing._provider_ready = False
    with tracing.start_ticket_root_span("test-ticket-id", "test") as root:
        assert root.trace_id is None
        root.set_input("x")  # accepted, discarded
        root.set_output("y")


def test_trace_stage_noop_when_disabled():
    """trace_stage must yield without error when tracing is off."""
    tracing._provider_ready = False
    with tracing.trace_stage("refine"):
        assert True  # body executed


# --- _ensure_tracing delegation ----------------------------------------


def test_ensure_tracing_delegates_to_llmio(monkeypatch):
    """_ensure_tracing calls llmio.setup_langfuse_tracing with the
    resolved per-repo creds, service_name='robotsix-mill', and an
    on_export_result adapter."""
    captured = {}
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (captured.update(kw), True)[1],
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="proj",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
        langfuse_base_url="https://lf.example.com",
    )
    tracing._ensure_tracing(rc)
    assert captured["public_key"] == "pk-a"
    assert captured["secret_key"] == "sk-a"
    assert captured["base_url"] == "https://lf.example.com"
    assert captured["project_id"] == "proj"
    assert captured["service_name"] == "robotsix-mill"
    assert callable(captured["on_export_result"])
    assert tracing._provider_ready is True
    assert "pk-a" in tracing._registered_keys


def test_ensure_tracing_falls_back_to_global_langfuse(monkeypatch):
    """With no repo_config, creds come from the canonical langfuse block
    and project_id is None."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://global.example.com",
            "pk-g",
            "sk-g",
            "cuid-123",
            "robotsix-mill",
        ),
    )
    captured = {}
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (captured.update(kw), True)[1],
    )
    tracing._ensure_tracing()
    assert captured["public_key"] == "pk-g"
    assert captured["secret_key"] == "sk-g"
    assert captured["base_url"] == "https://global.example.com"
    assert captured["project_id"] is None
    assert captured["service_name"] == "robotsix-mill"


def test_ensure_tracing_idempotent_per_key(monkeypatch):
    """A second call for the same public key does not re-register."""
    calls = []
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (calls.append(kw["public_key"]), True)[1],
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="proj",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    tracing._ensure_tracing(rc)
    tracing._ensure_tracing(rc)
    assert calls == ["pk-a"]


def test_ensure_tracing_multi_tenant_registers_each_key(monkeypatch):
    """Two repos register distinctly so traces route per-repo — repo A's
    traces are never attributed to repo B's project."""
    calls = []
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (calls.append(kw["public_key"]), True)[1],
    )
    repo_a = RepoConfig(
        repo_id="a",
        board_id="ba",
        langfuse_project_name="proj-a",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    repo_b = RepoConfig(
        repo_id="b",
        board_id="bb",
        langfuse_project_name="proj-b",
        langfuse_public_key="pk-b",
        langfuse_secret_key="sk-b",
    )
    tracing._ensure_tracing(repo_a)
    tracing._ensure_tracing(repo_b)
    assert calls == ["pk-a", "pk-b"]
    assert tracing._registered_keys == {"pk-a", "pk-b"}


# --- export-result adapter bridges llmio -> mill registry --------------


def test_export_adapter_records_failure_and_clears_on_success(monkeypatch):
    """The on_export_result adapter records a failure entry under the
    project label on ok=False and clears it on ok=True."""
    captured = {}
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (captured.update(kw), True)[1],
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="proj-a",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    tracing._ensure_tracing(rc)
    hook = captured["on_export_result"]

    hook("pk-a", False, "boom")
    failures = tracing.get_export_failures()
    assert len(failures) == 1
    assert failures[0]["project"] == "proj-a"
    assert "boom" in failures[0]["error"]

    hook("pk-a", True, None)
    assert tracing.get_export_failures() == []


def test_export_adapter_label_falls_back_to_public_key(monkeypatch):
    """When the repo has no project name, the failure label is the
    public key."""
    captured = {}
    monkeypatch.setattr(
        _llmio(),
        "setup_langfuse_tracing",
        lambda **kw: (captured.update(kw), True)[1],
    )
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-only",
            "sk-only",
            "",
            "pk-only",
        ),
    )
    tracing._ensure_tracing()
    captured["on_export_result"]("pk-only", False, "nope")
    failures = tracing.get_export_failures()
    assert failures
    assert failures[0]["project"] == "pk-only"


# --- start_ticket_root_span / force_traces_to_mill delegation ----------


def test_start_ticket_root_span_enters_llmio_contexts(monkeypatch):
    """start_ticket_root_span enters llmio.langfuse_session(ticket_id)
    and llmio.langfuse_project(pk) around the mill span."""
    calls = []
    monkeypatch.setattr(tracing, "_ensure_tracing", lambda repo_config=None: None)
    tracing._provider_ready = True
    monkeypatch.setattr(
        _llmio(), "langfuse_session", lambda sid: _record_cm(calls, "session", sid)
    )
    monkeypatch.setattr(
        _llmio(), "langfuse_project", lambda pk: _record_cm(calls, "project", pk)
    )
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: _FakeTracer())

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="proj",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    with tracing.start_ticket_root_span("ticket-1", "refine", repo_config=rc) as root:
        assert root is not None
    # The Langfuse session is repo-qualified for a legible single-project view.
    assert ("session", "r · ticket-1") in calls
    assert ("project", "pk-a") in calls


def test_start_ticket_root_span_pk_from_global_langfuse(monkeypatch):
    """When no repo_config pk is given, the project pk comes from the
    canonical langfuse block."""
    calls = []
    monkeypatch.setattr(tracing, "_ensure_tracing", lambda repo_config=None: None)
    tracing._provider_ready = True
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-global",
            "sk-global",
            "",
            "pk-global",
        ),
    )
    monkeypatch.setattr(
        _llmio(), "langfuse_session", lambda sid: _record_cm(calls, "session", sid)
    )
    monkeypatch.setattr(
        _llmio(), "langfuse_project", lambda pk: _record_cm(calls, "project", pk)
    )
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: _FakeTracer())

    with tracing.start_ticket_root_span("ticket-9", "implement"):
        pass
    assert ("session", "ticket-9") in calls
    assert ("project", "pk-global") in calls


def test_force_traces_to_mill_enters_llmio_project(monkeypatch):
    """force_traces_to_mill calls _ensure_tracing then enters
    llmio.langfuse_project with the config's public key."""
    calls = []
    monkeypatch.setattr(
        tracing,
        "_ensure_tracing",
        lambda repo_config=None: calls.append(("ensure", repo_config)),
    )
    monkeypatch.setattr(
        _llmio(), "langfuse_project", lambda pk: _record_cm(calls, "project", pk)
    )
    with tracing.force_traces_to_mill(MILL_CONFIG):
        pass
    assert ("ensure", MILL_CONFIG) in calls
    assert ("project", "pk-mill") in calls


# --- current_session / flush_tracing delegation ------------------------


def test_current_session_delegates_to_llmio(monkeypatch):
    """current_session() returns llmio.current_session()'s value."""
    monkeypatch.setattr(_llmio(), "current_session", lambda: "sess-xyz")
    assert tracing.current_session() == "sess-xyz"


def test_flush_tracing_delegates_with_timeout_millis(monkeypatch):
    """flush_tracing(timeout=5000) forwards timeout_millis=5000 to
    llmio.flush_tracing."""
    tracing._provider_ready = True
    captured = []
    monkeypatch.setattr(
        _llmio(),
        "flush_tracing",
        lambda timeout_millis=None: captured.append(timeout_millis),
    )
    tracing.flush_tracing(timeout=5000)
    assert captured == [5000]


def test_flush_tracing_default_timeout():
    """flush_tracing() default timeout is 10_000 ms."""
    import inspect

    sig = inspect.signature(tracing.flush_tracing)
    assert sig.parameters["timeout"].default == 10_000


# --- _tracing_enabled matrix -------------------------------------------


def test_tracing_enabled_no_env():
    """_tracing_enabled returns False when Secrets has no langfuse keys."""
    assert tracing._tracing_enabled() is False


def test_tracing_enabled_with_vars(monkeypatch):
    """_tracing_enabled returns True when both keys are set in the
    canonical langfuse block."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-test",
            "sk-test",
            "",
            "test-project",
        ),
    )
    assert tracing._tracing_enabled() is True


def test_tracing_enabled_missing_secret(monkeypatch):
    """_tracing_enabled returns False when only public key is set."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-test",
            "",
            "",
            "test-project",
        ),
    )
    assert tracing._tracing_enabled() is False


def test_tracing_enabled_per_repo(repo_config):
    """_tracing_enabled honours per-repo creds over the global Secrets."""
    assert tracing._tracing_enabled(repo_config) is True


# --- no heavy imports at module level ----------------------------------


def test_no_otel_imports_at_module_level():
    """Importing runtime.tracing must NOT pull opentelemetry / langfuse /
    pydantic_ai.agent / robotsix_llmio (they are lazy, inside functions).
    Checked in a clean subprocess — asserting the session-global
    sys.modules would be polluted by other tests that import them."""
    import subprocess
    import sys

    code = (
        "import sys, robotsix_mill.runtime.tracing as _t; "
        "bad=[m for m in ('opentelemetry','langfuse','pydantic_ai.agent',"
        "'robotsix_llmio') if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0, f"eagerly imported: {r.stdout.strip()}"


# --- make_session_id ---------------------------------------------------


class _FakeDatetime:
    @staticmethod
    def now(tz=None):
        return _real_datetime(2026, 5, 21, 14, 30, 25, tzinfo=UTC)


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


# --- qualify_session / current_ticket_id (single-project sessions) ------


def _rc(repo_id):
    return RepoConfig(
        repo_id=repo_id,
        board_id=repo_id,
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )


def test_qualify_session_prefixes_repo():
    assert (
        tracing.qualify_session("20260615T-x-ffea", _rc("robotsix-llmio"))
        == "robotsix-llmio · 20260615T-x-ffea"
    )


def test_qualify_session_idempotent_and_none_repo():
    rc = _rc("robotsix-llmio")
    once = tracing.qualify_session("t-1", rc)
    assert tracing.qualify_session(once, rc) == once  # not double-prefixed
    assert tracing.qualify_session("bare", None) == "bare"  # legacy path


def test_make_session_id_repo_qualified():
    sid = tracing.make_session_id("audit", _rc("robotsix-board"))
    assert sid.startswith("robotsix-board · audit-")


def test_current_ticket_id_strips_repo_prefix(monkeypatch):
    monkeypatch.setattr(
        tracing, "current_session", lambda: "robotsix-llmio · 20260615T-x-ffea"
    )
    assert tracing.current_ticket_id() == "20260615T-x-ffea"


def test_current_ticket_id_passthrough_when_unqualified(monkeypatch):
    monkeypatch.setattr(tracing, "current_session", lambda: "20260615T-x-ffea")
    assert tracing.current_ticket_id() == "20260615T-x-ffea"
    monkeypatch.setattr(tracing, "current_session", lambda: None)
    assert tracing.current_ticket_id() is None


# --- install_signal_handlers & flush_tracing timeout -------------------


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
    with contextlib.suppress(SystemExit):
        os.kill(os.getpid(), signal.SIGTERM)
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
    with contextlib.suppress(SystemExit):
        os.kill(os.getpid(), signal.SIGTERM)
    assert len(calls) == 1

    # Second signal — handler sees _shutdown_requested is True, returns.
    os.kill(os.getpid(), signal.SIGTERM)
    assert len(calls) == 1  # still only one flush


# --- force_traces_to_mill config ---------------------------------------


MILL_CONFIG = RepoConfig(
    repo_id="mill",
    board_id="mill-board",
    langfuse_project_name="robotsix-mill",
    langfuse_public_key="pk-mill",
    langfuse_secret_key="sk-mill",
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


def test_langfuse_trace_url_repo_config_project_id_preferred(repo_config):
    """langfuse_trace_url uses RepoConfig.langfuse_project_id over
    langfuse_project_name when both are set."""
    repo_config.langfuse_project_id = "cuid-abc123"
    repo_config.langfuse_project_name = "My Project"
    url = tracing.langfuse_trace_url("trace-xyz", repo_config=repo_config)
    assert url == ("https://cloud.langfuse.com/project/cuid-abc123/traces/trace-xyz")


def test_langfuse_trace_url_repo_config_name_fallback(repo_config):
    """langfuse_trace_url falls back to langfuse_project_name when
    langfuse_project_id is empty."""
    repo_config.langfuse_project_id = ""
    repo_config.langfuse_project_name = "name-only-project"
    url = tracing.langfuse_trace_url("trace-1", repo_config=repo_config)
    assert url == (
        "https://cloud.langfuse.com/project/name-only-project/traces/trace-1"
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
    assert url == ("https://cloud.langfuse.com/project/test-project/traces/trace-1")


def test_langfuse_trace_url_global_fallback(monkeypatch):
    """langfuse_trace_url uses the langfuse block when no RepoConfig is given."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-x",
            "sk-x",
            "",
            "secrets-project",
        ),
    )
    url = tracing.langfuse_trace_url("trace-456")
    assert url == (
        "https://cloud.langfuse.com/project/secrets-project/traces/trace-456"
    )


def test_langfuse_trace_url_global_project_id_preferred(monkeypatch):
    """Global fallback prefers project_id over project_name."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-x",
            "sk-x",
            "id-project",
            "name-project",
        ),
    )
    url = tracing.langfuse_trace_url("trace-789")
    assert url == ("https://cloud.langfuse.com/project/id-project/traces/trace-789")


def test_langfuse_trace_url_global_project_id_fallback(monkeypatch):
    """Global fallback uses project_id when project_name is absent."""
    monkeypatch.setattr(
        tracing,
        "_resolve_mill_langfuse",
        lambda: (
            "https://cloud.langfuse.com",
            "pk-x",
            "sk-x",
            "legacy-project-id",
            "",
        ),
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


def test_langfuse_trace_url_none_when_project_missing():
    """langfuse_trace_url returns None when no langfuse block is configured."""
    url = tracing.langfuse_trace_url("trace-missing-project")
    assert url is None


def test_langfuse_trace_url_trailing_slash_stripped(repo_config):
    """langfuse_trace_url strips trailing slashes from the base URL."""
    repo_config.langfuse_base_url = "https://cloud.langfuse.com/"
    url = tracing.langfuse_trace_url("trace-slash", repo_config=repo_config)
    assert url == ("https://cloud.langfuse.com/project/test-project/traces/trace-slash")


# ===========================================================================
# set_current_span_attribute tests
# ===========================================================================


def test_set_current_span_attribute_noop_when_no_span(monkeypatch):
    """set_current_span_attribute is a no-op when no span is recording."""
    from robotsix_mill.runtime.tracing import set_current_span_attribute

    # No active span — should not raise.
    set_current_span_attribute("test.key", "test_value")


def test_set_current_span_attribute_sets_on_recording_span(monkeypatch):
    """set_current_span_attribute sets the attribute on a recording span."""
    from robotsix_mill.runtime.tracing import set_current_span_attribute

    captured: dict = {}

    class FakeSpan:
        def is_recording(self):
            return True

        def set_attribute(self, key, value):
            captured[key] = value

    def fake_get_current_span():
        return FakeSpan()

    monkeypatch.setattr("opentelemetry.trace.get_current_span", fake_get_current_span)

    set_current_span_attribute("refine.model_level", 1)
    assert captured.get("refine.model_level") == 1


def test_set_current_span_attribute_skips_non_recording_span(monkeypatch):
    """set_current_span_attribute does nothing on a non-recording span."""
    from robotsix_mill.runtime.tracing import set_current_span_attribute

    captured: dict = {}

    class FakeSpan:
        def is_recording(self):
            return False

        def set_attribute(self, key, value):
            captured[key] = value

    def fake_get_current_span():
        return FakeSpan()

    monkeypatch.setattr("opentelemetry.trace.get_current_span", fake_get_current_span)

    set_current_span_attribute("refine.model_level", 1)
    assert "refine.model_level" not in captured


def test_set_current_span_attribute_noop_on_import_error(monkeypatch):
    """set_current_span_attribute is a no-op when opentelemetry is not installed."""
    # Simulate ImportError by removing opentelemetry from sys.modules
    # temporarily — but this is fragile.  Instead, test that the function
    # handles ImportError gracefully.
    import builtins

    import robotsix_mill.runtime.tracing as tracing_mod

    orig_import = builtins.__import__

    def fail_opentelemetry(name, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("No module named 'opentelemetry'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_opentelemetry)
    # Clear the cached module if present.
    import sys

    sys.modules.pop("opentelemetry", None)
    sys.modules.pop("opentelemetry.trace", None)

    # Should not raise.
    tracing_mod.set_current_span_attribute("test.key", "value")


# --- stage×model instrumentation context --------------------------------


def test_start_ticket_root_span_sets_instrumentation_context(monkeypatch):
    """The root span sets current_stage_name / current_board_id for the
    duration of the block and clears them afterwards."""
    monkeypatch.setattr(tracing, "_ensure_tracing", lambda repo_config=None: None)
    tracing._provider_ready = True
    monkeypatch.setattr(
        _llmio(), "langfuse_session", lambda sid: _record_cm([], "session", sid)
    )
    monkeypatch.setattr(
        _llmio(), "langfuse_project", lambda pk: _record_cm([], "project", pk)
    )
    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_tracer", lambda name: _FakeTracer())

    rc = RepoConfig(
        repo_id="r",
        board_id="board-b",
        langfuse_project_name="proj",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    assert tracing.current_stage_name() == ""
    assert tracing.current_board_id() == ""
    with tracing.start_ticket_root_span("ticket-1", "implement", repo_config=rc):
        assert tracing.current_stage_name() == "implement"
        assert tracing.current_board_id() == "board-b"
    assert tracing.current_stage_name() == ""
    assert tracing.current_board_id() == ""


def test_record_step_usage_embeds_stage_model_metric(monkeypatch):
    """record_step_usage emits a compact JSON metric carrying stage_name,
    model_name, token counts, timestamp, ticket and board id — no prompt
    text."""
    captured: dict = {}

    class FakeSpan:
        def is_recording(self):
            return True

        def set_attribute(self, key, value):
            captured[key] = value

    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: FakeSpan())
    monkeypatch.setattr(
        _llmio(), "current_session", lambda: "r · 20260814T123456Z-my-ticket-ab12"
    )
    stage_token = tracing._current_stage_name.set("implement")
    board_token = tracing._current_board_id.set("board-b")
    try:
        tracing.record_step_usage(
            request_count=3,
            model_name="openrouter/deepseek/deepseek-v4-pro",
            input_tokens=97_000,
            output_tokens=1_200,
            cache_read_input_tokens=40_000,
            backend="openrouter",
        )
    finally:
        tracing._current_stage_name.reset(stage_token)
        tracing._current_board_id.reset(board_token)

    payload = captured["langfuse.observation.metadata.mill.step_usage"]
    metric = json.loads(payload)
    assert metric["stage_name"] == "implement"
    assert metric["model_name"] == "openrouter/deepseek/deepseek-v4-pro"
    assert metric["input_tokens"] == 97_000
    assert metric["output_tokens"] == 1_200
    assert metric["cache_read_input_tokens"] == 40_000
    assert metric["backend"] == "openrouter"
    assert metric["ticket_id"] == "20260814T123456Z-my-ticket-ab12"
    assert metric["board_id"] == "board-b"
    assert metric["timestamp"]  # ISO-8601, present on every record
    # The metric must be small and prompt-free.
    assert len(payload) < 512
    assert "prompt" not in payload.lower()


def test_record_step_usage_aggregates_deepseek_by_stage(monkeypatch):
    """Sample query: grouping emitted records by stage×model recovers
    deepseek-v4-pro input-token totals per stage (the 24h-window query
    just adds a timestamp filter on top of this grouping)."""
    records: list[dict] = []

    class FakeSpan:
        def is_recording(self):
            return True

        def set_attribute(self, key, value):
            if key == "langfuse.observation.metadata.mill.step_usage":
                records.append(json.loads(value))

    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: FakeSpan())
    monkeypatch.setattr(
        _llmio(), "current_session", lambda: "r · 20260814T123456Z-sample-ab12"
    )

    def emit(stage: str, model: str, tokens: int) -> None:
        stage_token = tracing._current_stage_name.set(stage)
        try:
            tracing.record_step_usage(
                request_count=1,
                model_name=model,
                input_tokens=tokens,
                output_tokens=50,
            )
        finally:
            tracing._current_stage_name.reset(stage_token)

    # deepseek-v4-pro volume across two stages, plus a v4-flash call to
    # prove the model dimension splits too.
    emit("implement", "openrouter/deepseek/deepseek-v4-pro", 100_000)
    emit("implement", "openrouter/deepseek/deepseek-v4-pro", 60_000)
    emit("review", "openrouter/deepseek/deepseek-v4-pro", 40_000)
    emit("implement", "openrouter/deepseek/deepseek-v4-flash", 30_000)

    totals: dict[tuple[str, str], int] = {}
    counts: dict[tuple[str, str], int] = {}
    for rec in records:
        key = (rec["stage_name"], rec["model_name"])
        totals[key] = totals.get(key, 0) + rec["input_tokens"]
        counts[key] = counts.get(key, 0) + 1

    pro = "openrouter/deepseek/deepseek-v4-pro"
    flash = "openrouter/deepseek/deepseek-v4-flash"
    assert totals[("implement", pro)] == 160_000
    assert counts[("implement", pro)] == 2
    assert totals[("review", pro)] == 40_000
    assert totals[("implement", flash)] == 30_000
    # Per-call values are preserved so mean/p50/p95/max are derivable.
    per_call = [r["input_tokens"] for r in records if r["model_name"] == pro]
    assert max(per_call) == 100_000
    assert sum(per_call) / len(per_call) == 200_000 / 3


def test_record_step_usage_mirrors_to_local_store(monkeypatch):
    """record_step_usage also mirrors the compact dict into the local
    SQLite store so /metrics/step-usage can aggregate without Langfuse."""
    from robotsix_mill.runtime import step_usage_store

    mirrored: list[dict] = []
    monkeypatch.setattr(tracing, "set_current_span_attribute", lambda *a, **k: None)
    monkeypatch.setattr(
        step_usage_store, "record", lambda data, **kw: mirrored.append(data)
    )

    tracing.record_step_usage(
        request_count=1,
        model_name="deepseek-v4-pro",
        input_tokens=123,
        output_tokens=45,
    )

    assert len(mirrored) == 1
    assert mirrored[0]["model_name"] == "deepseek-v4-pro"
    assert mirrored[0]["input_tokens"] == 123
    assert mirrored[0]["output_tokens"] == 45
    assert "timestamp" in mirrored[0]

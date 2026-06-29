"""Unit tests for langfuse.client functions not covered elsewhere:
_langfuse_api_get, session_cost_cached, and session_total_cost edge cases.
"""

import base64
import json

import httpx
import pytest

from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.langfuse.client import (
    _cost_cache,
    _langfuse_api_get,
    session_cost_cached,
    session_total_cost,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _langfuse_settings(**overrides):
    """Return a Settings with tracing enabled via Secrets.

    Constructs a Settings and sets the Secrets singleton so that
    tracing_enabled and the Langfuse API helpers find the credentials.

    *overrides* keys may include ``langfuse_base_url``,
    ``langfuse_public_key``, and ``langfuse_secret_key`` to customize
    the Langfuse credentials (the old ``LANGFUSE_*`` env-var-style
    keys are mapped automatically).
    """

    # Map old LANGFUSE_* keys to Secrets field names.
    secrets_kwargs: dict = {}
    for env_key, field_name in [
        ("LANGFUSE_BASE_URL", "langfuse_base_url"),
        ("LANGFUSE_PUBLIC_KEY", "langfuse_public_key"),
        ("LANGFUSE_SECRET_KEY", "langfuse_secret_key"),
    ]:
        if env_key in overrides:
            secrets_kwargs[field_name] = overrides.pop(env_key)

    # Populate Secrets so get_secrets() returns matching values.
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(
        langfuse_base_url=secrets_kwargs.get(
            "langfuse_base_url", "https://lf.example.com"
        ),
        langfuse_public_key=secrets_kwargs.get("langfuse_public_key", "pk-test"),
        langfuse_secret_key=secrets_kwargs.get("langfuse_secret_key", "sk-test"),
    )
    return Settings(**overrides)


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class _FakeClient:
    """A controllable httpx.Client stand-in that captures get() calls."""

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, *, params, headers):
        self._last_call = (url, params, headers)
        return self._next_response


# ---------------------------------------------------------------------------
# _langfuse_api_get — direct unit tests (mock httpx.Client)
# ---------------------------------------------------------------------------


def test_langfuse_api_get_returns_none_when_tracing_disabled(settings):
    """When tracing_enabled is False, _langfuse_api_get returns None
    without making any HTTP call."""
    assert settings.tracing_enabled is False
    result = _langfuse_api_get(settings, "/api/public/traces")
    assert result is None


def test_langfuse_api_get_returns_json_on_200(monkeypatch):
    """Mock httpx.Client to return status 200 + a JSON dict; assert the
    same dict is returned."""
    response_data = {"data": [{"id": "trace-1", "totalCost": 0.05}]}

    client = _FakeClient()
    client._next_response = _FakeResponse(200, response_data)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client)

    s = _langfuse_settings()
    result = _langfuse_api_get(s, "/api/public/traces", params={"sessionId": "s1"})

    assert result == response_data
    assert client._last_call[0].endswith("/api/public/traces")
    assert client._last_call[1] == {"sessionId": "s1"}


@pytest.mark.parametrize("status_code", [404, 500, 503])
def test_langfuse_api_get_returns_none_on_non_200(status_code, monkeypatch):
    """Non-200 status codes cause _langfuse_api_get to return None."""
    client = _FakeClient()
    client._next_response = _FakeResponse(status_code, {"error": "fail"})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client)

    s = _langfuse_settings()
    result = _langfuse_api_get(s, "/api/public/traces")
    assert result is None


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_langfuse_api_get_returns_none_on_network_error(exc, monkeypatch):
    """Network errors (ConnectError, ReadTimeout) are caught → None
    (no exception propagates)."""

    class _ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            raise exc

    monkeypatch.setattr(httpx, "Client", _ErrorClient)

    s = _langfuse_settings()
    result = _langfuse_api_get(s, "/api/public/traces")
    assert result is None


def test_langfuse_api_get_returns_none_on_json_decode_error(monkeypatch):
    """If response.json() raises JSONDecodeError, return None (the broad
    except Exception catches it)."""

    class _BadJsonResponse:
        status_code = 200

        def json(self):
            raise json.JSONDecodeError("bad json", "", 0)

    class _BadJsonClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return _BadJsonResponse()

    monkeypatch.setattr(httpx, "Client", _BadJsonClient)

    s = _langfuse_settings()
    result = _langfuse_api_get(s, "/api/public/traces")
    assert result is None


def test_langfuse_api_get_constructs_correct_auth_header(monkeypatch):
    """Verify the Authorization header is ``Basic <base64(pk:sk)>``."""
    captured_headers = []

    class _CaptureClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            captured_headers.append(headers)
            return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(httpx, "Client", _CaptureClient)

    s = _langfuse_settings(
        LANGFUSE_PUBLIC_KEY="pk-mykey",
        LANGFUSE_SECRET_KEY="sk-secret",
    )
    result = _langfuse_api_get(s, "/api/public/traces")

    assert result == {"ok": True}
    assert len(captured_headers) == 1
    auth_header = captured_headers[0]["Authorization"]
    assert auth_header.startswith("Basic ")

    encoded = auth_header[len("Basic ") :]
    decoded = base64.b64decode(encoded).decode()
    assert decoded == "pk-mykey:sk-secret"


# ---------------------------------------------------------------------------
# session_cost_cached — cache-read tests
# ---------------------------------------------------------------------------


def test_session_cost_cached_returns_zero_when_cache_empty():
    """When _cost_cache has no entry, returns 0.0."""
    _cost_cache.clear()
    result = session_cost_cached("never-seen-id")
    assert result == 0.0


def test_session_cost_cached_returns_cached_value():
    """When _cost_cache has an entry, returns its cost value."""
    _cost_cache.clear()
    _cost_cache["test-session"] = (0.042, 1000.0)
    result = session_cost_cached("test-session")
    assert result == 0.042


def test_session_cost_cached_never_hits_network(monkeypatch):
    """session_cost_cached only reads from the in-memory cache; prove
    session_total_cost is never called."""

    def _fail(*args, **kwargs):
        raise AssertionError("session_total_cost must not be called")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_total_cost",
        _fail,
    )

    # Cache hit — must not call session_total_cost
    _cost_cache.clear()
    _cost_cache["s1"] = (0.99, 9999.0)
    result = session_cost_cached("s1")
    assert result == 0.99

    # Cache miss — still must not call session_total_cost
    _cost_cache.clear()
    result = session_cost_cached("unknown")
    assert result == 0.0


# ---------------------------------------------------------------------------
# session_total_cost — malformed-cost edge cases
# ---------------------------------------------------------------------------


def _fake_api_response(traces):
    """Return a callable that mimics _langfuse_api_get for given traces."""
    return lambda s, path, params=None, repo_config=None: {"data": traces}


def test_session_total_cost_handles_missing_totalcost_key(settings, monkeypatch):
    """Traces without a 'totalCost' key → _num(None) → 0.0."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _fake_api_response(
            [
                {"id": "t1", "name": "ok", "totalCost": 0.10},
                {"id": "t2", "name": "missing-key"},
            ]
        ),
    )
    cost = session_total_cost(settings, "s")
    assert cost == 0.10  # missing-key trace contributes 0.0


def test_session_total_cost_handles_non_numeric_totalcost(settings, monkeypatch):
    """totalCost is a non-numeric string → _num catches ValueError → 0.0."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _fake_api_response(
            [
                {"id": "t1", "totalCost": "not-a-number"},
                {"id": "t2", "totalCost": 0.05},
            ]
        ),
    )
    cost = session_total_cost(settings, "s")
    assert cost == 0.05  # "not-a-number" contributes 0.0


def test_session_total_cost_handles_totalcost_typeerror(settings, monkeypatch):
    """totalCost is a list → float(list) raises TypeError → caught → 0.0."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _fake_api_response(
            [
                {"id": "t1", "totalCost": [1, 2, 3]},
                {"id": "t2", "totalCost": 0.03},
            ]
        ),
    )
    cost = session_total_cost(settings, "s")
    assert cost == 0.03  # list contributes 0.0


# ---------------------------------------------------------------------------
# Multi-page aggregation (regression: old EXAMINE_CAP=500 silently
# discarded traces beyond page 5, under-counting cost by up to 3×).
# ---------------------------------------------------------------------------


def _multi_page_mock_client(pages: dict[int, dict]):
    """Return a mock httpx.Client that responds with *pages* keyed by
    page number (1-indexed). Each value is a dict with ``data`` and
    ``meta.totalPages``."""

    class _PagingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            page = params.get("page", 1)
            if page in pages:
                return _FakeResponse(200, pages[page])
            # Simulate pagination past the end — empty page
            return _FakeResponse(
                200, {"data": [], "meta": {"totalPages": max(pages.keys())}}
            )

    return _PagingClient


# ---------------------------------------------------------------------------
# Repo-qualified session id (regression for the #1395 consolidation:
# traces are stamped under "<repo> · <ticket>" but the cost/trace read
# path queried the bare ticket id and read $0 for every ticket).
# ---------------------------------------------------------------------------


from types import SimpleNamespace  # noqa: E402

from robotsix_mill.langfuse.client import session_cost  # noqa: E402


def _capturing_api_get(captured: list):
    """Fake _langfuse_api_get that records the sessionId param it was
    queried with and returns one $0.20 trace."""

    def _fn(s, path, params=None, repo_config=None):
        captured.append((params or {}).get("sessionId"))
        return {"data": [{"id": "t1", "totalCost": 0.20}]}

    return _fn


def test_session_total_cost_qualifies_session_id_with_repo(settings, monkeypatch):
    """When repo_config carries a repo_id, the Langfuse query uses the
    repo-qualified sessionId, not the bare ticket id."""
    captured: list = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _capturing_api_get(captured),
    )
    rc = SimpleNamespace(repo_id="robotsix-cost-monitor")
    cost = session_total_cost(settings, "ticket-123", repo_config=rc)
    assert cost == 0.20
    assert captured == ["robotsix-cost-monitor · ticket-123"]


def test_session_total_cost_bare_when_no_repo(settings, monkeypatch):
    """With no repo_config the bare id is used (legacy/single-repo)."""
    captured: list = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _capturing_api_get(captured),
    )
    session_total_cost(settings, "ticket-123")
    assert captured == ["ticket-123"]


def test_cost_cache_key_consistent_between_blocking_and_cached(settings, monkeypatch):
    """session_cost caches under the qualified key, and a subsequent
    session_cost_cached call with the same repo_config (but the bare id)
    reads that same entry — the bug was a key mismatch that always
    missed the cache on the polled /tickets list."""
    _cost_cache.clear()
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client._langfuse_api_get",
        _capturing_api_get([]),
    )
    rc = SimpleNamespace(repo_id="robotsix-cost-monitor")
    blocking = session_cost(settings, "ticket-123", repo_config=rc, force=True)
    cached = session_cost_cached("ticket-123", repo_config=rc)
    assert blocking == cached == 0.20
    # The cache is keyed by the qualified id, not the bare ticket id.
    assert "robotsix-cost-monitor · ticket-123" in _cost_cache
    assert session_cost_cached("ticket-123") == 0.0  # bare key misses


# ---------------------------------------------------------------------------
# trace_observation_summary
# ---------------------------------------------------------------------------


from robotsix_mill.langfuse.client import trace_observation_summary  # noqa: E402


def test_trace_observation_summary_empty():
    """Empty trace → all defaults."""
    s = trace_observation_summary({})
    assert s == {
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tool_calls": [],
        "error_count": 0,
        "warning_count": 0,
        "observation_count": 0,
    }


def test_trace_observation_summary_token_aggregation():
    """Token counts summed across GENERATION observations."""
    trace = {
        "model": "openai/gpt-4o",
        "observations": [
            {
                "name": "chat completion",
                "type": "GENERATION",
                "usage": {"input": 100, "output": 50},
                "level": "DEFAULT",
            },
            {
                "name": "chat completion",
                "type": "GENERATION",
                "usage": {"promptTokens": 200, "completionTokens": 75},
                "level": "DEFAULT",
            },
            {
                "name": "read_file",
                "type": "SPAN",
                "usage": None,
                "level": "DEFAULT",
            },
        ],
    }
    s = trace_observation_summary(trace)
    assert s["model"] == "openai/gpt-4o"
    assert s["input_tokens"] == 300  # 100 + 200
    assert s["output_tokens"] == 125  # 50 + 75
    assert s["total_tokens"] == 425
    assert s["observation_count"] == 3


def test_trace_observation_summary_tool_calls():
    """Non-chat SPAN observations are counted as tool calls."""
    trace = {
        "observations": [
            {"name": "chat completion", "type": "GENERATION", "level": "DEFAULT"},
            {"name": "read_file", "type": "SPAN", "level": "DEFAULT"},
            {"name": "read_file", "type": "SPAN", "level": "DEFAULT"},
            {"name": "run_command", "type": "SPAN", "level": "DEFAULT"},
            {"name": "explore run", "type": "SPAN", "level": "DEFAULT"},
            {"name": "chat final synthesis", "type": "GENERATION", "level": "DEFAULT"},
        ],
    }
    s = trace_observation_summary(trace)
    assert s["tool_calls"] == [
        {"name": "read_file", "count": 2},
        {"name": "run_command", "count": 1},
        {"name": "explore run", "count": 1},
    ]


def test_trace_observation_summary_error_warning_counts():
    """ERROR and WARNING levels are counted separately."""
    trace = {
        "observations": [
            {"name": "run_command", "type": "SPAN", "level": "ERROR"},
            {"name": "run_command", "type": "SPAN", "level": "ERROR"},
            {"name": "read_file", "type": "SPAN", "level": "WARNING"},
            {"name": "chat completion", "type": "GENERATION", "level": "DEFAULT"},
        ],
    }
    s = trace_observation_summary(trace)
    assert s["error_count"] == 2
    assert s["warning_count"] == 1


def test_trace_observation_summary_model_fallback():
    """When trace-level model is absent, fall back to first GENERATION model."""
    trace = {
        "observations": [
            {"name": "read_file", "type": "SPAN", "level": "DEFAULT"},
            {
                "name": "chat completion",
                "type": "GENERATION",
                "model": "openai/gpt-4o-mini",
                "level": "DEFAULT",
            },
        ],
    }
    s = trace_observation_summary(trace)
    assert s["model"] == "openai/gpt-4o-mini"

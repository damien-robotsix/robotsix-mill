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
# aggregate_cost_trend tests
# ---------------------------------------------------------------------------


def test_aggregate_cost_trend_disabled(settings):
    """Returns [] when tracing_enabled is False."""
    assert settings.tracing_enabled is False
    from robotsix_mill.langfuse.client import aggregate_cost_trend

    result = aggregate_cost_trend(settings, lookback_hours=24)
    assert result == []


def test_aggregate_cost_trend_api_error(settings, monkeypatch):
    """Returns [] when the Langfuse API returns an error."""
    from robotsix_mill.langfuse.client import aggregate_cost_trend

    # Need tracing enabled
    s = _langfuse_settings()

    class _ErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return _FakeResponse(500, {"error": "internal"})

    monkeypatch.setattr(httpx, "Client", _ErrorClient)

    result = aggregate_cost_trend(s, lookback_hours=24)
    assert result == []


def test_aggregate_cost_trend_sums_correctly(monkeypatch):
    """Traces in the same bucket should have their costs summed,
    and trace_count incremented. We verify the function buckets
    traces correctly without hitting real Langfuse."""
    from robotsix_mill.langfuse.client import aggregate_cost_trend

    s = _langfuse_settings()

    # We'll mock _langfuse_api_get directly via httpx.Client
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    # Create three traces: two in the same hour, one in the next hour
    t1_ts = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    t2_ts = (now - timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")
    t3_ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    fake_page_1 = {
        "data": [
            {"id": "t1", "name": "impl", "timestamp": t1_ts, "totalCost": 0.10},
            {"id": "t2", "name": "impl", "timestamp": t2_ts, "totalCost": 0.05},
            {"id": "t3", "name": "review", "timestamp": t3_ts, "totalCost": 0.20},
        ],
        "meta": {"totalPages": 1},
    }

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return _FakeResponse(200, fake_page_1)

    monkeypatch.setattr(httpx, "Client", _MockClient)

    result = aggregate_cost_trend(s, lookback_hours=3)

    # Should have 3 hourly buckets (or 4 depending on rounding)
    assert len(result) >= 2
    # Buckets containing t1+t2 should sum to 0.15 with trace_count 2
    # Bucket containing t3 should have 0.20 with trace_count 1
    total_cost_sum = sum(b["total_cost"] for b in result)
    total_trace_sum = sum(b["trace_count"] for b in result)
    assert total_cost_sum == pytest.approx(0.35)
    assert total_trace_sum == 3


def test_aggregate_cost_trend_produces_contiguous_buckets(monkeypatch):
    """Every hour in the lookback window gets a bucket, even empty ones."""
    from robotsix_mill.langfuse.client import aggregate_cost_trend

    s = _langfuse_settings()

    fake_page = {"data": [], "meta": {"totalPages": 1}}

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return _FakeResponse(200, fake_page)

    monkeypatch.setattr(httpx, "Client", _MockClient)

    result = aggregate_cost_trend(s, lookback_hours=3)

    # Should have 3 or 4 contiguous hourly buckets, all with zero cost
    assert len(result) >= 3
    for b in result:
        assert b["total_cost"] == 0.0
        assert b["trace_count"] == 0
        assert "ts" in b
        assert b["ts"].endswith("Z")


def test_aggregate_cost_trend_buckets_daily(monkeypatch):
    """lookback_hours > 24 → daily buckets at midnight boundaries."""
    from robotsix_mill.langfuse.client import aggregate_cost_trend
    from datetime import datetime, timedelta, timezone

    s = _langfuse_settings()

    now = datetime.now(timezone.utc)
    # Two traces: one ~60h ago (bucket 1 of 3), one ~20h ago (bucket 2 of 3)
    t1_ts = (now - timedelta(hours=60)).isoformat().replace("+00:00", "Z")
    t2_ts = (now - timedelta(hours=20)).isoformat().replace("+00:00", "Z")

    fake_page = {
        "data": [
            {"id": "d1", "name": "build", "timestamp": t1_ts, "totalCost": 0.30},
            {"id": "d2", "name": "deploy", "timestamp": t2_ts, "totalCost": 0.12},
        ],
        "meta": {"totalPages": 1},
    }

    class _MockClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, *, params, headers):
            return _FakeResponse(200, fake_page)

    monkeypatch.setattr(httpx, "Client", _MockClient)

    result = aggregate_cost_trend(s, lookback_hours=72)

    # Daily path: ceil(72/24) = 3 buckets
    assert len(result) == 3
    # Each ts must be midnight-aligned
    for b in result:
        assert b["ts"].endswith("T00:00:00Z"), f"not midnight: {b['ts']}"
    # Total cost and trace count should sum correctly
    assert sum(b["total_cost"] for b in result) == pytest.approx(0.42)
    assert sum(b["trace_count"] for b in result) == 2
    # At least two buckets have non-zero cost (each trace in its own day)
    nonzero = [b for b in result if b["total_cost"] > 0]
    assert len(nonzero) >= 2, (
        f"expected traces in different daily buckets, got: {nonzero}"
    )


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


def test_aggregate_cost_by_name_multi_page(monkeypatch):
    """600 traces across 6 pages: 200 * 'refine' at $0.01, 400 *
    'implement' at $0.02. Verify all 600 are counted (the old
    EXAMINE_CAP=500 would silently drop 100 traces)."""
    from datetime import datetime, timedelta, timezone
    from robotsix_mill.langfuse.client import aggregate_cost_by_name

    s = _langfuse_settings()
    now = datetime.now(timezone.utc)

    TOTAL_PAGES = 6
    PER_PAGE = 100

    # Build page payloads:
    #   Pages 1-2 → "refine" (200 traces @ $0.01)
    #   Pages 3-6 → "implement" (400 traces @ $0.02)
    pages: dict[int, dict] = {}
    seq = 0
    for pg in range(1, TOTAL_PAGES + 1):
        page_traces = []
        for _ in range(PER_PAGE):
            seq += 1
            ts = (now - timedelta(hours=seq * 0.01)).isoformat().replace("+00:00", "Z")
            if pg <= 2:
                page_traces.append(
                    {
                        "id": f"r{seq}",
                        "name": "refine",
                        "timestamp": ts,
                        "totalCost": 0.01,
                    }
                )
            else:
                page_traces.append(
                    {
                        "id": f"i{seq}",
                        "name": "implement",
                        "timestamp": ts,
                        "totalCost": 0.02,
                    }
                )
        pages[pg] = {
            "data": page_traces,
            "meta": {"totalPages": TOTAL_PAGES},
        }

    monkeypatch.setattr(httpx, "Client", _multi_page_mock_client(pages))

    result = aggregate_cost_by_name(s, lookback_hours=24)

    assert len(result) == 2, f"expected 2 names, got {len(result)}: {result}"
    by_name = {e["name"]: e for e in result}

    assert "refine" in by_name
    assert by_name["refine"]["total_cost"] == pytest.approx(2.00)  # 200 × $0.01
    assert by_name["refine"]["trace_count"] == 200

    assert "implement" in by_name
    assert by_name["implement"]["total_cost"] == pytest.approx(8.00)  # 400 × $0.02
    assert by_name["implement"]["trace_count"] == 400

    total = sum(e["total_cost"] for e in result)
    assert total == pytest.approx(10.00)


def test_aggregate_cost_trend_multi_page(monkeypatch):
    """600 traces across 6 pages in a 3-hour window. Verify all 600 are
    accounted for in the bucket totals."""
    from datetime import datetime, timedelta, timezone
    from robotsix_mill.langfuse.client import aggregate_cost_trend

    s = _langfuse_settings()
    now = datetime.now(timezone.utc)

    TOTAL_PAGES = 6
    PER_PAGE = 100

    pages: dict[int, dict] = {}
    seq = 0
    for pg in range(1, TOTAL_PAGES + 1):
        page_traces = []
        for _ in range(PER_PAGE):
            seq += 1
            # Spread traces across the lookback window
            offset_hours = (seq % 30) / 10.0  # 0.0 – 2.9 hours ago
            ts = (
                (now - timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")
            )
            page_traces.append(
                {"id": f"t{seq}", "name": "test", "timestamp": ts, "totalCost": 0.001}
            )
        pages[pg] = {
            "data": page_traces,
            "meta": {"totalPages": TOTAL_PAGES},
        }

    monkeypatch.setattr(httpx, "Client", _multi_page_mock_client(pages))

    result = aggregate_cost_trend(s, lookback_hours=3)

    total_cost = sum(b["total_cost"] for b in result)
    total_count = sum(b["trace_count"] for b in result)
    assert total_cost == pytest.approx(0.60)  # 600 × $0.001
    assert total_count == 600


# ---------------------------------------------------------------------------
# ticket_with_most_steps / trace_with_most_errors
# ---------------------------------------------------------------------------


def test_ticket_with_most_steps(monkeypatch):
    from robotsix_mill.langfuse import client as lf

    s = _langfuse_settings(LANGFUSE_PUBLIC_KEY="pk", LANGFUSE_SECRET_KEY="sk")
    traces = [
        {"name": "implement", "totalCost": 1.0, "sessionId": "A"},
        {"name": "ci_fix", "totalCost": 0.5, "sessionId": "A"},
        {"name": "ci_fix", "totalCost": 0.5, "sessionId": "A"},  # A = 3 steps
        {"name": "implement", "totalCost": 9.0, "sessionId": "B"},  # B = 1 step
    ]
    monkeypatch.setattr(lf, "_fetch_traces_time_window", lambda *a, **k: list(traces))
    res = lf.ticket_with_most_steps(s)
    assert res is not None
    assert res["session_id"] == "A"  # most steps, not most cost
    assert res["step_count"] == 3


def test_trace_with_most_errors(monkeypatch):
    from robotsix_mill.langfuse import client as lf

    s = _langfuse_settings(LANGFUSE_PUBLIC_KEY="pk", LANGFUSE_SECRET_KEY="sk")
    traces = [
        {"id": "t1", "name": "ci_fix", "totalCost": 2.0, "sessionId": "A"},
        {"id": "t2", "name": "implement", "totalCost": 5.0, "sessionId": "B"},
    ]
    obs_by_trace = {
        "t1": [{"level": "ERROR"}, {"level": "ERROR"}, {"output": "Error: x"}],
        "t2": [{"level": "DEFAULT", "output": "ok"}],
    }
    monkeypatch.setattr(lf, "_fetch_traces_time_window", lambda *a, **k: list(traces))
    monkeypatch.setattr(
        lf,
        "fetch_trace_observations",
        lambda settings, tid, repo_config=None: obs_by_trace.get(tid),
    )
    res = lf.trace_with_most_errors(s)
    assert res is not None
    assert res["id"] == "t1"
    assert res["error_count"] == 3


def test_trace_with_most_errors_none_when_clean(monkeypatch):
    from robotsix_mill.langfuse import client as lf

    s = _langfuse_settings(LANGFUSE_PUBLIC_KEY="pk", LANGFUSE_SECRET_KEY="sk")
    monkeypatch.setattr(
        lf,
        "_fetch_traces_time_window",
        lambda *a, **k: [{"id": "t1", "name": "x", "totalCost": 1.0}],
    )
    monkeypatch.setattr(
        lf,
        "fetch_trace_observations",
        lambda *a, **k: [{"level": "DEFAULT", "output": "fine"}],
    )
    assert lf.trace_with_most_errors(s) is None

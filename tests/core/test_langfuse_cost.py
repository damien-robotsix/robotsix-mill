"""Unit tests for ``LangfuseCostLogSource`` and its helpers.

Drives the Langfuse REST adapter offline via ``httpx.MockTransport``, covering
``fetch_logged_cost``, ``fetch_logged_cost_by_provider``, ``prune_before``,
timestamp parsing, and the observation provider/cost extraction helpers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from robotsix_llmio.core import langfuse_cost as langfuse_cost_module
from robotsix_llmio.core.cost_log import CostWindow, LoggedCost
from robotsix_llmio.core.langfuse_cost import (
    LangfuseCostLogSource,
    _observation_cost,
    _observation_provider,
    _parse_timestamp,
)


def _install_transport(monkeypatch, handler) -> list[httpx.Request]:
    """Patch ``httpx.Client`` so the adapter uses a ``MockTransport`` running
    *handler*. Returns a list that captures every request the adapter sends."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(langfuse_cost_module.httpx, "Client", _client)
    return captured


def _window() -> CostWindow:
    return CostWindow(
        start=datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
        end=datetime(2026, 6, 3, 11, 0, tzinfo=UTC),
    )


def _adapter() -> LangfuseCostLogSource:
    return LangfuseCostLogSource(
        public_key="pub", secret_key="sec", base_url="https://lf.example.com"
    )


# --------------------------------------------------------------------------- #
# fetch_logged_cost
# --------------------------------------------------------------------------- #
def test_fetch_logged_cost_single_page(monkeypatch):
    """A single non-empty page followed by an empty page aggregates correctly."""
    data = [
        {"id": "t1", "totalCost": 0.5, "timestamp": "2026-06-03T10:01:00Z"},
        {"id": "t2", "totalCost": 1.5, "timestamp": "2026-06-03T10:02:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": data if page == 1 else []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert isinstance(result, LoggedCost)
    assert result.record_count == 2
    assert result.total_cost == pytest.approx(2.0)


def test_fetch_logged_cost_multi_page_break(monkeypatch):
    """Pagination loops until an empty ``data`` page breaks the loop."""
    pages = {
        1: [{"id": "t1", "totalCost": 1.0, "timestamp": "2026-06-03T10:01:00Z"}],
        2: [{"id": "t2", "totalCost": 2.0, "timestamp": "2026-06-03T10:02:00Z"}],
        3: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": pages[page]})

    captured = _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert result.record_count == 2
    assert result.total_cost == pytest.approx(3.0)
    # pages 1, 2, then the empty page 3 that breaks the loop.
    assert [int(r.url.params["page"]) for r in captured] == [1, 2, 3]


def test_fetch_logged_cost_record_aggregation(monkeypatch):
    """Each CostRecord field maps from the trace dict; total is the sum."""
    data = [
        {
            "id": "t1",
            "totalCost": 0.75,
            "timestamp": "2026-06-03T10:01:00Z",
            "sessionId": "sess-1",
            "name": "trace-one",
        },
        {
            "id": "t2",
            "totalCost": 0.25,
            "timestamp": "2026-06-03T10:02:00Z",
            "sessionId": "sess-2",
            "name": "trace-two",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": data if page == 1 else []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert result.total_cost == pytest.approx(1.0)
    first = result.records[0]
    assert first.id == "t1"
    assert first.cost == pytest.approx(0.75)
    assert first.timestamp == datetime(2026, 6, 3, 10, 1, tzinfo=UTC)
    assert first.session_id == "sess-1"
    assert first.name == "trace-one"


def test_fetch_logged_cost_empty_data(monkeypatch):
    """An immediately empty response yields a zero-cost result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert result == LoggedCost(total_cost=0.0, record_count=0, records=[])


def test_fetch_logged_cost_non_2xx_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="500"):
        _adapter().fetch_logged_cost(_window())


# --------------------------------------------------------------------------- #
# fetch_logged_cost_by_provider
# --------------------------------------------------------------------------- #
def test_fetch_by_provider_filters_and_paginates(monkeypatch):
    """Only observations whose metadata provider matches are summed; the
    observations endpoint is paged until an empty page."""
    pages = {
        1: [
            {
                "id": "o1",
                "calculatedTotalCost": 0.4,
                "startTime": "2026-06-03T10:01:00Z",
                "traceId": "tr1",
                "name": "gen-1",
                "metadata": {"provider": "openrouter"},
            },
            {
                "id": "o2",
                "calculatedTotalCost": 9.9,
                "startTime": "2026-06-03T10:02:00Z",
                "metadata": {"provider": "claude-sdk"},
            },
        ],
        2: [
            {
                "id": "o3",
                "calculatedTotalCost": 0.6,
                "startTime": "2026-06-03T10:03:00Z",
                "traceId": "tr3",
                "metadata": {"provider": "openrouter"},
            },
        ],
        3: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/observations"
        assert request.url.params["type"] == "GENERATION"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": pages[page]})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost_by_provider(_window(), "openrouter")

    assert result.record_count == 2
    assert result.total_cost == pytest.approx(1.0)
    ids = {r.id for r in result.records}
    assert ids == {"o1", "o3"}
    # session_id falls back to the parent traceId for observations.
    o1 = next(r for r in result.records if r.id == "o1")
    assert o1.session_id == "tr1"
    assert o1.name == "gen-1"


def test_fetch_by_provider_cost_extraction_order(monkeypatch):
    """Cost is read from calculatedTotalCost, then totalCost, then
    costDetails.total — in that order."""
    data = [
        {
            "id": "calc",
            "calculatedTotalCost": 1.0,
            "totalCost": 99.0,
            "startTime": "2026-06-03T10:01:00Z",
            "metadata": {"provider": "p"},
        },
        {
            "id": "total",
            "totalCost": 2.0,
            "startTime": "2026-06-03T10:02:00Z",
            "metadata": {"provider": "p"},
        },
        {
            "id": "details",
            "costDetails": {"total": 3.0},
            "startTime": "2026-06-03T10:03:00Z",
            "metadata": {"provider": "p"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": data if page == 1 else []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost_by_provider(_window(), "p")

    assert result.total_cost == pytest.approx(6.0)
    by_id = {r.id: r.cost for r in result.records}
    assert by_id == {
        "calc": pytest.approx(1.0),
        "total": pytest.approx(2.0),
        "details": pytest.approx(3.0),
    }


def test_fetch_by_provider_non_2xx_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="403"):
        _adapter().fetch_logged_cost_by_provider(_window(), "openrouter")


# --------------------------------------------------------------------------- #
# prune_before
# --------------------------------------------------------------------------- #
def test_prune_before_deletes_and_counts(monkeypatch):
    """GET lists traces ≤ cutoff, DELETE removes them by id, and the loop stops
    once a list page is empty; the total deleted count is returned."""
    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    list_pages = iter(
        [
            [{"id": "t1"}, {"id": "t2"}],
            [{"id": "t3"}],
            [],
        ]
    )
    deleted_bodies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/traces"
        if request.method == "GET":
            assert request.url.params["toTimestamp"] == cutoff.isoformat()
            return httpx.Response(200, json={"data": next(list_pages)})
        assert request.method == "DELETE"
        body = json.loads(request.content)
        deleted_bodies.append(body["traceIds"])
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    count = _adapter().prune_before(cutoff)

    assert count == 3
    assert deleted_bodies == [["t1", "t2"], ["t3"]]


def test_prune_before_empty_returns_zero(monkeypatch):
    """No traces to prune: a single empty list page returns zero with no
    DELETE issued."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"data": []})

    _install_transport(monkeypatch, handler)
    count = _adapter().prune_before(datetime(2026, 6, 1, tzinfo=UTC))

    assert count == 0


def test_prune_before_non_2xx_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="503"):
        _adapter().prune_before(datetime(2026, 6, 1, tzinfo=UTC))


def test_prune_before_delete_non_2xx_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "t1"}]})
        return httpx.Response(500, text="delete-failed")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="delete"):
        _adapter().prune_before(datetime(2026, 6, 1, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# _parse_timestamp
# --------------------------------------------------------------------------- #
def test_parse_timestamp_z_suffix():
    assert _parse_timestamp("2024-01-01T12:00:00Z") == datetime(
        2024, 1, 1, 12, 0, tzinfo=UTC
    )


def test_parse_timestamp_offset_suffix():
    assert _parse_timestamp("2024-01-01T12:00:00+00:00") == datetime(
        2024, 1, 1, 12, 0, tzinfo=UTC
    )


def test_parse_timestamp_datetime_passthrough():
    moment = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert _parse_timestamp(moment) is moment


def test_parse_timestamp_invalid_raises():
    with pytest.raises(ValueError):
        _parse_timestamp("not-a-timestamp")


# --------------------------------------------------------------------------- #
# _observation_provider / _observation_cost
# --------------------------------------------------------------------------- #
def test_observation_provider_present():
    assert _observation_provider({"metadata": {"provider": "openrouter"}}) == (
        "openrouter"
    )


def test_observation_provider_missing_metadata():
    assert _observation_provider({}) is None
    assert _observation_provider({"metadata": {"other": "x"}}) is None


def test_observation_cost_extraction_order():
    assert _observation_cost(
        {"calculatedTotalCost": 1.0, "totalCost": 2.0}
    ) == pytest.approx(1.0)
    assert _observation_cost({"totalCost": 2.0}) == pytest.approx(2.0)
    assert _observation_cost({"costDetails": {"total": 3.0}}) == pytest.approx(3.0)


def test_observation_cost_missing_defaults_zero():
    assert _observation_cost({}) == 0.0
    assert _observation_cost({"costDetails": {}}) == 0.0

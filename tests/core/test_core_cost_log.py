"""Langfuse read seam — ``LangfuseCostLogSource`` request building, pagination,
aggregation, and protocol conformance, driven offline via ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import httpx
import pytest

from robotsix_llmio.core import langfuse_cost as langfuse_cost_module
from robotsix_llmio.core.cost_log import CostLogSource, CostWindow, LoggedCost
from robotsix_llmio.core.langfuse_cost import LangfuseCostLogSource


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
        start=datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 3, 11, 0, tzinfo=timezone.utc),
    )


def _adapter() -> LangfuseCostLogSource:
    return LangfuseCostLogSource(
        public_key="pub", secret_key="sec", base_url="https://lf.example.com"
    )


def test_multi_page_aggregation(monkeypatch):
    pages = {
        1: [
            {"id": "t1", "totalCost": 0.5, "timestamp": "2026-06-03T10:01:00Z"},
            {"id": "t2", "totalCost": 1.25, "timestamp": "2026-06-03T10:02:00Z"},
        ],
        2: [
            {"id": "t3", "totalCost": 0.25, "timestamp": "2026-06-03T10:03:00Z"},
        ],
        3: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": pages[page]})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert isinstance(result, LoggedCost)
    assert result.record_count == 3
    assert result.total_cost == pytest.approx(2.0)


def test_per_record_population(monkeypatch):
    data = [
        {
            "id": "t1",
            "totalCost": 0.5,
            "timestamp": "2026-06-03T10:01:00Z",
            "sessionId": "sess-1",
            "name": "trace-one",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": data if page == 1 else []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert len(result.records) == 1
    record = result.records[0]
    assert record.id == "t1"
    assert record.cost == pytest.approx(0.5)
    assert record.timestamp == datetime(2026, 6, 3, 10, 1, tzinfo=timezone.utc)
    assert record.session_id == "sess-1"
    assert record.name == "trace-one"


def test_empty_window_zero_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert result == LoggedCost(total_cost=0.0, record_count=0, records=[])


def test_non_2xx_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    _install_transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="401"):
        _adapter().fetch_logged_cost(_window())


def test_runtime_protocol_conformance():
    assert isinstance(_adapter(), CostLogSource)


def test_request_shape(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": []
                if page > 1
                else [
                    {"id": "t1", "totalCost": 0.1, "timestamp": "2026-06-03T10:01:00Z"},
                ]
            },
        )

    captured = _install_transport(monkeypatch, handler)
    _adapter().fetch_logged_cost(_window())

    first = captured[0]
    assert first.url.path == "/api/public/traces"
    assert first.url.params["fromTimestamp"] == "2026-06-03T10:00:00+00:00"
    assert first.url.params["toTimestamp"] == "2026-06-03T11:00:00+00:00"
    expected_auth = "Basic " + base64.b64encode(b"pub:sec").decode()
    assert first.headers["Authorization"] == expected_auth


def test_meta_total_pages_stops_pagination(monkeypatch):
    """When the response carries ``meta.totalPages``, paging stops at that page
    even if the last page is non-empty."""
    data = [{"id": "t1", "totalCost": 1.0, "timestamp": "2026-06-03T10:01:00Z"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data, "meta": {"totalPages": 1}})

    captured = _install_transport(monkeypatch, handler)
    result = _adapter().fetch_logged_cost(_window())

    assert result.record_count == 1
    assert len(captured) == 1

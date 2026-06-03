"""Provider-cost read seam + reconciliation — offline unit tests.

``reconcile`` and ``_utc_dates`` are pure. The OpenRouter activity fetch and
the Langfuse time-based ``prune_before`` hit httpx, so they are exercised with
``httpx.MockTransport`` (no network, no respx dependency).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from robotsix_llmio.core.cost_log import CostWindow, LoggedCost
from robotsix_llmio.core.provider_cost import (
    DEFAULT_TOLERANCE,
    ProviderCost,
    reconcile,
)
from robotsix_llmio.openrouter import provider_cost as orpc
from robotsix_llmio.openrouter.provider_cost import (
    OpenRouterProviderCostSource,
    _utc_dates,
)


def _window(start: str, end: str) -> CostWindow:
    return CostWindow(
        start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
    )


# --- reconcile (pure) --------------------------------------------------------


def test_reconcile_within_tolerance():
    logged = LoggedCost(total_cost=10.40, record_count=12)
    provider = ProviderCost(total_cost=11.20)
    d = reconcile(logged, provider)  # default $1 tolerance
    assert d.delta == round(abs(11.20 - 10.40), 10) or abs(d.delta - 0.80) < 1e-9
    assert d.within_tolerance is True
    assert d.tolerance == DEFAULT_TOLERANCE
    assert d.logged_total == 10.40 and d.provider_total == 11.20


def test_reconcile_over_tolerance():
    d = reconcile(
        LoggedCost(total_cost=5.0, record_count=3),
        ProviderCost(total_cost=9.5),
    )
    assert abs(d.delta - 4.5) < 1e-9
    assert d.within_tolerance is False


def test_reconcile_custom_tolerance():
    d = reconcile(
        LoggedCost(total_cost=5.0, record_count=3),
        ProviderCost(total_cost=6.5),
        tolerance=2.0,
    )
    assert d.within_tolerance is True  # 1.5 <= 2.0


# --- _utc_dates (pure) -------------------------------------------------------


def test_utc_dates_single_settled_day():
    w = _window("2026-06-02T00:00:00", "2026-06-03T00:00:00")
    assert _utc_dates(w) == ["2026-06-02"]


def test_utc_dates_multi_day():
    w = _window("2026-06-01T00:00:00", "2026-06-03T00:00:00")
    assert _utc_dates(w) == ["2026-06-01", "2026-06-02"]  # end exclusive


# --- OpenRouter activity fetch (mocked httpx) --------------------------------


def _mock_client_factory(monkeypatch, module, handler):
    """Patch *module*.httpx.Client to use a MockTransport(handler).

    ``module.httpx`` is the shared httpx module, so patching its ``Client``
    affects every reference — capture the real class FIRST so the factory
    doesn't recurse into itself.
    """
    real_client = httpx.Client

    def _make(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "Client", _make)


def test_openrouter_fetch_sums_usage_and_byok(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/activity"
        assert request.url.params.get("date") == "2026-06-02"
        assert request.headers["Authorization"] == "Bearer mgmt-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "model": "a",
                        "usage": 1.0,
                        "byok_usage_inference": 0.5,
                        "num_requests": 3,
                    },
                    {
                        "model": "b",
                        "usage": 2.0,
                        "byok_usage_inference": 0,
                        "num_requests": 1,
                    },
                ]
            },
        )

    _mock_client_factory(monkeypatch, orpc, handler)
    src = OpenRouterProviderCostSource(management_key="mgmt-key")
    pc = src.fetch_provider_cost(_window("2026-06-02T00:00:00", "2026-06-03T00:00:00"))
    assert abs(pc.total_cost - 3.5) < 1e-9  # 1.5 + 2.0
    assert pc.request_count == 4
    assert pc.breakdown == {"a": 1.5, "b": 2.0}


def test_openrouter_fetch_raises_on_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    _mock_client_factory(monkeypatch, orpc, handler)
    src = OpenRouterProviderCostSource(management_key="bad")
    try:
        src.fetch_provider_cost(_window("2026-06-02T00:00:00", "2026-06-03T00:00:00"))
        raise AssertionError("expected RuntimeError on non-2xx")
    except RuntimeError as e:
        assert "403" in str(e)


# --- Langfuse time-based prune (mocked httpx) --------------------------------


def test_langfuse_prune_before_deletes_old_traces(monkeypatch):
    from robotsix_llmio.core import langfuse_cost as lc

    pages = [
        [{"id": "t1"}, {"id": "t2"}],  # first list page
        [],  # then nothing left ≤ cutoff
    ]
    deleted_payloads: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params.get("toTimestamp") == "2026-06-01T00:00:00+00:00"
            return httpx.Response(200, json={"data": pages.pop(0)})
        if request.method == "DELETE":
            deleted_payloads.append(json.loads(request.content)["traceIds"])
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method}")

    _mock_client_factory(monkeypatch, lc, handler)
    src = lc.LangfuseCostLogSource(public_key="pk", secret_key="sk")
    n = src.prune_before(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert n == 2
    assert deleted_payloads == [["t1", "t2"]]

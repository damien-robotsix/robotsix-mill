"""Core provider-cost read seam & reconciliation — offline unit tests.

``reconcile`` is pure. The Langfuse time-based ``prune_before`` hits httpx,
so it is exercised with ``httpx.MockTransport`` (no network, no respx dependency).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from robotsix_llmio.core.cost_log import CostWindow, LoggedCost
from robotsix_llmio.core.provider_cost import (
    DEFAULT_TOLERANCE,
    ProviderCost,
    reconcile,
)


def _window(start: str, end: str) -> CostWindow:
    return CostWindow(
        start=datetime.fromisoformat(start).replace(tzinfo=UTC),
        end=datetime.fromisoformat(end).replace(tzinfo=UTC),
    )


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


# --- Langfuse time-based prune (mocked httpx) --------------------------------


def test_langfuse_fetch_logged_cost_by_provider_filters(monkeypatch):
    from robotsix_llmio.core import langfuse_cost as lc

    pages = [
        [
            {
                "id": "o1",
                "calculatedTotalCost": 1.5,
                "metadata": {"provider": "openrouter"},
                "startTime": "2026-06-02T01:00:00Z",
            },
            {
                "id": "o2",
                "calculatedTotalCost": 9.0,
                "metadata": {"provider": "claude-sdk"},
                "startTime": "2026-06-02T02:00:00Z",
            },
            {  # no provider tag → excluded
                "id": "o3",
                "calculatedTotalCost": 4.0,
                "metadata": {},
                "startTime": "2026-06-02T03:00:00Z",
            },
        ],
        [],  # second page empty → stop
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/observations"
        assert request.url.params.get("type") == "GENERATION"
        return httpx.Response(200, json={"data": pages.pop(0)})

    _mock_client_factory(monkeypatch, lc, handler)
    src = lc.LangfuseCostLogSource(public_key="pk", secret_key="sk")
    w = _window("2026-06-02T00:00:00", "2026-06-03T00:00:00")
    logged = src.fetch_logged_cost_by_provider(w, "openrouter")
    assert abs(logged.total_cost - 1.5) < 1e-9
    assert logged.record_count == 1
    assert logged.records[0].id == "o1"
    assert logged.records[0].session_id is None  # no traceId on the obs


def test_langfuse_observation_cost_fallbacks():
    from robotsix_llmio.core.langfuse_cost import _observation_cost

    assert _observation_cost({"calculatedTotalCost": 2.0}) == 2.0
    assert _observation_cost({"totalCost": 3.0}) == 3.0
    assert _observation_cost({"costDetails": {"total": 4.0}}) == 4.0
    assert _observation_cost({}) == 0.0


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
    n = src.prune_before(datetime(2026, 6, 1, tzinfo=UTC))
    assert n == 2
    assert deleted_payloads == [["t1", "t2"]]

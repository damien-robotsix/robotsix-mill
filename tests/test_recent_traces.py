"""Tests for langfuse_client.list_recent_traces.

The function had two bugs the board's deep-review feature surfaced:

1. With a cost filter active, the function asked Langfuse for
   ``limit * 5`` traces, which exceeded Langfuse's hard cap of 100 the
   moment the UI's "show" field went above 20. The /api/public/traces
   endpoint returned HTTP 400, _langfuse_api_get returned None, and the
   function returned [] — so the UI showed "no traces" the moment the
   user moved past Show=20 with any cost filter set.

2. The cost filter was applied AFTER a single bounded fetch, so any
   matching trace older than that first page never got considered.
   The user wanted the filter applied to ALL recent traces.

Both fixes use pagination: page through Langfuse 100 at a time, filter
as we go, stop when we have ``limit`` matches or we've examined the
``examine_cap`` safety budget.
"""

from __future__ import annotations

from robotsix_mill.config import Settings


def _settings(tmp_path):
    return Settings(
        MILL_DATA_DIR=str(tmp_path),
        LANGFUSE_BASE_URL="https://lf.example.com",
        LANGFUSE_PUBLIC_KEY="pk",
        LANGFUSE_SECRET_KEY="sk",
    )


def _trace(tid: str, cost: float = 0.0, name: str = "n") -> dict:
    """Build a fake trace. Default name='n' (a named/completed trace).
    Pass name=None or "" to simulate an in-flight unnamed trace."""
    return {"id": tid, "name": name, "timestamp": "2026-05-21T00:00:00Z",
            "sessionId": "s", "totalCost": cost, "userId": None}


def test_no_cost_filter_single_fetch(tmp_path, monkeypatch):
    """Without cost filter: one Langfuse call, returns up to limit."""
    calls = []

    def fake_get(settings, path, params=None):
        calls.append((path, dict(params or {})))
        return {"data": [_trace(f"t{i}", 0.01) for i in range(50)]}

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(_settings(tmp_path), limit=10)
    assert len(out) == 10
    assert len(calls) == 1  # single fetch
    # ``page`` should NOT be sent for the no-filter happy path (preserves
    # current behaviour; only the limit param is set).
    assert "page" not in calls[0][1]


def test_cost_filter_does_not_break_at_limit_21(tmp_path, monkeypatch):
    """Regression for the 'limit > 20' cliff bug.

    Previously: limit=21 with any cost filter produced fetch_limit=105,
    Langfuse capped at 100 → HTTP 400 → [] returned.

    Now: paginate in 100-trace chunks, never exceed Langfuse's cap.
    """
    calls = []

    def fake_get(settings, path, params=None):
        p = dict(params or {})
        calls.append(p)
        # Return a full page of cost=0.01 traces (matches min_cost=0).
        return {"data": [_trace(f"t{p['page'] * 100 + i}", 0.01)
                         for i in range(100)]}

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(
        _settings(tmp_path), limit=21, min_cost=0.0,
    )
    assert len(out) == 21
    # Every Langfuse request must have limit <= 100 (the API cap).
    for c in calls:
        assert c["limit"] <= 100, f"oversized fetch: {c['limit']}"


def test_cost_filter_paginates_for_sparse_matches(tmp_path, monkeypatch):
    """Sparse matches: filter applied AS WE PAGE, not after one bounded
    fetch. If only 1 in every 100 traces matches, we have to read
    multiple pages to get ``limit`` of them — which is the new
    behaviour."""
    calls = []

    def fake_get(settings, path, params=None):
        p = dict(params or {})
        page = p["page"]
        calls.append(page)
        # Page 1: 100 traces all $0.001 (below min_cost=0.01) — no matches.
        # Page 2: 100 traces all $0.05 (above the filter) — all matches.
        if page == 1:
            return {"data": [_trace(f"a{i}", 0.001) for i in range(100)]}
        if page == 2:
            return {"data": [_trace(f"b{i}", 0.05) for i in range(100)]}
        return {"data": []}  # exhausted

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(
        _settings(tmp_path), limit=5, min_cost=0.01,
    )
    assert len(out) == 5
    assert all(t["id"].startswith("b") for t in out), \
        "filter must be applied to ALL paginated traces, not just page 1"
    assert calls == [1, 2], "must have requested two pages"


def test_examine_cap_bounds_pagination(tmp_path, monkeypatch):
    """A too-strict filter that matches nothing must not paginate
    forever. The examine_cap (max(limit * 20, 500) traces examined)
    bounds the worst case."""
    calls = []

    def fake_get(settings, path, params=None):
        p = dict(params or {})
        calls.append(p["page"])
        # Every page is full of low-cost traces — nothing matches.
        return {"data": [_trace(f"p{p['page']}-{i}", 0.0) for i in range(100)]}

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(
        _settings(tmp_path), limit=10, min_cost=999.0,
    )
    assert out == []
    # examine_cap = max(10*20, 500) = 500, page size 100 → at most 5 pages.
    assert len(calls) <= 5, f"unbounded pagination: {len(calls)} pages"


def test_unnamed_traces_filtered_out_no_cost_filter(tmp_path, monkeypatch):
    """In-flight traces show as unnamed (root span hasn't closed +
    propagated a name yet). They can't be deep-reviewed (partial
    observation tree), so the picker excludes them."""
    def fake_get(settings, path, params=None):
        # Mix of named and unnamed (None/empty) traces.
        return {"data": [
            _trace("t1", 0.01, name="refine"),
            _trace("t2", 0.02, name=None),       # in-flight
            _trace("t3", 0.03, name=""),         # in-flight
            _trace("t4", 0.04, name="implement"),
            _trace("t5", 0.05, name="retrospect"),
        ]}

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(_settings(tmp_path), limit=10)
    assert [t["id"] for t in out] == ["t1", "t4", "t5"]


def test_unnamed_traces_filtered_out_with_cost_filter(tmp_path, monkeypatch):
    """Same filter applies under the paginated cost-filter path."""
    def fake_get(settings, path, params=None):
        return {"data": [
            _trace("t1", 0.05, name="refine"),
            _trace("t2", 0.06, name=None),       # in-flight, excluded
            _trace("t3", 0.07, name="implement"),
        ]}

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(
        _settings(tmp_path), limit=10, min_cost=0.01,
    )
    assert [t["id"] for t in out] == ["t1", "t3"]


def test_cost_filter_handles_empty_page(tmp_path, monkeypatch):
    """If Langfuse returns an empty page mid-pagination, stop cleanly
    and return what we have."""
    calls = []

    def fake_get(settings, path, params=None):
        p = dict(params or {})
        calls.append(p["page"])
        if p["page"] == 1:
            return {"data": [_trace(f"x{i}", 0.05) for i in range(3)]}
        return {"data": []}  # exhausted

    from robotsix_mill import langfuse_client
    monkeypatch.setattr(langfuse_client, "_langfuse_api_get", fake_get)

    out = langfuse_client.list_recent_traces(
        _settings(tmp_path), limit=10, min_cost=0.01,
    )
    assert len(out) == 3
    assert calls == [1, 2]

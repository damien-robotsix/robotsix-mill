"""Dedicated unit tests for ``_paginated_get`` — the shared GitHub API
pagination helper.

These exercise the pagination loop, 401-retry with partial-accumulation
clear, fallback-on-exception, and boundary cases (empty result, exact
page size) by mocking the ``_ApiClient.retrying_client`` transport seam.
"""

from __future__ import annotations

import httpx as real_httpx

from robotsix_mill.forge._github_pagination import _paginated_get


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A response whose ``status_code``, ``.json()``, and
    ``.raise_for_status()`` are fully controlled."""

    def __init__(
        self, status_code: int, json_data: list[dict[str, object]]
    ) -> None:
        self.status_code = status_code
        self._json = json_data

    def json(self) -> list[dict[str, object]]:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code == 401:
            return  # _paginated_get handles 401 before calling raise_for_status
        if not (200 <= self.status_code < 300):
            req = object()  # dummy request
            raise real_httpx.HTTPStatusError(
                f"Server error: {self.status_code}",
                request=req,  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


class _MockClient:
    """Fake ``httpx.Client`` that returns *pages* in order on each
    ``.get()`` call and records every call for later inspection."""

    def __init__(self, pages: list[_FakeResponse]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, object]] = []

    def get(
        self, url: str, headers: object = None, params: object = None
    ) -> _FakeResponse:
        self.calls.append(
            {"url": url, "headers": headers, "params": params}
        )
        if not self._pages:
            return _FakeResponse(200, [])
        return self._pages.pop(0)


def _make_http(
    *retry_sequences: list[tuple[int, list[dict[str, object]]]],
) -> tuple[object, list[list[_MockClient]]]:
    """Build a fake ``_ApiClient`` and return ``(fake_http, all_clients)``.

    Each element of *retry_sequences* is a list of ``(status_code,
    json_data)`` tuples representing the pages returned by successive
    ``c.get()`` calls inside one retry attempt.

    ``all_clients`` is a list (one entry per retry attempt) of
    ``_MockClient`` instances so tests can inspect recorded calls.
    """
    on_retry_log: list[int] = []
    all_clients: list[list[_MockClient]] = []

    class _FakeApiClient:
        def retrying_client(self, on_retry=None, max_retries=2):
            for retry_idx, pages in enumerate(retry_sequences):
                if on_retry is not None:
                    on_retry()
                    on_retry_log.append(retry_idx)
                responses = [
                    _FakeResponse(sc, data) for sc, data in pages
                ]
                clients: list[_MockClient] = []
                client = _MockClient(responses)
                clients.append(client)
                all_clients.append(clients)
                yield retry_idx, client, "https://api.github.com", {
                    "Authorization": "Bearer tok"
                }
                # Control returns here when caller *continues* (401).
                # Nothing to do in the mock — sleep + token invalidation
                # aren't visible to _paginated_get.

    fake_http = _FakeApiClient()
    # Attach the on_retry log for test assertions
    fake_http._on_retry_log = on_retry_log  # type: ignore[attr-defined]
    return fake_http, all_clients


def _item_fn(item: dict[str, object]) -> str:
    """Extract the ``"name"`` key — matches the real-world pattern used
    by label / branch / PR list callers."""
    return str(item["name"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# -- single page ------------------------------------------------------------


def test_single_page_less_than_100():
    """One page with 50 items → returns all 50, stops after first page."""
    items = [{"name": f"item-{i}"} for i in range(50)]
    http, _ = _make_http([(200, items)])

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    assert result == [f"item-{i}" for i in range(50)]


# -- multi-page -------------------------------------------------------------


def test_multi_page():
    """Three pages (100, 100, 50) → all 250 items returned."""
    page1 = [{"name": f"a-{i}"} for i in range(100)]
    page2 = [{"name": f"b-{i}"} for i in range(100)]
    page3 = [{"name": f"c-{i}"} for i in range(50)]
    http, _ = _make_http([(200, page1), (200, page2), (200, page3)])

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    assert len(result) == 250
    assert result[:100] == [f"a-{i}" for i in range(100)]
    assert result[100:200] == [f"b-{i}" for i in range(100)]
    assert result[200:] == [f"c-{i}" for i in range(50)]


# -- exact-100 last page boundary -------------------------------------------


def test_exactly_100_on_last_page():
    """Two pages of exactly 100 each → the loop requests a third page
    (which returns 0 items), and correctly stops."""
    page1 = [{"name": f"p1-{i}"} for i in range(100)]
    page2 = [{"name": f"p2-{i}"} for i in range(100)]
    page3: list[dict[str, object]] = []  # third page is empty
    http, _ = _make_http([(200, page1), (200, page2), (200, page3)])

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    assert len(result) == 200
    assert result[:100] == [f"p1-{i}" for i in range(100)]
    assert result[100:] == [f"p2-{i}" for i in range(100)]


# -- 401 retry mid-loop -----------------------------------------------------


def test_401_on_page_2_clears_and_retries():
    """Page 1 succeeds (100 items — exactly the page size so the loop
    requests page 2), page 2 returns 401.
    The retry clears accumulated output and re-fetches both pages
    successfully."""
    page1a = [{"name": f"first-{i}"} for i in range(100)]
    page2_401: tuple[int, list[dict[str, object]]] = (401, [])
    page1b = [{"name": f"retry-{i}"} for i in range(100)]
    page2b = [{"name": f"retry2-{i}"} for i in range(30)]

    http, all_clients = _make_http(
        [(200, page1a), page2_401],       # first retry: page 1 OK, page 2 401
        [(200, page1b), (200, page2b)],   # second retry: both OK
    )

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    # on_retry should have been called twice (once per retry attempt)
    assert http._on_retry_log == [0, 1]  # type: ignore[attr-defined]

    # Result should be from the second (successful) retry only
    assert len(result) == 130
    assert result[:100] == [f"retry-{i}" for i in range(100)]
    assert result[100:] == [f"retry2-{i}" for i in range(30)]

    # First retry attempt's client made 2 calls (page 1 OK, page 2 401)
    assert len(all_clients[0][0].calls) == 2
    # Second retry attempt's client made 2 calls (both OK)
    assert len(all_clients[1][0].calls) == 2


# -- non-401 exception → fallback ------------------------------------------


def test_non_401_exception_returns_fallback():
    """Page 1 succeeds (100 items — exactly the page size so the loop
    requests page 2), page 2 raises HTTPStatusError (500).
    The exception is caught and the fallback is returned."""
    page1 = [{"name": f"ok-{i}"} for i in range(100)]
    page2_500: tuple[int, list[dict[str, object]]] = (500, [])
    http, _ = _make_http([(200, page1), page2_500])

    result = _paginated_get(
        http, "/repos/o/r/pulls",
        item_fn=_item_fn,
        fallback=["fallback-value"],
    )

    assert result == ["fallback-value"]


def test_non_401_exception_returns_typed_fallback():
    """Fallback can be any type — not just a list.  Verify it passes
    through unchanged."""
    page1_500: tuple[int, list[dict[str, object]]] = (500, [])
    http, _ = _make_http([page1_500])

    result = _paginated_get(
        http, "/repos/o/r/pulls",
        item_fn=_item_fn,
        fallback=None,
    )

    assert result is None


# -- empty result -----------------------------------------------------------


def test_empty_result():
    """First page returns 0 items → loop breaks immediately, returns []."""
    http, _ = _make_http([(200, [])])

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    assert result == []


# -- url / params forwarding -----------------------------------------------


def test_url_suffix_and_params_are_forwarded():
    """The url_suffix is appended to the API base URL and extra *params*
    are merged alongside ``per_page`` and ``page``."""
    items = [{"name": "x"}]
    http, all_clients = _make_http([(200, items)])

    _paginated_get(
        http,
        "/repos/owner/repo/issues/42/labels",
        params={"state": "open", "sort": "updated"},
        item_fn=_item_fn,
    )

    calls = all_clients[0][0].calls
    assert len(calls) == 1
    assert calls[0]["url"] == (
        "https://api.github.com"
        "/repos/owner/repo/issues/42/labels"
    )
    assert calls[0]["params"] == {
        "per_page": 100,
        "page": 1,
        "state": "open",
        "sort": "updated",
    }


# -- fallback default (None → not passed) ----------------------------------


def test_fallback_defaults_to_none():
    """When *fallback* is not passed, it defaults to ``None``.
    The overload that omits *fallback* returns ``list[T]``."""
    page1: list[dict[str, object]] = [{"name": "a"}]
    http, _ = _make_http([(200, page1)])

    result = _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    # No fallback provided — normal path returns the list
    assert result == ["a"]


# -- page counter increments ------------------------------------------------


def test_page_counter_increments():
    """Each page request increments the ``page`` query parameter."""
    page1 = [{"name": f"a-{i}"} for i in range(100)]
    page2 = [{"name": f"b-{i}"} for i in range(100)]
    page3 = [{"name": f"c-{i}"} for i in range(50)]
    http, all_clients = _make_http([(200, page1), (200, page2), (200, page3)])

    _paginated_get(
        http, "/repos/o/r/pulls", item_fn=_item_fn
    )

    calls = all_clients[0][0].calls
    assert len(calls) == 3
    assert calls[0]["params"]["page"] == 1  # type: ignore[index]
    assert calls[1]["params"]["page"] == 2  # type: ignore[index]
    assert calls[2]["params"]["page"] == 3  # type: ignore[index]

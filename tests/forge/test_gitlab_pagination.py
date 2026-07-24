"""Dedicated unit tests for ``_paginated_get`` — the shared GitLab API
pagination helper.

These exercise the pagination loop, error handling, item transformation,
and boundary cases (empty result, exact page size) by mocking the
``_ApiClient.client`` transport seam.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import httpx as real_httpx

from robotsix_mill.forge.gitlab._pagination import _paginated_get


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A response whose ``status_code``, ``.json()``, and
    ``.raise_for_status()`` are fully controlled."""

    def __init__(self, status_code: int, json_data: list[dict[str, Any]]) -> None:
        self.status_code = status_code
        self._json = json_data

    def json(self) -> list[dict[str, Any]]:
        return self._json

    def raise_for_status(self) -> None:
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
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self._pages:
            return _FakeResponse(200, [])
        return self._pages.pop(0)


def _make_http(
    pages: list[tuple[int, list[dict[str, Any]]]],
) -> tuple[Any, _MockClient]:
    """Build a fake ``_ApiClient`` whose ``client()`` context manager
    yields a ``_MockClient`` pre-loaded with *pages*.

    Returns ``(fake_http, mock_client)`` so tests can inspect recorded
    calls via ``mock_client.calls``.
    """
    responses = [_FakeResponse(sc, data) for sc, data in pages]
    mock_client = _MockClient(responses)

    class _FakeApiClient:
        @contextmanager
        def client(self):  # type: ignore[no-untyped-def]
            yield (
                mock_client,
                "https://gitlab.com/api/v4",
                {"Authorization": "Bearer tok"},
            )

    return _FakeApiClient(), mock_client


def _item_fn(item: dict[str, Any]) -> str:
    """Extract the ``"name"`` key — matches the real-world pattern used
    by branch / MR list callers."""
    return str(item["name"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# -- single page ------------------------------------------------------------


def test_single_page_less_than_100() -> None:
    """One page with 50 items → yields all 50, stops after first page."""
    items = [{"name": f"item-{i}"} for i in range(50)]
    http, _ = _make_http([(200, items)])

    result = list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert result == [f"item-{i}" for i in range(50)]


# -- multi-page -------------------------------------------------------------


def test_multi_page() -> None:
    """Three pages (100, 100, 50) → all 250 items yielded."""
    page1 = [{"name": f"a-{i}"} for i in range(100)]
    page2 = [{"name": f"b-{i}"} for i in range(100)]
    page3 = [{"name": f"c-{i}"} for i in range(50)]
    http, _ = _make_http([(200, page1), (200, page2), (200, page3)])

    result = list(
        _paginated_get(
            http,
            "/projects/1/merge_requests",
            params={"state": "opened"},
            item_fn=_item_fn,
        )
    )

    assert len(result) == 250
    assert result[:100] == [f"a-{i}" for i in range(100)]
    assert result[100:200] == [f"b-{i}" for i in range(100)]
    assert result[200:] == [f"c-{i}" for i in range(50)]


# -- exact-100 last page boundary -------------------------------------------


def test_exactly_100_on_last_page() -> None:
    """Two pages of exactly 100 each → the loop requests a third page
    (which returns 0 items), and correctly stops."""
    page1 = [{"name": f"p1-{i}"} for i in range(100)]
    page2 = [{"name": f"p2-{i}"} for i in range(100)]
    page3: list[dict[str, Any]] = []  # third page is empty
    http, _ = _make_http([(200, page1), (200, page2), (200, page3)])

    result = list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert len(result) == 200
    assert result[:100] == [f"p1-{i}" for i in range(100)]
    assert result[100:] == [f"p2-{i}" for i in range(100)]


# -- empty result -----------------------------------------------------------


def test_empty_result() -> None:
    """First page returns 0 items → loop breaks immediately, yields nothing."""
    http, _ = _make_http([(200, [])])

    result = list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert result == []


# -- HTTP error -------------------------------------------------------------


def test_http_error_raises() -> None:
    """A 500 response raises HTTPStatusError via raise_for_status()."""
    http, _ = _make_http([(500, [])])

    try:
        list(
            _paginated_get(
                http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
            )
        )
    except real_httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500
    else:
        raise AssertionError("Expected HTTPStatusError")


def test_http_404_raises() -> None:
    """A 404 response raises HTTPStatusError via raise_for_status()."""
    http, _ = _make_http([(404, [])])

    try:
        list(
            _paginated_get(
                http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
            )
        )
    except real_httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 404
    else:
        raise AssertionError("Expected HTTPStatusError")


# -- item_fn transform ------------------------------------------------------


def test_item_fn_transform() -> None:
    """Verify item_fn is applied to each dict before yielding."""
    items = [{"name": "main"}, {"name": "develop"}, {"name": "feature/x"}]
    http, _ = _make_http([(200, items)])

    result = list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert result == ["main", "develop", "feature/x"]


def test_item_fn_custom_transform() -> None:
    """Verify a custom item_fn that extracts a different key."""
    items: list[dict[str, Any]] = [
        {"id": 1, "title": "Fix bug"},
        {"id": 2, "title": "Add feature"},
    ]
    http, _ = _make_http([(200, items)])

    def extract_id(item: dict[str, Any]) -> int:
        return int(item["id"])

    result = list(
        _paginated_get(
            http, "/projects/1/merge_requests", params={}, item_fn=extract_id
        )
    )

    assert result == [1, 2]


# -- parameter forwarding ---------------------------------------------------


def test_per_page_and_page_are_forwarded() -> None:
    """Verify ``per_page=100`` and ``page=1`` are sent on the first request."""
    items = [{"name": "x"}]
    http, mock_client = _make_http([(200, items)])

    list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert len(mock_client.calls) == 1
    assert mock_client.calls[0]["params"] == {"per_page": 100, "page": 1}


def test_extra_params_are_merged() -> None:
    """Extra *params* are merged alongside ``per_page`` and ``page``."""
    items = [{"name": "x"}]
    http, mock_client = _make_http([(200, items)])

    list(
        _paginated_get(
            http,
            "/projects/1/merge_requests",
            params={"state": "opened", "sort": "updated"},
            item_fn=_item_fn,
        )
    )

    assert mock_client.calls[0]["params"] == {
        "per_page": 100,
        "page": 1,
        "state": "opened",
        "sort": "updated",
    }


def test_url_suffix_is_appended() -> None:
    """The url_suffix is appended to the API base URL."""
    items = [{"name": "x"}]
    http, mock_client = _make_http([(200, items)])

    list(
        _paginated_get(
            http,
            "/projects/42/repository/branches",
            params={"search": "feat"},
            item_fn=_item_fn,
        )
    )

    assert mock_client.calls[0]["url"] == (
        "https://gitlab.com/api/v4/projects/42/repository/branches"
    )


# -- page counter increments ------------------------------------------------


def test_page_counter_increments() -> None:
    """Each page request increments the ``page`` query parameter."""
    page1 = [{"name": f"a-{i}"} for i in range(100)]
    page2 = [{"name": f"b-{i}"} for i in range(100)]
    page3 = [{"name": f"c-{i}"} for i in range(50)]
    http, mock_client = _make_http([(200, page1), (200, page2), (200, page3)])

    list(
        _paginated_get(
            http, "/projects/1/repository/branches", params={}, item_fn=_item_fn
        )
    )

    assert len(mock_client.calls) == 3
    assert mock_client.calls[0]["params"]["page"] == 1  # type: ignore[index]
    assert mock_client.calls[1]["params"]["page"] == 2  # type: ignore[index]
    assert mock_client.calls[2]["params"]["page"] == 3  # type: ignore[index]

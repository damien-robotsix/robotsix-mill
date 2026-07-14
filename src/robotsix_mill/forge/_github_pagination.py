"""Shared pagination helper for GitHub API endpoints.

Extracted from ``github_pr.py`` so it can be reused across methods
and fixes the silent-truncation bug affecting repos with more than
100 items of any paginated resource.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from ._http import _ApiClient

T = TypeVar("T")
R = TypeVar("R")


@overload
def _paginated_get(
    http: _ApiClient,
    url_suffix: str,
    *,
    params: dict[str, Any] | None = None,
    item_fn: Callable[[dict[str, Any]], T],
    fallback: R,
) -> list[T] | R:
    pass


@overload
def _paginated_get(
    http: _ApiClient,
    url_suffix: str,
    *,
    params: dict[str, Any] | None = None,
    item_fn: Callable[[dict[str, Any]], T],
) -> list[T]:
    pass


def _paginated_get(
    http: _ApiClient,
    url_suffix: str,
    *,
    params: dict[str, Any] | None = None,
    item_fn: Callable[[dict[str, Any]], T],
    fallback: Any = None,
) -> Any:
    """Paginate through a GitHub API endpoint, calling *item_fn* on each item.

    Integrates with :meth:`_ApiClient.retrying_client` to retry on 401
    (invalidating the cached token and clearing accumulated output).
    Pagination stops when fewer than 100 items are returned (last page).

    Returns *fallback* when all retries are exhausted or any other
    exception occurs (matches the existing "return [] on failure"
    convention).
    """
    out: list[T] = []
    try:
        for _retry, c, api, headers in http.retrying_client(on_retry=out.clear):
            page = 1
            hit_401 = False
            while True:
                r = c.get(
                    f"{api}{url_suffix}",
                    headers=headers,
                    params={
                        "per_page": 100,
                        "page": page,
                        **(params or {}),
                    },
                )
                if r.status_code == 401:
                    hit_401 = True
                    break
                r.raise_for_status()
                items: list[dict[str, Any]] = r.json()
                for item in items:
                    out.append(item_fn(item))
                if len(items) < 100:
                    break
                page += 1
            if hit_401:
                continue
            break
    except Exception:
        return fallback
    return out

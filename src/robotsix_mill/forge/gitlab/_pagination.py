"""Shared pagination helper for GitLab API endpoints.

Extracted from the monolithic ``gitlab.py`` so it can be reused across
the core, CI, code-scanning, and Dependabot sub-modules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from .._http import _ApiClient

T = TypeVar("T")


def _paginated_get(
    http: _ApiClient,
    url_suffix: str,
    *,
    params: dict[str, Any],
    item_fn: Callable[[dict[str, Any]], T],
) -> Iterator[T]:
    """Paginate through a GitLab API endpoint, yielding items via *item_fn*.

    Each dict from the JSON array response is passed to *item_fn*; the
    result is yielded.  Pagination stops when fewer than 100 items are
    returned (last page).
    """
    with http.client() as (c, api, headers):
        page = 1
        while True:
            r = c.get(
                f"{api}{url_suffix}",
                headers=headers,
                params={"per_page": 100, "page": page, **params},
            )
            r.raise_for_status()
            items: list[dict[str, Any]] = r.json()
            for item in items:
                yield item_fn(item)
            if len(items) < 100:
                break
            page += 1

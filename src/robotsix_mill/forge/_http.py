"""Lightweight HTTP transport wrapper shared by GitHub and GitLab forge
adapters.  Eliminates the repeated ``import httpx`` / auth-token / URL
preamble that was copied into every HTTP-calling method.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

import httpx

from ..config import RepoConfig, Settings


class _ApiClient:
    """Thin transport wrapper that owns the auth-headers and base-URL
    resolution so individual forge methods only supply the path and
    verb-specific kwargs.

    Every request opens a fresh ``httpx.Client(timeout=30)``, calls
    the configured ``headers_factory`` at request time (so the 50-min
    GitHub-App token cache and the GitLab secrets lookup stay fresh),
    and returns the raw ``httpx.Response`` — the **caller** is
    responsible for ``.raise_for_status()``, ``.json()``, ``.text``,
    and any status-code–specific branching.

    The ``client()`` context manager yields ``(httpx.Client, api_base,
    headers)`` for callers that need multiple requests inside one
    client lifecycle (retry loops, pagination, multi-step flows).
    """

    def __init__(
        self,
        settings: Settings,
        repo_config: RepoConfig | None,
        api_url_attr: str,
        headers_factory: Callable[[Settings, RepoConfig | None], dict[str, str]],
    ) -> None:
        self._settings = settings
        self._repo_config = repo_config
        self._api_url_attr = api_url_attr
        self._headers_factory = headers_factory

    # -- internal helper --------------------------------------------------

    def _do(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """Open a client, make a single request, buffer the body, and return
        the response.  *method* is the lower-case httpx verb (``"get"``,
        ``"post"``, …) — we call the corresponding method on the client so
        that test mocks which override only ``Client.get`` / ``Client.post``
        / … continue to intercept.
        """
        api_base = getattr(self._settings, self._api_url_attr).rstrip("/")
        headers = self._headers_factory(self._settings, self._repo_config)
        url = f"{api_base}{path}"

        with httpx.Client(timeout=30) as c:
            fn = getattr(c, method)
            r: httpx.Response = fn(url, headers=headers, **kwargs)
            # Buffer the body while the client is still open so the
            # caller can safely invoke .json() / .text after return.
            # Real httpx.Response has .read(); fake test responses
            # carry their payload pre-populated and don't need it.
            if hasattr(r, "read"):
                r.read()
        return r

    # -- public convenience wrappers --------------------------------------

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """Generic request — *method* is the HTTP verb (``"GET"``, …)."""
        return self._do(method.lower(), path, **kwargs)

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        """``GET <api_base><path>`` — returns the raw response."""
        return self._do("get", path, **kwargs)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        """``POST <api_base><path>`` — returns the raw response."""
        return self._do("post", path, **kwargs)

    def put(self, path: str, **kwargs: object) -> httpx.Response:
        """``PUT <api_base><path>`` — returns the raw response."""
        return self._do("put", path, **kwargs)

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        """``DELETE <api_base><path>`` — returns the raw response."""
        return self._do("delete", path, **kwargs)

    # -- context manager for multi-call flows -----------------------------

    @contextmanager
    def client(self) -> Iterator[tuple[httpx.Client, str, dict[str, str]]]:
        """Yield ``(httpx.Client, api_base_url, headers_dict)`` inside a live
        client context.  Use when you need several requests in one connection
        lifecycle (retry loops, pagination, multi-step flows).
        """
        api_base = getattr(self._settings, self._api_url_attr).rstrip("/")
        headers = self._headers_factory(self._settings, self._repo_config)
        with httpx.Client(timeout=30) as c:
            yield c, api_base, headers

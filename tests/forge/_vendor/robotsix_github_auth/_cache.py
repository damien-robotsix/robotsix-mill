"""In-process TTL cache for GitHub App installation tokens.

Thread-safe.  Keyed by ``(installation_id, frozen_scope_tuple)`` so that
different permission sets get separate entries.  Tokens are evicted when
their remaining lifetime drops below ``REFRESH_MARGIN_SECONDS`` (5 min).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_github_auth._models import InstallationToken

_REFRESH_MARGIN_SECONDS: float = 300.0


def _freeze_scopes(scopes: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Convert an optional scope mapping into a hashable, sort-stable key."""
    if scopes is None:
        return ()
    return tuple(sorted(scopes.items()))


class _TokenCache:
    """Thread-safe in-memory cache for installation tokens."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[tuple[str, tuple[tuple[str, str], ...]], InstallationToken] = {}

    def get(
        self,
        installation_id: str,
        scopes: Mapping[str, str] | None = None,
    ) -> InstallationToken | None:
        """Return a cached token if one exists and is still fresh enough."""
        key = (installation_id, _freeze_scopes(scopes))
        with self._lock:
            token = self._store.get(key)
            if token is None:
                return None
            if token.is_expired(margin_seconds=_REFRESH_MARGIN_SECONDS):
                return None
            return token

    def put(
        self,
        installation_id: str,
        scopes: Mapping[str, str] | None,
        token: InstallationToken,
    ) -> None:
        """Store a freshly minted token in the cache."""
        key = (installation_id, _freeze_scopes(scopes))
        with self._lock:
            self._store[key] = token

    def invalidate(self, installation_id: str) -> None:
        """Remove all cached tokens for the given installation."""
        with self._lock:
            keys_to_del = [k for k in self._store if k[0] == installation_id]
            for k in keys_to_del:
                del self._store[k]

    def clear(self) -> None:
        """Remove every cached entry."""
        with self._lock:
            self._store.clear()


_token_cache = _TokenCache()

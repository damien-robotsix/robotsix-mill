"""Offline unit tests for ``OpenRouterProvider.__init__`` auth resolution.

``OpenRouterProvider`` is an abstract base class (it declares the
``_tier_models`` abstractmethod), so these tests instantiate a minimal
concrete subclass that implements ``_tier_models``. ``__init__`` never
calls ``_tier_models``, so a trivial empty map is sufficient. All tests are
fully offline and key-free — each test sets or clears ``OPENROUTER_API_KEY``
explicitly via ``monkeypatch``.
"""

from __future__ import annotations

import pytest

from robotsix_llmio.core.provider import Tier
from robotsix_llmio.openrouter.provider import OpenRouterProvider


class _Concrete(OpenRouterProvider):
    """Minimal concrete provider so the ABC can be instantiated in tests."""

    def _tier_models(self) -> dict[Tier, str]:
        return {}


def test_missing_key_raises(monkeypatch):
    """With no explicit key and no env var, construction raises a clear
    ``RuntimeError`` naming the missing OpenRouter API key."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OpenRouter API key missing"):
        _Concrete(api_key=None)


def test_explicit_api_key_succeeds(monkeypatch):
    """An explicit ``api_key=`` is stored even when the env var is unset, and
    the default ``base_url`` is recorded."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    provider = _Concrete(api_key="sk-test")
    assert provider._api_key == "sk-test"
    assert provider._base_url == "https://openrouter.ai/api/v1"


def test_env_var_fallback_succeeds(monkeypatch):
    """When ``api_key`` is ``None`` the constructor falls back to the
    ``OPENROUTER_API_KEY`` environment variable."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    provider = _Concrete(api_key=None)
    assert provider._api_key == "sk-env"

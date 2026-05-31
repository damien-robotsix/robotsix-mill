"""Derived DeepSeek layer — pin + per-tier reasoning policy."""

from __future__ import annotations

import pytest

from robotsix_llmio.core.provider import Tier
from robotsix_llmio.openrouter_deepseek.provider import OpenRouterDeepseekProvider


def _model(tier: Tier):
    """Build a DeepSeek model for a tier with reasoning policy stamped (as the
    provider does), without needing network/key beyond construction."""
    pytest.importorskip("pydantic_ai.providers.openrouter")
    from robotsix_llmio.openrouter_deepseek.model import OpenRouterDeepseekModel

    name = {
        Tier.DEFAULT: "deepseek/deepseek-v4-pro",
        Tier.CHEAP: "deepseek/deepseek-v4-flash",
    }[tier]
    from pydantic_ai.providers.openrouter import OpenRouterProvider as _Pyd

    m = OpenRouterDeepseekModel(name, provider=_Pyd(api_key="x"))
    OpenRouterDeepseekProvider(api_key="x")._post_build_model(m, tier)
    return m


# --- pin + reasoning policy ------------------------------------------------


def test_default_tier_pins_and_xhigh():
    m = _model(Tier.DEFAULT)
    ms: dict = {}
    m._inject_pin((), {"model_settings": ms})
    assert ms["extra_body"]["provider"] == {
        "only": ["DeepSeek"],
        "allow_fallbacks": False,
    }
    assert ms["extra_body"]["reasoning"] == {"effort": "xhigh"}


def test_cheap_tier_pins_and_disables_reasoning():
    m = _model(Tier.CHEAP)
    ms: dict = {}
    m._inject_pin((), {"model_settings": ms})
    assert ms["extra_body"]["provider"]["only"] == ["DeepSeek"]
    assert ms["extra_body"]["reasoning"] == {"enabled": False}


def test_pin_respects_caller_provider_override():
    m = _model(Tier.DEFAULT)
    ms = {"extra_body": {"provider": {"only": ["Other"]}}}
    m._inject_pin((), {"model_settings": ms})
    assert ms["extra_body"]["provider"]["only"] == ["Other"]  # untouched

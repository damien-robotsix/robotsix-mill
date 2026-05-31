"""Derived DeepSeek layer — pin, per-tier reasoning, transient."""

from __future__ import annotations

import pytest

from robotsix_llmio.core.provider import Tier
from robotsix_llmio.openrouter_deepseek.provider import OpenRouterDeepseekProvider
from robotsix_llmio.openrouter_deepseek.transient import (
    is_deepseek_reasoning_roundtrip_error,
    is_deepseek_transient,
)


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


# --- transient -------------------------------------------------------------


def test_reasoning_roundtrip_400_detected():
    class HTTP400(Exception):
        status_code = 400

        def __str__(self):
            return "The reasoning_content in the thinking mode must be passed back."

    e = HTTP400()
    assert is_deepseek_reasoning_roundtrip_error(e) is True
    assert is_deepseek_transient(e) is True


def test_plain_400_not_transient():
    class HTTP400(Exception):
        status_code = 400

        def __str__(self):
            return "bad request"

    assert is_deepseek_reasoning_roundtrip_error(HTTP400()) is False
    assert is_deepseek_transient(HTTP400()) is False

"""OpenRouter transport provider — auth + per-tier model construction.

Model-family agnostic. A derived layer supplies the tier→model map (and any
quirks) by overriding the hooks. Knows nothing about reasoning policy.
"""

from __future__ import annotations

import os
from abc import abstractmethod
from typing import Any

from ..core import timeout_http_client
from ..core.provider import LLMProvider, Tier
from .model import OpenRouterModel
from .transient import is_openrouter_transient

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(LLMProvider):
    """Builds cost-instrumented OpenRouter models, one per tier.

    Subclasses MUST implement :meth:`_tier_models` (the baked tier→model map)
    and MAY override :meth:`_model_class` / :meth:`_post_build_model` to add
    provider-family quirks (pin, reasoning policy, …).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self._api_key:
            raise RuntimeError(
                "OpenRouter API key missing: pass api_key= or set OPENROUTER_API_KEY."
            )
        self._base_url = base_url

    # --- hooks for derived layers -------------------------------------------
    @abstractmethod
    def _tier_models(self) -> dict[Tier, str]:
        """Baked tier→model-name map (supplied by the derived layer)."""
        raise NotImplementedError

    def _model_class(self) -> type[OpenRouterModel]:
        """The OpenRouterModel subclass to instantiate (overridable)."""
        return OpenRouterModel

    def _post_build_model(self, model: OpenRouterModel, tier: Tier) -> None:
        """Hook to stamp per-tier policy onto a freshly built model. No-op here."""

    # --- core API -----------------------------------------------------------
    def new_model(self, tier: Tier = Tier.DEFAULT) -> tuple[Any, Any]:
        from pydantic_ai.providers.openrouter import (
            OpenRouterProvider as _PydOpenRouterProvider,
        )

        model_name = self._tier_models()[tier]
        http_client = timeout_http_client()
        pyd_provider = _PydOpenRouterProvider(
            api_key=self._api_key, http_client=http_client
        )
        model = self._model_class()(model_name, provider=pyd_provider)
        self._post_build_model(model, tier)
        return model, http_client

    def _is_transient(self, exc: BaseException) -> bool:
        return is_openrouter_transient(exc)

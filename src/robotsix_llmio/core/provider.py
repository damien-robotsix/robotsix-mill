"""Provider-agnostic base: the ``Tier`` enum and the ``LLMProvider`` ABC."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, TypeVar

from . import retry as _retry
from .agent import AgentHandle
from .agent import build_agent as _build_agent

T = TypeVar("T")


class Tier(str, Enum):
    """The only model selector a consumer chooses. A derived provider maps each
    tier to a concrete model + policy."""

    DEFAULT = "default"  # capable tier
    CHEAP = "cheap"  # fast/cheap tier


class LLMProvider(ABC):
    """Base for every provider. A derived provider implements :meth:`new_model`
    (and optionally :meth:`_is_transient`); the generic ``build_agent`` /
    ``call_with_retry`` are inherited."""

    @abstractmethod
    def new_model(self, tier: Tier = Tier.DEFAULT) -> tuple[Any, Any]:
        """Return ``(model, http_client)`` for *tier* — a fully configured
        pydantic-ai model (provider/auth/cost/quirks baked in) plus the http
        client to close when done."""
        raise NotImplementedError

    def _is_transient(self, exc: BaseException) -> bool:
        """Transient predicate for ``call_with_retry``. Override to widen with
        provider-specific signatures."""
        return _retry.is_transient(exc)

    def build_agent(
        self,
        *,
        tier: Tier = Tier.DEFAULT,
        system_prompt: str,
        tools: list | None = None,
        output_type: Any = str,
        name: str | None = None,
        retries: int = 2,
    ) -> AgentHandle:
        """Build a ready-to-run agent for *tier*. The caller supplies the final
        system prompt, tools, and output_type (domain concerns)."""
        model, http_client = self.new_model(tier)
        return _build_agent(
            model,
            http_client,
            system_prompt=system_prompt,
            tools=tools,
            output_type=output_type,
            name=name,
            retries=retries,
        )

    def call_with_retry(
        self,
        fn: Callable[[], T],
        *,
        what: str = "model call",
        sleep: Callable[[float], None] = time.sleep,
        fallback_fn: Callable[[], T] | None = None,
    ) -> T:
        """Run *fn* with bounded transient/rate-limit retry, using this
        provider's transient signatures."""
        return _retry.call_with_retry(
            fn,
            what=what,
            sleep=sleep,
            fallback_fn=fallback_fn,
            is_transient_fn=self._is_transient,
        )

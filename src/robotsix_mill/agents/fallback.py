"""A primary agent handle paired with a lazily-built fallback handle.

When mill routes an agent to the Claude SDK, ``build_agent`` wraps it in this so
a *terminal* Claude failure (after local retries are exhausted) can fall back to
the equivalent DeepSeek/OpenRouter build of the same agent — same system prompt,
tools, and output type. The orchestration lives in
:func:`robotsix_mill.agents.retry.run_agent`; this class just carries the
primary handle and a thunk that builds the fallback on demand.

The fallback is built lazily — only when actually needed — so the OpenRouter
client/key cost is paid only on a real fallback, and direct callers
(``.run_sync`` / ``.run`` / attribute access) always hit the primary.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("robotsix_mill.agents.fallback")


class FallbackAgentHandle:
    """Primary handle + lazy fallback builder.

    ``run_agent``/``arun_agent`` detect the fallback via the public
    ``fallback_builder`` attribute and call :meth:`build_fallback` only after the
    primary's local retries fail. Everything else delegates to the primary.
    """

    def __init__(self, primary: Any, fallback_builder: Callable[[], Any]) -> None:
        # Set on the instance dict before anything else so __getattr__ (which
        # reads self._primary) can't recurse during construction.
        self._primary = primary
        self._fallback: Any = None
        self.fallback_builder = fallback_builder

    def build_fallback(self) -> Any:
        """Build (once) and return the fallback handle."""
        if self._fallback is None:
            log.info("building fallback agent handle")
            self._fallback = self.fallback_builder()
        return self._fallback

    def run_sync(self, *args: Any, **kwargs: Any) -> Any:
        return self._primary.run_sync(*args, **kwargs)

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        return await self._primary.run(*args, **kwargs)

    def close(self) -> None:
        """Close the primary and the fallback (if it was ever built)."""
        for handle in (self._primary, self._fallback):
            close = getattr(handle, "close", None) if handle is not None else None
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 — close must never raise
                    pass

    def __getattr__(self, name: str) -> Any:
        # Reached only for names not defined on this wrapper → delegate to the
        # primary (e.g. result-shaping helpers some call sites poke at).
        return getattr(self._primary, name)

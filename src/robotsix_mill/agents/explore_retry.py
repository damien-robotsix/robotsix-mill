"""Retry primitives for the exploration sub-agent.

Extracted from ``explore.py`` to keep that module focused on the public
entry point (``run_explore``) and the tool factories.  This module owns
the self-contained pieces of the retry machinery:

- the budget-exhausted sentinel (``mark_/is_/reset_explore_budget_exhausted``),
- the outer-retry tuning constants, and
- a single explore attempt (``_run_single_explore_attempt``) plus its
  wall-clock budget helper (``_attempt_timeout``).

``explore.py`` re-exports the sentinel helpers so existing importers
(``coding.py``) and the monkeypatch seams in the tests keep resolving
them via ``robotsix_mill.agents.explore``.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..runtime.tracing import trace_stage

# --------------------------------------------------------------------------
# Budget-exhausted sentinel — set when the explore sub-agent exceeds its
# UsageLimits.request_limit even after a bounded retry.  ``coding.py``
# checks this after the coordinator run to escalate to BLOCKED.
# --------------------------------------------------------------------------

_explore_budget_exhausted: bool = False


def mark_explore_budget_exhausted() -> None:
    """Set the explore-budget-exhausted sentinel.

    Called by the explore sub-agent retry path when it exceeds
    ``UsageLimits.request_limit`` even after a bounded retry.
    ``coding.py`` checks this after the coordinator run.
    """
    global _explore_budget_exhausted
    _explore_budget_exhausted = True


def is_explore_budget_exhausted() -> bool:
    """Return whether the explore-budget-exhausted sentinel is set."""
    return _explore_budget_exhausted


def reset_explore_budget_exhausted() -> None:
    """Reset the explore-budget-exhausted sentinel for the next
    coordinator run.
    """
    global _explore_budget_exhausted
    _explore_budget_exhausted = False


# Number of outer retry attempts when the explore sub-agent fails with a
# non-transient (or exhausts its transient retries) error.  Each retry
# simplifies the question so the sub-agent has a better chance of
# completing before another connection hiccup.
_EXPLORE_MAX_ATTEMPTS = 3

# Maximum backoff delay (seconds) between outer explore retries.
_EXPLORE_BACKOFF_CAP = 30.0


async def _run_single_explore_attempt(
    *,
    agent: Any,
    prompt: str,
    limits: object | None,
    settings: Settings,
) -> str:
    """Run one explore agent attempt, handling truncation and budget exhaustion.

    Returns the explore output on success.  Raises on failure — the caller
    (``run_explore``) owns the outer retry loop.
    """
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    from .retry import acall_with_retry

    # ``limits`` is None on Claude tiers (the SDK tool loop warns on and
    # drops ``usage_limits``); only forward it when there is one.
    run_kwargs: dict[str, Any] = {} if limits is None else {"usage_limits": limits}

    with trace_stage("explore"):
        try:

            async def _call_explore() -> Any:
                return await agent.run(prompt, **run_kwargs)

            result = await acall_with_retry(
                _call_explore,
                what="explore",
            )
        except UsageLimitExceeded:
            # Budget exhausted — retry ONCE with a stricter prompt and
            # no tools.  The outer retry loop in run_explore handles
            # connection errors, not budget caps, so this fires on
            # every attempt.
            retry_agent = Agent(
                model=getattr(agent, "model", None),
                system_prompt=(
                    "You already exceeded your exploration budget on a "
                    "previous attempt. Return ONLY your single best answer "
                    "now — at most 3 file paths with one-line notes. Do "
                    "NOT call any tools. No speculation, no preamble. If "
                    "you cannot answer, say 'unable to answer'."
                ),
                output_type=str,
                tools=[],
                name="explore-retry",
                model_settings=getattr(agent, "model_settings", None),
            )
            retry_limits = UsageLimits(request_limit=2)
            try:
                retry_result = await retry_agent.run(
                    prompt,
                    usage_limits=retry_limits,
                )
            except UsageLimitExceeded:
                mark_explore_budget_exhausted()
                raise
            return str(retry_result.output).strip()

    # Detect truncation (finish_reason == 'length') and auto-continue
    # with a single follow-up call so the caller gets a complete answer.
    output = str(result.output).strip()
    finish_reason = getattr(getattr(result, "response", None), "finish_reason", None)
    if finish_reason == "length":
        all_msgs = getattr(result, "all_messages", None)
        try:
            history = all_msgs() if all_msgs is not None else None
        except TypeError:
            history = None
        if history is not None:
            continuation_result = await agent.run(
                "Continue exactly from where you were cut off. "
                "Do not repeat anything already said. "
                "Start from the last incomplete sentence.",
                message_history=history,
                **run_kwargs,
            )
        else:
            continuation_result = await agent.run(
                "Continue exactly from where you were cut off. "
                "Do not repeat anything already said. "
                "Start from the last incomplete sentence.",
                **run_kwargs,
            )
        output += "\n" + str(continuation_result.output).strip()

    return output


def _attempt_timeout(settings: Settings, *, on_claude: bool) -> float:
    """Wall-clock budget for one scout attempt.

    ``explore_timeout_seconds`` is sized for haiku on the Claude SDK. While
    provider failover has the scout's level on the OpenRouter slot the same
    prompt runs on deepseek-flash, whose tool turns are several times slower
    (2026-09-09: 51 of 74 killed attempts were fallback scouts that never
    finished in 90 s), so the fallback slot gets the configured multiple.
    """
    if on_claude:
        return float(settings.explore_timeout_seconds)
    return float(settings.explore_timeout_seconds) * float(
        settings.explore_fallback_timeout_factor
    )

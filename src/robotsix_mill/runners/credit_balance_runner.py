"""Proactive OpenRouter credit-balance poll.

Queries ``GET https://openrouter.ai/api/v1/credits``, computes
``remaining = total_credits - total_usage``, and sets or clears the
board-level low-credit warning via :mod:`~robotsix_mill.runtime.credit_status`.

Skips silently when no OpenRouter key is configured (the mill may be
running ``llm_backend: claude_sdk``).  On API failure the warning state
is left unchanged — only successful polls and the reactive 402 path
mutate it.

Key precedence: management key first, then API key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings, get_secrets

log = logging.getLogger("robotsix_mill.credit_balance")


@dataclass
class CreditBalanceResult:
    """What a credit-balance check produced."""

    balance_usd: float
    threshold_usd: float
    low: bool
    error: str | None = None


def check_credit_balance(
    settings: Settings | None = None,
) -> CreditBalanceResult:
    """Fetch the OpenRouter credit balance and compare against threshold.

    Returns a :class:`CreditBalanceResult` with ``low=True`` when the
    remaining balance is below *settings.low_credit_threshold_usd*.
    On missing keys or API errors the result carries ``error`` and the
    caller should NOT flip the warning state.
    """
    secrets = get_secrets()
    key = secrets.openrouter_management_key or secrets.openrouter_api_key
    if not key:
        log.debug("credit_balance: no OpenRouter key configured — skipping poll")
        return CreditBalanceResult(
            balance_usd=0.0,
            threshold_usd=0.0,
            low=False,
            error="no OpenRouter key configured",
        )

    threshold = settings.low_credit_threshold_usd if settings is not None else 5.0

    try:
        resp = httpx.get(
            "https://openrouter.ai/api/v1/credits",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        total_credits = float(data.get("total_credits", 0.0))
        total_usage = float(data.get("total_usage", 0.0))
        remaining = total_credits - total_usage

        low = remaining < threshold
        log.info(
            "credit_balance: remaining=$%.4f threshold=$%.2f low=%s",
            remaining,
            threshold,
            low,
        )
        return CreditBalanceResult(
            balance_usd=remaining,
            threshold_usd=threshold,
            low=low,
        )
    except Exception as exc:
        log.warning("credit_balance: API error — %s", exc)
        return CreditBalanceResult(
            balance_usd=0.0,
            threshold_usd=threshold,
            low=False,
            error=str(exc),
        )


def run_credit_balance_check(
    settings: Settings | None = None,
) -> CreditBalanceResult:
    """Top-level entry point for the poll-loop caller.

    Fetches the balance, updates the module-level credit-status state
    on success, and returns the result.
    """
    result = check_credit_balance(settings)

    if result.error is not None and "no OpenRouter key" not in result.error:
        # API failure — leave state unchanged.
        return result

    if result.error is not None:
        # No key — nothing to do.
        return result

    from ..runtime.credit_status import record_balance_low, record_balance_ok

    if result.low:
        detail = (
            f"OpenRouter balance ${result.balance_usd:.2f} "
            f"below threshold ${result.threshold_usd:.2f}"
        )
        record_balance_low(
            balance_usd=result.balance_usd,
            threshold_usd=result.threshold_usd,
            detail=detail,
        )
    else:
        record_balance_ok(
            balance_usd=result.balance_usd,
            threshold_usd=result.threshold_usd,
        )
    return result

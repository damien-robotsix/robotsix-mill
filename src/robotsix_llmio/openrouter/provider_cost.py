"""OpenRouter implementation of the :class:`ProviderCostSource` read seam.

The **only** module that knows the OpenRouter *activity* (management) API on
the provider-cost path. Self-contained: depends only on ``httpx`` and the
public REST API — no pydantic-ai, no OTel.

``OpenRouterProviderCostSource`` reads OpenRouter's billed spend for a window
via ``GET /api/v1/activity?date=YYYY-MM-DD``. That endpoint reports per-UTC-day,
per-model spend, so a window is summed across each UTC day it covers (the
reconciliation use case passes a single settled day). Requires a
**management** key (not a normal inference key); credentials are always passed
in explicitly — the adapter reads no env vars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from ..core.cost_log import CostWindow
from ..core.provider_cost import ProviderCost

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_TIMEOUT = 20


@dataclass(frozen=True)
class KeyUsage:
    """An OpenRouter key's CUMULATIVE (lifetime) usage, from ``auth/key``.

    *usage* is total USD spent on this key since creation; *limit* is the
    credit ceiling (``None`` = unlimited). Cumulative, not windowed — a
    per-day per-key cost is the consumer's daily snapshot diff.
    """

    usage: float
    limit: float | None = None
    label: str | None = None


class OpenRouterKeyCostSource:
    """Per-KEY cumulative usage from OpenRouter's ``GET /api/v1/auth/key``.

    Reads the usage of the *authenticating inference key* (not the account
    total). Two uses: (1) per-project reconciliation when each project has its
    own key — snapshot ``fetch_key_usage`` daily and diff to get a window's
    spend; (2) an isolated live test — read usage, make a known call, read the
    delta. Credentials passed in explicitly; reads no env vars.
    """

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self._key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    def fetch_key_usage(self) -> KeyUsage:
        """Current cumulative usage of the authenticating key.

        Raises ``RuntimeError`` on any non-2xx response.
        """
        url = f"{self._base_url}/auth/key"
        headers = {"Authorization": f"Bearer {self._key}"}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, headers=headers)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(
                f"OpenRouter auth/key request failed: "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json().get("data") or {}
        limit = data.get("limit")
        return KeyUsage(
            usage=float(data.get("usage", 0) or 0),
            limit=None if limit is None else float(limit),
            label=data.get("label"),
        )


class OpenRouterProviderCostSource:
    """Provider-billed cost from OpenRouter's activity API.

    Implements :class:`~robotsix_llmio.core.provider_cost.ProviderCostSource`.
    """

    def __init__(
        self,
        *,
        management_key: str,
        base_url: str | None = None,
    ) -> None:
        self._key = management_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    def fetch_provider_cost(self, window: CostWindow) -> ProviderCost:
        """Sum OpenRouter's billed spend across every UTC day *window* covers.

        Raises ``RuntimeError`` on any non-2xx response rather than silently
        returning zero (a silent zero would read as "all spend unlogged").
        """
        total = 0.0
        breakdown: dict[str, float] = {}
        request_count = 0
        headers = {"Authorization": f"Bearer {self._key}"}
        url = f"{self._base_url}/activity"

        with httpx.Client(timeout=_TIMEOUT) as client:
            for date_str in _utc_dates(window):
                resp = client.get(url, params={"date": date_str}, headers=headers)
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(
                        f"OpenRouter activity request failed for {date_str}: "
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                data = resp.json().get("data") or []
                for entry in data:
                    usage = float(entry.get("usage", 0) or 0)
                    byok = float(entry.get("byok_usage_inference", 0) or 0)
                    sub_total = usage + byok
                    total += sub_total
                    model = str(entry.get("model", "unknown"))
                    breakdown[model] = breakdown.get(model, 0.0) + sub_total
                    request_count += int(entry.get("num_requests", 0) or 0)

        return ProviderCost(
            total_cost=total,
            breakdown=breakdown,
            request_count=request_count,
        )


def _as_utc(dt: datetime) -> datetime:
    """Normalize *dt* to UTC, treating a naive datetime as already-UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _utc_dates(window: CostWindow) -> list[str]:
    """Every UTC date (``YYYY-MM-DD``) whose midnight falls before *window.end*,
    starting at *window.start*'s date. For a single settled day this is one
    date; the *end* is exclusive."""
    start = _as_utc(window.start)
    end = _as_utc(window.end)
    out: list[str] = []
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    while day < end:
        out.append(day.date().isoformat())
        day += timedelta(days=1)
    return out

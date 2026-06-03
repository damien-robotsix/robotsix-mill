"""Backend-neutral read seam for PROVIDER-billed cost, plus reconciliation.

The counterpart to :mod:`robotsix_llmio.core.cost_log`: where ``cost_log``
reads what *we* logged (per-call cost stamped on spans, read back from a log
backend), this module reads what the *provider* actually billed, and
:func:`reconcile` compares the two over a window.

Providers (OpenRouter, …) implement :class:`ProviderCostSource`; the consumer
depends only on the protocol, never on a concrete provider. :func:`reconcile`
is pure — no I/O — so it is trivially unit-testable; the consumer owns policy
(threshold reaction, retention-window selection, scheduling).

No backend import, no httpx, no network in this module — pure types + a pure
comparison. Provider adapters live in their transport package
(e.g. :mod:`robotsix_llmio.openrouter.provider_cost`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .cost_log import CostWindow, LoggedCost

# Flat absolute tolerance (in the cost unit the backends report — USD for
# OpenRouter/Langfuse). A window whose logged-vs-provider gap is within this is
# considered reconciled. Deliberately NOT relative: a flat band is simpler to
# reason about and avoids tiny-window noise.
DEFAULT_TOLERANCE = 1.0


@dataclass(frozen=True)
class ProviderCost:
    """Provider-billed cost for a window.

    *breakdown* maps a provider-defined label (e.g. model id) to its cost;
    *request_count* is the provider's own request tally when exposed (0 when
    not). *total_cost* is authoritative — *breakdown* is for diagnostics.
    """

    total_cost: float
    breakdown: dict[str, float] = field(default_factory=dict)
    request_count: int = 0


@runtime_checkable
class ProviderCostSource(Protocol):
    """Backend-neutral read interface for provider-billed cost over a window."""

    def fetch_provider_cost(self, window: CostWindow) -> ProviderCost: ...


@dataclass(frozen=True)
class Discrepancy:
    """Outcome of reconciling logged cost against provider-billed cost."""

    logged_total: float
    provider_total: float
    delta: float  # abs(provider_total - logged_total)
    within_tolerance: bool
    tolerance: float


def reconcile(
    logged: LoggedCost,
    provider: ProviderCost,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> Discrepancy:
    """Compare *logged* (what we recorded) against *provider* (what was billed).

    Pure window-total reconciliation: the absolute delta of the two totals,
    flagged ``within_tolerance`` when ``delta <= tolerance``. Per-generation
    alignment is intentionally out of scope until the cost recorder captures
    provider generation ids (today ``CostRecord.id`` is the log backend's trace
    id, which doesn't map to a provider line-item).

    The caller owns the *retention guard*: only reconcile windows still inside
    the log backend's retention horizon — a window older than retention reads
    back as ~0 logged cost (pruned) and would look like a huge false gap.
    """
    delta = abs(provider.total_cost - logged.total_cost)
    return Discrepancy(
        logged_total=logged.total_cost,
        provider_total=provider.total_cost,
        delta=delta,
        within_tolerance=delta <= tolerance,
        tolerance=tolerance,
    )

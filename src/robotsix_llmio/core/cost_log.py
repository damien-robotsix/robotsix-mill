"""Backend-neutral read seam for *logged* cost/trace data.

This is the read-side counterpart to the OTLP write seam in
:mod:`robotsix_llmio.core.tracing`. Where the write side stamps per-call cost
onto OTel spans and ships them to a backend over pure OTLP, this module defines
the neutral types and protocol a consumer depends on to read that cost back —
without coupling to any particular log backend.

Backends (Langfuse, SQL, …) implement :class:`CostLogSource`; the consumer
depends only on the protocol, never on a concrete backend. The Langfuse
implementation lives in :mod:`robotsix_llmio.core.langfuse_cost`.

No backend import, no httpx, no network in this module — it is pure types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CostWindow:
    """A time window to query logged cost for.

    *start* is treated as inclusive and *end* as exclusive; adapters serialize
    these as ISO-8601 timestamps for the backend.
    """

    start: datetime
    end: datetime


@dataclass(frozen=True)
class CostRecord:
    """One logged unit of cost (one trace).

    *session_id* and *name* are populated when the backend exposes them.
    """

    id: str
    cost: float
    timestamp: datetime
    session_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class LoggedCost:
    """Aggregate result of a logged-cost query.

    *records* is the per-call/per-trace breakdown backing *total_cost*.
    """

    total_cost: float
    record_count: int
    records: list[CostRecord] = field(default_factory=list)


@runtime_checkable
class CostLogSource(Protocol):
    """Backend-neutral read interface for logged cost.

    A backend adapter implements this single method to expose logged cost over
    a time window. Consumers depend on this protocol, not on any concrete
    backend.
    """

    def fetch_logged_cost(self, window: CostWindow) -> LoggedCost: ...

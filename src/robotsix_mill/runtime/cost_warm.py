"""On-demand cost-cache warming, driven by the board's ``/tickets`` poll.

Replaces the old always-on ``cost_warmer`` background daemon. The board's
own list poll schedules a fire-and-forget warm of the rows it just returned
(as a FastAPI ``BackgroundTask``, after the response is sent), so the cost
column fills in within a poll cycle — but only for the tickets actually
shown, only while someone is watching the board, and with no perpetual
backend loop hammering Langfuse for every open ticket forever.

The ``/tickets`` list endpoint serves cost cache-only (``session_cost_cached``,
no HTTP) to stay fast; this refreshes that cache so the next poll is warm.
``session_cost`` already TTL-caches, so warming an already-fresh ticket is a
no-op — only cold/stale ids cost a Langfuse call.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

log = logging.getLogger("robotsix_mill.runtime.cost_warm")

# Process-wide: only one warm runs at a time. Overlapping board polls would
# otherwise pile up threads; a skipped poke is harmless because the in-flight
# warm (or the next poke) covers the same ids and the TTL cache dedupes work.
_warm_lock = threading.Lock()
_WARM_WORKERS = 4


def warm_ticket_costs(settings: Any, items: Sequence[tuple[str, Any]]) -> None:
    """Refresh the per-ticket Langfuse cost cache for *items*.

    *items* is ``[(ticket_id, repo_config), ...]``. Best-effort: intended to
    run as a ``BackgroundTask`` after the list response is sent. No-op when a
    warm is already in flight (non-blocking lock) or *items* is empty.
    """
    if not items:
        return
    if not _warm_lock.acquire(blocking=False):
        return  # a warm is already running — it (or the next poke) covers this
    try:
        from ..langfuse.client import session_cost

        def _one(item: tuple[str, Any]) -> None:
            ticket_id, repo_config = item
            try:
                session_cost(settings, ticket_id, repo_config=repo_config)
            except Exception:  # noqa: BLE001 — best-effort cache warm
                log.debug("cost warm: lookup failed for %s", ticket_id, exc_info=True)

        with ThreadPoolExecutor(max_workers=_WARM_WORKERS) as ex:
            list(ex.map(_one, items))
    finally:
        _warm_lock.release()

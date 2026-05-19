"""Read-side Langfuse helper: fetch a compact summary of the traces for
a ticket's session (mill sets ``session.id = ticket.id``), and list all
traces in a time window for health checks.

Used by the retrospect stage, the trace-health runner, and the
periodic cost-sync loop. Fully graceful: returns ``None`` / ``[]``
when Langfuse isn't configured or the API errors — callers degrade
without failing.
"""

from __future__ import annotations

import base64
import logging
import time

from .config import Settings

log = logging.getLogger("robotsix_mill.langfuse_client")

# Short in-memory cache for read-time cost lookups. Per-ticket cost is
# read straight from the Langfuse session on demand (the board polls
# often); this TTL keeps that to one Langfuse call per ticket per
# minute instead of one per render. Process-local, best-effort.
_COST_TTL_SECONDS = 60.0
_cost_cache: dict[str, tuple[float, float]] = {}  # id -> (cost, monotonic)


def _langfuse_api_get(settings: Settings, path: str, params: dict | None = None):
    """Low-level authenticated GET to the Langfuse public API.

    Returns the JSON-decoded response body, or ``None`` when Langfuse is
    unconfigured / unreachable / the request fails."""
    if not settings.tracing_enabled:
        return None
    host = (settings.langfuse_base_url or "").rstrip("/")
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    try:
        import httpx

        with httpx.Client(timeout=20) as c:
            r = c.get(
                f"{host}{path}",
                params=params or {},
                headers={"Authorization": f"Basic {auth}"},
            )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 — analysis must not fail the caller
        return None


def session_total_cost(settings: Settings, session_id: str) -> float | None:
    """Return the total USD cost for a Langfuse session (sum of
    ``totalCost`` across all its traces), or ``None`` when Langfuse
    is unconfigured / unreachable / returns no data."""
    data = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
    )
    if data is None:
        return None
    traces = data.get("data", [])
    total = 0.0

    def _num(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    for t in traces:
        total += _num(t.get("totalCost"))
    return total


def session_cost(settings: Settings, session_id: str) -> float:
    """Read-time per-ticket cost: the Langfuse session total, cached for
    ``_COST_TTL_SECONDS``. Always returns a number (0.0 when Langfuse is
    unconfigured / unreachable / has no data) so callers never special-
    case None. This replaces the old persisted ``cost_usd`` + sync loop:
    cost lives in Langfuse; we just read and briefly cache it."""
    now = time.monotonic()
    hit = _cost_cache.get(session_id)
    if hit is not None and (now - hit[1]) < _COST_TTL_SECONDS:
        return hit[0]
    cost = session_total_cost(settings, session_id)
    if cost is None:
        # Don't poison the cache with a transient failure — serve the
        # last known value if we have one, else 0.0 (uncached so the
        # next read retries Langfuse).
        return hit[0] if hit is not None else 0.0
    _cost_cache[session_id] = (cost, now)
    return cost


def fetch_session_summary(settings: Settings, session_id: str) -> str | None:
    """Return a short text summary of the session's traces (count,
    total cost, total latency, per-trace lines), or ``None`` if Langfuse
    is unconfigured / unreachable."""
    data = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
    )
    if data is None:
        return None
    traces = data.get("data", [])
    if not traces:
        return "(no Langfuse traces found for this session)"

    def num(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    total_cost = sum(num(t.get("totalCost")) for t in traces)
    total_lat = sum(num(t.get("latency")) for t in traces)
    lines = [
        f"traces={len(traces)}  total_cost=${total_cost:.4f}  "
        f"total_latency={total_lat:.1f}s",
    ]
    for t in traces[:40]:
        lines.append(
            f"- {t.get('name', '?')}  "
            f"${num(t.get('totalCost')):.4f}  "
            f"{num(t.get('latency')):.1f}s  "
            f"obs={len(t.get('observations') or [])}"
        )
    return "\n".join(lines)


def list_all_traces_since(
    settings: Settings, from_timestamp: str
) -> list[dict]:
    """Return every trace created at or after *from_timestamp* by
    paginating the Langfuse public API.

    Returns an empty list (never crashes) when Langfuse is unconfigured
    or any HTTP / JSON error occurs — the caller must treat ``[]`` as
    "no data available."
    """
    if not settings.tracing_enabled:
        return []
    host = (settings.langfuse_base_url or "").rstrip("/")
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    try:
        import httpx

        all_traces: list[dict] = []
        page = 1
        with httpx.Client(timeout=30) as c:
            while True:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": 50,
                        "page": page,
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "Langfuse trace list returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    return []
                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)
                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1
        return all_traces
    except Exception:  # noqa: BLE001 — never crash the caller
        log.exception("failed to list Langfuse traces since %s", from_timestamp)
        return []

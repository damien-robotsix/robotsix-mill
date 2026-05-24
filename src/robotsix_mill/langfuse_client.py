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
from datetime import datetime, timedelta

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


def session_cost_cached(session_id: str) -> float:
    """Non-blocking cost lookup: return the cached value if any, else
    0.0. NEVER hits the network. Use this in hot paths like the board's
    /tickets list, which polls every 5s; with N tickets cold the full
    ``session_cost`` would issue N Langfuse HTTP calls and block the
    response for seconds, long enough that the next poll tick cancels
    its predecessor. Per-ticket detail GETs still use the full
    ``session_cost`` to keep the drawer authoritative."""
    hit = _cost_cache.get(session_id)
    if hit is None:
        return 0.0
    return hit[0]


def fetch_trace_detail(settings: Settings, trace_id: str) -> dict | None:
    """Fetch a single trace by ID from the Langfuse API.

    Returns the JSON-decoded response body, or ``None`` on failure
    (including when Langfuse is unconfigured).
    """
    return _langfuse_api_get(settings, f"/api/public/traces/{trace_id}")



def fetch_session_summary(settings: Settings, session_id: str) -> str | None:
    """Return a short text summary of the session's traces grouped by
    stage, with per-stage cost/latency/observation subtotals and a
    ``## Warnings/Errors`` section sourced from per-trace detail calls.

    Returns ``None`` if Langfuse is unconfigured / unreachable.
    """
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

    # --- group by stage name -------------------------------------------
    from collections import defaultdict

    stages: dict[str, list[dict]] = defaultdict(list)
    for t in traces:
        stages[t.get("name", "?")].append(t)

    lines.append("")
    lines.append("## By stage")
    for stage_name in sorted(stages):
        stage_traces = stages[stage_name]
        stage_cost = sum(num(t.get("totalCost")) for t in stage_traces)
        stage_lat = sum(num(t.get("latency")) for t in stage_traces)
        stage_obs = sum(
            len(t.get("observations") or []) for t in stage_traces
        )
        lines.append(
            f"- {stage_name}: "
            f"${stage_cost:.4f}  "
            f"{stage_lat:.1f}s  "
            f"obs={stage_obs}"
        )

    # --- per-trace detail: collect warnings / errors -------------------
    MAX_WARNINGS = 20
    warnings_errors: list[str] = []
    for t in traces:
        trace_id = t.get("id")
        if not trace_id:
            continue
        detail = fetch_trace_detail(settings, trace_id)
        if detail is None:
            continue
        observations = detail.get("observations") or []
        for obs in observations:
            level = obs.get("level")
            if level in ("WARNING", "ERROR"):
                msg = obs.get("statusMessage", "")
                warnings_errors.append(
                    f"- {t.get('name', '?')} [{level}] {msg}"
                )

    if len(warnings_errors) > MAX_WARNINGS:
        omitted = len(warnings_errors) - MAX_WARNINGS
        warnings_errors = warnings_errors[:MAX_WARNINGS]
        warnings_errors.append(f"(+{omitted} more warnings/errors not shown)")

    if warnings_errors:
        lines.append("")
        lines.append("## Warnings/Errors")
        lines.extend(warnings_errors)

    return "\n".join(lines)


def list_recent_traces(
    settings: Settings,
    limit: int = 10,
    min_cost: float | None = None,
    max_cost: float | None = None,
) -> list[dict]:
    """Return up to *limit* most-recent traces from Langfuse, ordered by
    timestamp descending. Optionally filter by totalCost (inclusive).

    When neither *min_cost* nor *max_cost* is provided, fetches exactly
    *limit* traces (no extra API cost — current behaviour preserved).

    When a cost filter is active, fetches ``max(limit * 5, 50)`` traces
    to increase the chance of finding matches after filtering, then
    applies the cost filter in Python and returns at most *limit*.

    Returns an empty list (never crashes) when Langfuse is unconfigured
    or any HTTP / JSON error occurs — the caller must treat ``[]`` as
    "no data available."
    """
    cost_filter_active = min_cost is not None or max_cost is not None

    def _named(t: dict) -> bool:
        """A trace is 'ready for review' iff its root span has closed
        and propagated a name. In-flight traces show as unnamed/null
        until completion — they shouldn't appear in the picker since
        deep review can't analyse a partial observation tree anyway."""
        n = t.get("name")
        return isinstance(n, str) and n.strip() != ""

    # No cost filter: single fetch, drop unnamed.
    if not cost_filter_active:
        # Over-fetch a bit so dropping unnamed still gives us ``limit``
        # named ones in the common case (named traces are dominant).
        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={"orderBy": "timestamp.desc", "limit": min(limit * 2, 100)},
        )
        if data is None:
            return []
        return [t for t in data.get("data", []) if _named(t)][:limit]

    # Cost filter active: paginate so the cost filter is applied to ALL
    # recent traces (in chronological order) until we have ``limit``
    # matches, instead of being applied AFTER a single capped fetch.
    # Two bugs the previous single-shot logic had:
    #   1. Asking Langfuse for limit*5 traces sent >100 once limit≥21,
    #      and Langfuse's /api/public/traces caps limit at 100 — request
    #      returned HTTP 400, the function returned [], the UI showed
    #      "no traces" the moment the user set Show > 20 with a filter.
    #   2. Filter was applied AFTER the capped fetch, so matches further
    #      back in time were never even examined.
    # Paginate in pages of 100 (Langfuse's max), bounded by
    # ``examine_cap`` so a too-strict filter can't paginate forever.
    def _cost(t: dict) -> float:
        try:
            return float(t.get("totalCost") or 0)
        except (TypeError, ValueError):
            return 0.0

    PAGE_SIZE = 100
    examine_cap = max(limit * 20, 500)  # don't scan more than this
    filtered: list[dict] = []
    examined = 0
    page = 1
    while len(filtered) < limit and examined < examine_cap:
        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={
                "orderBy": "timestamp.desc",
                "limit": PAGE_SIZE,
                "page": page,
            },
        )
        if data is None:
            break  # API failed — return what we have
        traces = data.get("data", [])
        if not traces:
            break  # exhausted Langfuse's history
        for t in traces:
            examined += 1
            if not _named(t):
                continue  # in-flight trace — skip
            c = _cost(t)
            if min_cost is not None and c < min_cost:
                continue
            if max_cost is not None and c > max_cost:
                continue
            filtered.append(t)
            if len(filtered) >= limit:
                break
        page += 1

    return filtered


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


def aggregate_cost_trend(
    settings: Settings,
    lookback_hours: float = 24,
) -> list[dict]:
    """Return cost bucketed by time for the last *lookback_hours*.

    - Bucket width: 1 hour when lookback ≤ 24, 1 day when > 24.
    - Each bucket has ``ts`` (ISO-8601 start), ``total_cost``, and
      ``trace_count``.
    - Examines at most 500 traces to bound API calls.

    Graceful: returns ``[]`` when tracing is disabled or the API errors.
    """
    if not settings.tracing_enabled:
        return []

    from_timestamp = (
        datetime.utcnow() - timedelta(hours=lookback_hours)
    ).isoformat() + "Z"

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    host = (settings.langfuse_base_url or "").rstrip("/")

    PAGE_SIZE = 100
    EXAMINE_CAP = 500
    all_traces: list[dict] = []
    api_ok = False  # distinguish "API error" from "no traces in window"

    try:
        import httpx

        page = 1
        with httpx.Client(timeout=20) as c:
            while len(all_traces) < EXAMINE_CAP:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": PAGE_SIZE,
                        "page": page,
                        "orderBy": "timestamp.desc",
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "aggregate_cost_trend: Langfuse returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    break

                api_ok = True
                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)

                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1

    except Exception:
        log.exception("aggregate_cost_trend failed")
        return []

    # API error → return [] so the frontend shows the empty state.
    if not api_ok:
        return []

    # Truncate to cap
    traces = all_traces[:EXAMINE_CAP]

    # Determine bucket width
    if lookback_hours <= 24:
        # Hourly buckets
        bucket_delta = timedelta(hours=1)
        num_buckets = int(lookback_hours)  # ceil handled by range
        if lookback_hours != int(lookback_hours):
            num_buckets = int(lookback_hours) + 1
        now = datetime.utcnow()
        bucket_starts = [
            (now - timedelta(hours=lookback_hours)) + bucket_delta * i
            for i in range(num_buckets)
        ]
        ts_fmt = lambda dt: dt.replace(minute=0, second=0, microsecond=0).isoformat() + "Z"
    else:
        # Daily buckets
        bucket_delta = timedelta(days=1)
        num_days = int(lookback_hours / 24)
        if lookback_hours % 24 != 0:
            num_days += 1
        now = datetime.utcnow()
        start = now - timedelta(hours=lookback_hours)
        bucket_starts = [
            (start + bucket_delta * i).replace(hour=0, minute=0, second=0, microsecond=0)
            for i in range(num_days)
        ]
        ts_fmt = lambda dt: dt.isoformat() + "Z"

    # Initialize buckets
    buckets: dict[str, dict] = {}
    for bs in bucket_starts:
        key = ts_fmt(bs)
        buckets[key] = {"ts": key, "total_cost": 0.0, "trace_count": 0}

    # Sort bucket keys for boundary lookups
    bucket_keys = sorted(buckets.keys())

    # Assign traces to buckets
    for t in traces:
        ts_str = t.get("timestamp")
        if not ts_str:
            continue
        try:
            # Parse timestamp; Langfuse returns ISO-8601
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            # Replace tzinfo to naive for comparison with naive bucket boundaries
            ts_naive = ts.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue

        cost = float(t.get("totalCost") or 0)

        # Find the right bucket: the last bucket whose start <= trace timestamp
        assigned = None
        for key in bucket_keys:
            bucket_dt = datetime.fromisoformat(
                key.replace("Z", "+00:00")
            ).replace(tzinfo=None)
            if bucket_dt <= ts_naive:
                assigned = key
            else:
                break

        if assigned is not None:
            buckets[assigned]["total_cost"] += cost
            buckets[assigned]["trace_count"] += 1

    # Return all buckets including empty ones for a continuous x-axis
    return [buckets[k] for k in bucket_keys]


def aggregate_cost_by_name(
    settings: Settings,
    lookback_hours: float = 24,
) -> list[dict]:
    """Return cost aggregated by trace name for Langfuse traces within
    the last *lookback_hours*.

    Graceful: returns ``[]`` when tracing is disabled or the API errors.
    Examines at most 500 traces to bound API calls.
    """
    if not settings.tracing_enabled:
        return []

    from_timestamp = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat() + "Z"

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    host = (settings.langfuse_base_url or "").rstrip("/")

    PAGE_SIZE = 100
    EXAMINE_CAP = 500
    all_traces: list[dict] = []

    try:
        import httpx

        page = 1
        with httpx.Client(timeout=20) as c:
            while len(all_traces) < EXAMINE_CAP:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": PAGE_SIZE,
                        "page": page,
                        "orderBy": "timestamp.desc",
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "aggregate_cost_by_name: Langfuse returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    break

                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)

                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1

    except Exception:
        log.exception("aggregate_cost_by_name failed")
        return []

    # Truncate to cap
    traces = all_traces[:EXAMINE_CAP]

    # Aggregate by name
    agg: dict[str, dict] = {}
    for t in traces:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        cost = float(t.get("totalCost") or 0)
        if name not in agg:
            agg[name] = {"total_cost": 0.0, "trace_count": 0}
        agg[name]["total_cost"] += cost
        agg[name]["trace_count"] += 1

    result = [
        {"name": name, "total_cost": entry["total_cost"], "trace_count": entry["trace_count"]}
        for name, entry in agg.items()
    ]
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return result


def most_expensive_ticket(
    settings: Settings,
    lookback_hours: float = 24,
) -> dict | None:
    """Return the session with the highest total cost within the last
    *lookback_hours*.

    Groups traces by ``sessionId``, sums ``totalCost`` per session,
    and returns the single session with the highest total cost.

    Graceful: returns ``None`` when tracing is disabled or the API
    errors.  Examines at most 500 traces to bound API calls.
    """
    if not settings.tracing_enabled:
        return None

    from_timestamp = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat() + "Z"

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    host = (settings.langfuse_base_url or "").rstrip("/")

    PAGE_SIZE = 100
    EXAMINE_CAP = 500
    all_traces: list[dict] = []

    try:
        import httpx

        page = 1
        with httpx.Client(timeout=20) as c:
            while len(all_traces) < EXAMINE_CAP:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": PAGE_SIZE,
                        "page": page,
                        "orderBy": "timestamp.desc",
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "most_expensive_ticket: Langfuse returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    break

                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)

                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1

    except Exception:
        log.exception("most_expensive_ticket failed")
        return None

    # Aggregate by session_id
    agg: dict[str, dict] = {}
    for t in all_traces[:EXAMINE_CAP]:
        sid = (t.get("sessionId") or "").strip()
        if not sid:
            continue
        cost = float(t.get("totalCost") or 0)
        if sid not in agg:
            agg[sid] = {"total_cost": 0.0, "trace_count": 0}
        agg[sid]["total_cost"] += cost
        agg[sid]["trace_count"] += 1

    if not agg:
        return None

    # Pick the session with the highest total cost
    best_sid, best = max(agg.items(), key=lambda item: item[1]["total_cost"])
    return {
        "session_id": best_sid,
        "total_cost": best["total_cost"],
        "trace_count": best["trace_count"],
    }


def most_expensive_trace(
    settings: Settings,
    lookback_hours: float = 24,
) -> dict | None:
    """Return the single trace with the highest ``totalCost`` within the
    last *lookback_hours*.

    Skips unnamed/in-flight traces (same ``_named`` filter as
    ``list_recent_traces``).

    Graceful: returns ``None`` when tracing is disabled or the API
    errors.  Examines at most 500 traces to bound API calls.
    """
    if not settings.tracing_enabled:
        return None

    from_timestamp = (datetime.utcnow() - timedelta(hours=lookback_hours)).isoformat() + "Z"

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    host = (settings.langfuse_base_url or "").rstrip("/")

    PAGE_SIZE = 100
    EXAMINE_CAP = 500
    all_traces: list[dict] = []

    try:
        import httpx

        page = 1
        with httpx.Client(timeout=20) as c:
            while len(all_traces) < EXAMINE_CAP:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": PAGE_SIZE,
                        "page": page,
                        "orderBy": "timestamp.desc",
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "most_expensive_trace: Langfuse returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    break

                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)

                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1

    except Exception:
        log.exception("most_expensive_trace failed")
        return None

    # Find the single named trace with highest cost
    best_trace: dict | None = None
    best_cost = -1.0

    for t in all_traces[:EXAMINE_CAP]:
        name = t.get("name")
        if not (isinstance(name, str) and name.strip() != ""):
            continue
        cost = float(t.get("totalCost") or 0)
        if cost > best_cost:
            best_cost = cost
            best_trace = t

    if best_trace is None:
        return None

    return {
        "id": best_trace.get("id", ""),
        "name": best_trace.get("name", ""),
        "total_cost": best_cost,
        "timestamp": best_trace.get("timestamp", ""),
        "session_id": best_trace.get("sessionId") or None,
    }

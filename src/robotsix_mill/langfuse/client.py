"""Read-side Langfuse helper: fetch a compact summary of the traces for
a ticket's session (mill sets ``session.id = ticket.id``), and list all
traces in a time window for health checks.

Used by the retrospect stage, the trace-health runner, and the
periodic cost-sync loop. Fully graceful: returns ``None`` / ``[]``
when Langfuse isn't configured or the API errors — callers degrade
without failing.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from robotsix_llmio.core import LangfuseReadClient

from ..config import RepoConfig, Settings, get_secrets

log = logging.getLogger("robotsix_mill.langfuse.client")

# Short in-memory cache for read-time cost lookups. Per-ticket cost is
# read straight from the Langfuse session on demand (the board polls
# often); this TTL keeps that to one Langfuse call per ticket per
# minute instead of one per render. Process-local, best-effort.
_COST_TTL_SECONDS = 60.0
_cost_cache: dict[str, tuple[float, float]] = {}  # id -> (cost, monotonic)


def _build_read_client(
    settings: Settings, repo_config: RepoConfig | None = None
) -> LangfuseReadClient | None:
    """Build a :class:`LangfuseReadClient` from mill's credential sources,
    or ``None`` when Langfuse is unconfigured.

    The shared client (``robotsix_llmio.core``) owns the Langfuse REST
    read-protocol kernel — Basic auth, base-URL default, and paginated
    GETs.  Mill only decides *which* credentials to feed it: a per-repo
    override when *repo_config* is given, else the global
    :class:`Secrets` singleton (kept for backward compatibility during
    the transition to per-repo credentials)."""
    if repo_config is None:
        if not settings.tracing_enabled:
            return None
        secrets = get_secrets()
        public_key = secrets.langfuse_public_key
        secret_key = secrets.langfuse_secret_key
        base_url = secrets.langfuse_base_url
    else:
        public_key = repo_config.langfuse_public_key
        secret_key = repo_config.langfuse_secret_key
        base_url = repo_config.langfuse_base_url
        if not (public_key and secret_key):
            return None
    return LangfuseReadClient(
        public_key=public_key or "",
        secret_key=secret_key or "",
        base_url=base_url,
    )


def _parse_iso(value: str) -> datetime:
    """Naive-UTC parse of a Langfuse ISO-8601 timestamp.

    Delegates to the shared kernel's
    :meth:`LangfuseReadClient.parse_timestamp` (which tolerates a
    trailing ``Z``) and drops the tzinfo so the result can be compared
    against the naive bucket boundaries used by the aggregators."""
    return LangfuseReadClient.parse_timestamp(value).replace(tzinfo=None)


def _langfuse_api_get(
    settings: Settings,
    path: str,
    params: dict | None = None,
    repo_config: RepoConfig | None = None,
):
    """Single authenticated GET to the Langfuse public API.

    The shared :class:`LangfuseReadClient` owns auth-header construction
    and base-URL resolution; this helper layers mill's single-shot
    (non-paginated) GET — used for trace-detail fetches and the
    session endpoints — on top.

    Returns the JSON-decoded response body, or ``None`` when Langfuse is
    unconfigured / unreachable / the request fails."""
    client = _build_read_client(settings, repo_config)
    if client is None:
        return None
    try:
        import httpx

        with httpx.Client(timeout=20) as c:
            r = c.get(
                client.url(path),
                params=params or {},
                headers={"Authorization": client.auth_header()},
            )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001 — analysis must not fail the caller
        return None


def session_total_cost(
    settings: Settings, session_id: str, repo_config: RepoConfig | None = None
) -> float | None:
    """Return the total USD cost for a Langfuse session (sum of
    ``totalCost`` across all its traces), or ``None`` when Langfuse
    is unconfigured / unreachable / returns no data."""
    data = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
        repo_config=repo_config,
    )
    if data is None:
        return None
    traces = data.get("data", [])
    total = 0.0

    def _num(x):
        try:
            return float(x or 0)
        except TypeError, ValueError:
            return 0.0

    for t in traces:
        total += _num(t.get("totalCost"))
    return total


def session_traces(
    settings: Settings,
    session_id: str,
    repo_config: RepoConfig | None = None,
) -> list[dict] | None:
    """Return Langfuse traces for *session_id* as a list of
    ``{name, cost, at, trace_id, model}`` dicts ordered by timestamp
    ascending.

    ``model`` carries the trace-level model / provider tag when the
    Langfuse API provides it (e.g. ``"openai/gpt-4o"``); absent keys
    default to ``""``.

    ``None`` is returned when Langfuse is unconfigured / unreachable so
    the caller can degrade rather than show ``$0`` and pretend that's
    real. The drawer uses this to overlay per-step cost on history rows.
    """
    data = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
        repo_config=repo_config,
    )
    if data is None:
        return None

    def _num(x):
        try:
            return float(x or 0)
        except TypeError, ValueError:
            return 0.0

    out: list[dict] = []
    for t in data.get("data") or []:
        # latency > 0 means Langfuse has the trace's end time. While
        # the trace is still running it'll be 0/null — the drawer uses
        # this to keep an in-flight trace from being labelled
        # `interrupted` (it isn't — it just hasn't finished yet).
        out.append(
            {
                "name": t.get("name") or "?",
                "cost": _num(t.get("totalCost")),
                "at": t.get("timestamp") or "",
                "trace_id": t.get("id") or "",
                "latency": _num(t.get("latency")),
                "model": t.get("model") or "",
            }
        )
    out.sort(key=lambda r: r["at"])
    return out


def session_cost(
    settings: Settings,
    session_id: str,
    repo_config: RepoConfig | None = None,
    *,
    force: bool = False,
) -> float:
    """Read-time per-ticket cost: the Langfuse session total, cached for
    ``_COST_TTL_SECONDS``. Always returns a number (0.0 when Langfuse is
    unconfigured / unreachable / has no data) so callers never special-
    case None. This replaces the old persisted ``cost_usd`` + sync loop:
    cost lives in Langfuse; we just read and briefly cache it.

    ``force=True`` bypasses the TTL gate and always hits Langfuse. Use
    it from the fast warmer loop for active-stage tickets — they're
    actively accruing cost and the board user notices a stale value
    much faster than for an idle ticket. Throttle the caller (not the
    cache) so Langfuse isn't hammered.
    """
    now = time.monotonic()
    hit = _cost_cache.get(session_id)
    if not force and hit is not None and (now - hit[1]) < _COST_TTL_SECONDS:
        return hit[0]
    cost = session_total_cost(settings, session_id, repo_config=repo_config)
    if cost is None:
        # Don't poison the cache with a transient failure — serve the
        # last known value if we have one, else 0.0 (uncached so the
        # next read retries Langfuse).
        return hit[0] if hit is not None else 0.0
    _cost_cache[session_id] = (cost, now)
    return cost


def effective_cost(total: float, baseline: float) -> float:
    """Per-attempt cost after excluding the pre-redraft baseline.

    The Langfuse session total is cumulative over the whole session
    lifetime; ``baseline`` is the snapshot captured at the most recent
    redraft. Subtracting it (clamped at zero) yields the cost spent
    since that redraft — the value used for the dollar-cap limit and the
    primary ``cost_usd`` display."""
    return max(0.0, total - (baseline or 0.0))


def session_cost_cached(session_id: str) -> float:
    """Non-blocking cost lookup: return the cached value if any, else
    0.0. NEVER hits the network. Use this in hot paths like the board's
    /tickets list, which polls every 1s; with N tickets cold the full
    ``session_cost`` would issue N Langfuse HTTP calls and block the
    response for seconds, long enough that the next poll tick cancels
    its predecessor. Per-ticket detail GETs still use the full
    ``session_cost`` to keep the drawer authoritative."""
    hit = _cost_cache.get(session_id)
    if hit is None:
        return 0.0
    return hit[0]


def fetch_trace_detail(
    settings: Settings, trace_id: str, repo_config: RepoConfig | None = None
) -> dict | None:
    """Fetch a single trace by ID from the Langfuse API.

    Returns the JSON-decoded response body, or ``None`` on failure
    (including when Langfuse is unconfigured).
    """
    return _langfuse_api_get(
        settings, f"/api/public/traces/{trace_id}", repo_config=repo_config
    )


def fetch_trace_observations(
    settings: Settings,
    trace_id: str,
    repo_config: RepoConfig | None = None,
) -> list[dict] | None:
    """Return the list of observations for a trace, filtered to
    the fields relevant for event-mode validation.

    Each observation dict contains:
    ``type`` (e.g. ``"GENERATION"``, ``"SPAN"``),
    ``level``, ``statusMessage``, ``input``, ``output``,
    ``model``, ``usage`` (token counts), ``costDetails``,
    ``name``, ``startTime``, ``endTime``.

    Returns ``None`` when Langfuse is unconfigured / unreachable,
    or the trace is not found.
    """
    detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)
    if detail is None:
        return None
    observations = detail.get("observations") or []
    _fields = (
        "type",
        "level",
        "statusMessage",
        "input",
        "output",
        "model",
        "usage",
        "costDetails",
        "name",
        "startTime",
        "endTime",
    )
    return [{k: obs.get(k) for k in _fields} for obs in observations]


def fetch_session_summary(
    settings: Settings, session_id: str, repo_config: RepoConfig | None = None
) -> str | None:
    """Return a short text summary of the session's traces grouped by
    stage, with per-stage cost/latency/observation subtotals and a
    ``## Warnings/Errors`` section sourced from per-trace detail calls.

    Returns ``None`` if Langfuse is unconfigured / unreachable.
    """
    data = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
        repo_config=repo_config,
    )
    if data is None:
        return None
    traces = data.get("data", [])
    if not traces:
        return "(no Langfuse traces found for this session)"

    def num(x):
        try:
            return float(x or 0)
        except TypeError, ValueError:
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
        stage_obs = sum(len(t.get("observations") or []) for t in stage_traces)
        lines.append(
            f"- {stage_name}: ${stage_cost:.4f}  {stage_lat:.1f}s  obs={stage_obs}"
        )

    # --- per-trace detail: collect warnings / errors -------------------
    MAX_WARNINGS = 20
    warnings_errors: list[str] = []
    for t in traces:
        trace_id = t.get("id")
        if not trace_id:
            continue
        detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)
        if detail is None:
            continue
        observations = detail.get("observations") or []
        for obs in observations:
            level = obs.get("level")
            if level in ("WARNING", "ERROR"):
                msg = obs.get("statusMessage", "")
                warnings_errors.append(f"- {t.get('name', '?')} [{level}] {msg}")

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
    repo_config: RepoConfig | None = None,
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
            repo_config=repo_config,
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
        except TypeError, ValueError:
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
            repo_config=repo_config,
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
    settings: Settings, from_timestamp: str, repo_config: RepoConfig | None = None
) -> list[dict]:
    """Return every trace created at or after *from_timestamp* by
    paginating the Langfuse public API.

    Returns an empty list (never crashes) when Langfuse is unconfigured
    or any HTTP / JSON error occurs — the caller must treat ``[]`` as
    "no data available."
    """
    client = _build_read_client(settings, repo_config)
    if client is None:
        return []
    try:
        all_traces: list[dict] = []
        for page in client.iter_pages(
            "/api/public/traces",
            params={"fromTimestamp": from_timestamp, "limit": 50},
            error_label="trace list",
        ):
            all_traces.extend(page)
        return all_traces
    except Exception:  # noqa: BLE001 — never crash the caller
        log.exception("failed to list Langfuse traces since %s", from_timestamp)
        return []


def _fetch_traces_for_tickets(
    settings: Settings,
    max_tickets: int,
    repo_config: RepoConfig | None = None,
) -> list[dict]:
    """Paginate traces by ``timestamp.desc`` (no ``fromTimestamp``),
    collecting traces until we have seen *max_tickets* distinct
    ``sessionId`` values.  Safety cap: 100 pages (10 000 traces).
    """
    PAGE_SIZE = 100
    MAX_PAGES = 100
    client = _build_read_client(settings, repo_config)
    if client is None:
        return []
    all_traces: list[dict] = []
    seen_sessions: set[str] = set()

    try:
        for page_num, page in enumerate(
            client.iter_pages(
                "/api/public/traces",
                params={"limit": PAGE_SIZE, "orderBy": "timestamp.desc"},
                error_label="_fetch_traces_for_tickets",
            ),
            start=1,
        ):
            for t in page:
                all_traces.append(t)
                sid = (t.get("sessionId") or "").strip()
                if sid:
                    seen_sessions.add(sid)

            if len(seen_sessions) >= max_tickets:
                break
            if page_num >= MAX_PAGES:
                break

    except Exception:
        log.exception("_fetch_traces_for_tickets failed")
        return []

    return all_traces


def _fetch_traces_time_window(
    settings: Settings,
    lookback_hours: float,
    max_pages: int,
    caller_name: str,
    repo_config: RepoConfig | None = None,
) -> list[dict] | None:
    """Paginate traces within a time window.

    Returns all traces whose ``timestamp`` >= now - *lookback_hours*,
    paginating at most *max_pages* pages (100 traces/page).

    ``caller_name`` is used in log messages so the source of a failure
    is clear.

    Returns ``None`` on API / HTTP failure, ``[]`` when the window
    contains no traces.
    """
    from_timestamp = (
        (datetime.now(timezone.utc) - timedelta(hours=lookback_hours))
        .isoformat()
        .replace("+00:00", "Z")
    )

    PAGE_SIZE = 100
    client = _build_read_client(settings, repo_config)
    if client is None:
        return None
    all_traces: list[dict] = []

    try:
        for page_num, page in enumerate(
            client.iter_pages(
                "/api/public/traces",
                params={
                    "fromTimestamp": from_timestamp,
                    "limit": PAGE_SIZE,
                    "orderBy": "timestamp.desc",
                },
                error_label=caller_name,
            ),
            start=1,
        ):
            all_traces.extend(page)
            if page_num >= max_pages:
                break

    except Exception:
        log.exception("%s failed", caller_name)
        return None

    return all_traces


def _accumulate_cost_by_key(
    traces: list[dict],
    key_fn: Callable[[dict], str | None],
) -> dict[str, dict]:
    """Group *traces* by a key and sum ``totalCost`` per group.

    *key_fn* receives a trace dict and must return a non-empty string
    key, or ``None`` to skip the trace.

    Returns ``{key: {"total_cost": float, "trace_count": int}}``.
    """
    agg: dict[str, dict] = {}
    for t in traces:
        key = key_fn(t)
        if key is None:
            continue
        cost = float(t.get("totalCost") or 0)
        if key not in agg:
            agg[key] = {"total_cost": 0.0, "trace_count": 0}
        agg[key]["total_cost"] += cost
        agg[key]["trace_count"] += 1
    return agg


def aggregate_cost_trend(
    settings: Settings,
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_config: RepoConfig | None = None,
) -> list[dict]:
    """Return cost bucketed by time.

    With *lookback_hours* (default 24): bucket traces from the last
    *lookback_hours* hours.  Bucket width: 1 hour when lookback ≤ 24,
    1 day when > 24.

    With *max_tickets*: collect traces from the last *max_tickets*
    distinct sessions, compute the time span from the earliest and
    latest trace, and bucket over that span (hourly if ≤ 24 h, daily
    otherwise).

    When both are provided, *max_tickets* takes precedence.

    Each bucket has ``ts`` (ISO-8601 start), ``total_cost``, and
    ``trace_count``.  Paginates up to 100 pages / 10 000 traces.

    Graceful: returns ``[]`` when tracing is disabled or the API errors.
    """
    if repo_config is None and not settings.tracing_enabled:
        return []
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return []

    # --- ticket-count mode ---
    if max_tickets is not None:
        if lookback_hours != 24:
            log.debug(
                "aggregate_cost_trend: max_tickets=%d overrides lookback_hours=%.1f",
                max_tickets,
                lookback_hours,
            )
        all_traces = _fetch_traces_for_tickets(settings, max_tickets, repo_config)
        if not all_traces:
            return []

        # Compute time span from collected traces
        timestamps: list[datetime] = []
        for t in all_traces:
            ts_str = t.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = _parse_iso(ts_str)
                timestamps.append(ts)
            except ValueError, TypeError:
                continue

        if not timestamps:
            return []

        span_start = min(timestamps)
        span_end = max(timestamps)
        span_hours = (span_end - span_start).total_seconds() / 3600.0
        if span_hours < 1.0:
            span_hours = 1.0  # at least one hour to get a non-empty bucket

        if span_hours <= 24:
            # Hourly buckets
            bucket_delta = timedelta(hours=1)
            num_buckets = int(span_hours)
            if span_hours != int(span_hours):
                num_buckets += 1
            bucket_starts = [span_start + bucket_delta * i for i in range(num_buckets)]
            ts_fmt = lambda dt: (
                dt.replace(minute=0, second=0, microsecond=0).isoformat() + "Z"
            )
        else:
            # Daily buckets
            bucket_delta = timedelta(days=1)
            num_days = int(span_hours / 24)
            if span_hours % 24 != 0:
                num_days += 1
            bucket_starts = [
                (span_start + bucket_delta * i).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                for i in range(num_days)
            ]
            ts_fmt = lambda dt: dt.isoformat() + "Z"

        # Initialize buckets
        buckets: dict[str, dict] = {}
        for bs in bucket_starts:
            key = ts_fmt(bs)
            buckets[key] = {"ts": key, "total_cost": 0.0, "trace_count": 0}

        bucket_keys = sorted(buckets.keys())

        # Assign traces to buckets
        for t in all_traces:
            ts_str = t.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = _parse_iso(ts_str)
            except ValueError, TypeError:
                continue

            cost = float(t.get("totalCost") or 0)
            assigned = None
            for key in bucket_keys:
                bucket_dt = _parse_iso(key)
                if bucket_dt <= ts:
                    assigned = key
                else:
                    break

            if assigned is not None:
                buckets[assigned]["total_cost"] += cost
                buckets[assigned]["trace_count"] += 1

        return [buckets[k] for k in bucket_keys]

    # --- time-window mode (original behaviour) ---
    all_traces = _fetch_traces_time_window(
        settings,
        lookback_hours,
        max_pages=100,
        caller_name="aggregate_cost_trend",
        repo_config=repo_config,
    )

    if all_traces is None:
        return []  # API error

    # Determine bucket width
    if lookback_hours <= 24:
        # Hourly buckets
        bucket_delta = timedelta(hours=1)
        num_buckets = int(lookback_hours)  # ceil handled by range
        if lookback_hours != int(lookback_hours):
            num_buckets = int(lookback_hours) + 1
        now = datetime.now(timezone.utc)
        bucket_starts = [
            (now - timedelta(hours=lookback_hours)) + bucket_delta * i
            for i in range(num_buckets)
        ]
        ts_fmt = lambda dt: (
            dt.replace(minute=0, second=0, microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    else:
        # Daily buckets
        bucket_delta = timedelta(days=1)
        num_days = int(lookback_hours / 24)
        if lookback_hours % 24 != 0:
            num_days += 1
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=lookback_hours)
        bucket_starts = [
            (start + bucket_delta * i).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for i in range(num_days)
        ]
        ts_fmt = lambda dt: dt.isoformat().replace("+00:00", "Z")

    # Initialize buckets
    buckets: dict[str, dict] = {}
    for bs in bucket_starts:
        key = ts_fmt(bs)
        buckets[key] = {"ts": key, "total_cost": 0.0, "trace_count": 0}

    # Sort bucket keys for boundary lookups
    bucket_keys = sorted(buckets.keys())

    # Assign traces to buckets
    for t in all_traces:
        ts_str = t.get("timestamp")
        if not ts_str:
            continue
        try:
            # Parse timestamp; Langfuse returns ISO-8601 (naive for
            # comparison with naive bucket boundaries).
            ts_naive = _parse_iso(ts_str)
        except ValueError, TypeError:
            continue

        cost = float(t.get("totalCost") or 0)

        # Find the right bucket: the last bucket whose start <= trace timestamp
        assigned = None
        for key in bucket_keys:
            bucket_dt = _parse_iso(key)
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
    max_tickets: int | None = None,
    repo_config: RepoConfig | None = None,
) -> list[dict]:
    """Return cost aggregated by trace name.

    With *lookback_hours* (default 24): aggregate traces from the last
    *lookback_hours* hours.

    With *max_tickets*: aggregate traces from the last *max_tickets*
    distinct sessions.

    When both are provided, *max_tickets* takes precedence.

    Graceful: returns ``[]`` when tracing is disabled or the API errors.
    Paginates up to 100 pages / 10,000 traces.
    """
    if repo_config is None and not settings.tracing_enabled:
        return []
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return []

    # --- ticket-count mode ---
    if max_tickets is not None:
        if lookback_hours != 24:
            log.debug(
                "aggregate_cost_by_name: max_tickets=%d overrides lookback_hours=%.1f",
                max_tickets,
                lookback_hours,
            )
        all_traces = _fetch_traces_for_tickets(settings, max_tickets, repo_config)
    else:
        # --- time-window mode ---
        all_traces = _fetch_traces_time_window(
            settings,
            lookback_hours,
            max_pages=100,
            caller_name="aggregate_cost_by_name",
            repo_config=repo_config,
        )

    # Aggregate by name
    if all_traces is None:
        return []  # API error
    agg = _accumulate_cost_by_key(
        all_traces,
        key_fn=lambda t: (t.get("name") or "").strip() or None,
    )

    result = [
        {
            "name": name,
            "total_cost": entry["total_cost"],
            "trace_count": entry["trace_count"],
        }
        for name, entry in agg.items()
    ]
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return result


def most_expensive_ticket(
    settings: Settings,
    lookback_hours: float = 24,
    max_tickets: int | None = None,
    repo_config: RepoConfig | None = None,
) -> dict | None:
    """Return the session with the highest total cost.

    With *lookback_hours* (default 24): examine traces from the last
    *lookback_hours* hours.

    With *max_tickets*: examine traces from the last *max_tickets*
    distinct sessions.

    When both are provided, *max_tickets* takes precedence.

    Groups traces by ``sessionId``, sums ``totalCost`` per session,
    and returns the single session with the highest total cost.

    Graceful: returns ``None`` when tracing is disabled or the API
    errors.  Examines at most 500 traces (time-window) or 10 000
    traces / 100 pages (ticket-count) to bound API calls.
    """
    if repo_config is None and not settings.tracing_enabled:
        return None
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return None

    # --- ticket-count mode ---
    if max_tickets is not None:
        if lookback_hours != 24:
            log.debug(
                "most_expensive_ticket: max_tickets=%d overrides lookback_hours=%.1f",
                max_tickets,
                lookback_hours,
            )
        all_traces = _fetch_traces_for_tickets(settings, max_tickets, repo_config)
    else:
        # --- time-window mode ---
        all_traces = _fetch_traces_time_window(
            settings,
            lookback_hours,
            max_pages=5,
            caller_name="most_expensive_ticket",
            repo_config=repo_config,
        )
        all_traces = all_traces[:500] if all_traces is not None else None

    # Aggregate by session_id
    if all_traces is None:
        return None  # API error
    agg = _accumulate_cost_by_key(
        all_traces,
        key_fn=lambda t: (t.get("sessionId") or "").strip() or None,
    )

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
    max_tickets: int | None = None,
    repo_config: RepoConfig | None = None,
) -> dict | None:
    """Return the single trace with the highest ``totalCost``.

    With *lookback_hours* (default 24): examine traces from the last
    *lookback_hours* hours.

    With *max_tickets*: examine traces from the last *max_tickets*
    distinct sessions.

    When both are provided, *max_tickets* takes precedence.

    Skips unnamed/in-flight traces (same ``_named`` filter as
    ``list_recent_traces``).

    Graceful: returns ``None`` when tracing is disabled or the API
    errors.  Examines at most 500 traces (time-window) or 10 000
    traces / 100 pages (ticket-count) to bound API calls.
    """
    if repo_config is None and not settings.tracing_enabled:
        return None
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return None

    # --- ticket-count mode ---
    if max_tickets is not None:
        if lookback_hours != 24:
            log.debug(
                "most_expensive_trace: max_tickets=%d overrides lookback_hours=%.1f",
                max_tickets,
                lookback_hours,
            )
        all_traces = _fetch_traces_for_tickets(settings, max_tickets, repo_config)
    else:
        # --- time-window mode ---
        all_traces = _fetch_traces_time_window(
            settings,
            lookback_hours,
            max_pages=5,
            caller_name="most_expensive_trace",
            repo_config=repo_config,
        )
        all_traces = all_traces[:500] if all_traces is not None else None

    if all_traces is None:
        return None  # API error

    # Find the single named trace with highest cost
    best_trace: dict | None = None
    best_cost = -1.0

    for t in all_traces:
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


def ticket_with_most_steps(
    settings: Settings,
    lookback_hours: float = 24,
    repo_config: RepoConfig | None = None,
) -> dict | None:
    """Return the session (ticket) that ran the most pipeline *steps* in
    the window, counting one step per trace (each agent invocation /
    stage retry produces one trace).

    Groups traces by ``sessionId`` and returns the session with the
    highest trace count, along with its summed cost. Cheap — no
    per-observation fetches. Graceful: ``None`` when tracing is
    disabled / unconfigured / the API errors.
    """
    if repo_config is None and not settings.tracing_enabled:
        return None
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return None

    all_traces = _fetch_traces_time_window(
        settings,
        lookback_hours,
        max_pages=5,
        caller_name="ticket_with_most_steps",
        repo_config=repo_config,
    )
    if all_traces is None:
        return None
    all_traces = all_traces[:500]
    agg = _accumulate_cost_by_key(
        all_traces,
        key_fn=lambda t: (t.get("sessionId") or "").strip() or None,
    )
    if not agg:
        return None
    best_sid, best = max(agg.items(), key=lambda item: item[1]["trace_count"])
    return {
        "session_id": best_sid,
        "step_count": best["trace_count"],
        "total_cost": best["total_cost"],
    }


def trace_with_most_errors(
    settings: Settings,
    lookback_hours: float = 24,
    repo_config: RepoConfig | None = None,
    max_scan: int = 40,
) -> dict | None:
    """Return the trace with the most error observations in the window.

    Errors burn re-run tokens, so the noisiest trace is a prime
    cost-reduction specimen. Counting errors requires the observation
    tree, so this scans at most *max_scan* candidate traces (the
    highest-cost ones first — error-heavy traces tend to be expensive)
    and fetches each one's observations, counting observations whose
    ``level`` is ``ERROR``/``WARNING`` or whose output matches a tool-error
    pattern. Returns the trace with the highest error count (>0), else
    ``None``.

    Graceful: ``None`` when tracing is disabled / unconfigured / the API
    errors or no candidate has any errors.
    """
    if repo_config is None and not settings.tracing_enabled:
        return None
    if repo_config is not None and not (
        repo_config.langfuse_public_key and repo_config.langfuse_secret_key
    ):
        return None

    all_traces = _fetch_traces_time_window(
        settings,
        lookback_hours,
        max_pages=5,
        caller_name="trace_with_most_errors",
        repo_config=repo_config,
    )
    if all_traces is None:
        return None
    # Scan the most expensive candidates first (capped) — error storms
    # correlate with high cost, and this bounds the per-observation fetches.
    named = [
        t
        for t in all_traces
        if isinstance(t.get("name"), str) and (t.get("name") or "").strip()
    ]
    named.sort(key=lambda t: float(t.get("totalCost") or 0), reverse=True)
    candidates = named[:max_scan]

    best: dict | None = None
    best_errors = 0
    for t in candidates:
        obs = fetch_trace_observations(settings, t.get("id") or "", repo_config)
        if not obs:
            continue
        errors = sum(1 for o in obs if _observation_is_error(o))
        if errors > best_errors:
            best_errors = errors
            best = {
                "id": t.get("id", ""),
                "name": t.get("name", ""),
                "error_count": errors,
                "total_cost": float(t.get("totalCost") or 0),
                "session_id": t.get("sessionId") or None,
            }
    return best


def _observation_is_error(obs: dict) -> bool:
    """True when an observation signals an error — by Langfuse ``level``
    or by an error pattern in its ``statusMessage`` / ``output``."""
    level = (obs.get("level") or "").upper()
    if level in ("ERROR", "WARNING"):
        return True
    blob = f"{obs.get('statusMessage') or ''} {obs.get('output') or ''}"
    return bool(_ERROR_OBS_PATTERN.search(blob))


_ERROR_OBS_PATTERN = re.compile(
    r"(error:|Traceback \(most recent call last\)|"
    r"UsageLimitExceeded|UnexpectedModelBehavior|non-zero exit status)",
    re.IGNORECASE,
)

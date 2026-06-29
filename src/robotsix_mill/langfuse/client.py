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
import time

from robotsix_llmio.core import LangfuseReadClient

from ..config import RepoConfig, Settings, get_secrets

log = logging.getLogger("robotsix_mill.langfuse.client")

# Short in-memory cache for read-time cost lookups. Per-ticket cost is
# read straight from the Langfuse session on demand (the board polls
# often); this TTL keeps that to one Langfuse call per ticket per
# minute instead of one per render. Process-local, best-effort.
_COST_TTL_SECONDS = 60.0
_cost_cache: dict[str, tuple[float, float]] = {}  # id -> (cost, monotonic)


def _qualified(session_id: str, repo_config: RepoConfig | None) -> str:
    """Repo-qualify a ticket/session id so cost + trace lookups query the
    same Langfuse ``sessionId`` the tracer stamps (``<repo> · <id>``).

    The #1395 single-project consolidation prefixed every trace's session
    with ``<repo> · ``, but the cost/trace read path kept querying the bare
    ticket id — so every lookup matched nothing and read ``$0``. Qualifying
    here (idempotent, and a no-op when no repo is known) repairs that for
    all callers and keeps the cost-cache key consistent between the
    blocking and cache-only reads."""
    if repo_config is None:
        return session_id
    from ..runtime.tracing import qualify_session

    return qualify_session(session_id, repo_config)


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
    except Exception as e:
        log.debug("Langfuse API GET %s failed: %s: %s", repr(path), type(e).__name__, e)
        return None


def session_total_cost(
    settings: Settings, session_id: str, repo_config: RepoConfig | None = None
) -> float | None:
    """Return the total USD cost for a Langfuse session (sum of
    ``totalCost`` across all its traces), or ``None`` when Langfuse
    is unconfigured / unreachable / returns no data."""
    session_id = _qualified(session_id, repo_config)
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
    session_id = _qualified(session_id, repo_config)
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
    session_id = _qualified(session_id, repo_config)
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


def session_cost_cached(
    session_id: str, repo_config: RepoConfig | None = None
) -> float:
    """Non-blocking cost lookup: return the cached value if any, else
    0.0. NEVER hits the network. Use this in hot paths like the board's
    /tickets list, which polls every 1s; with N tickets cold the full
    ``session_cost`` would issue N Langfuse HTTP calls and block the
    response for seconds, long enough that the next poll tick cancels
    its predecessor. Per-ticket detail GETs still use the full
    ``session_cost`` to keep the drawer authoritative.

    *repo_config* must match the one passed to ``session_cost`` so the
    cache key (the repo-qualified session id) lines up — otherwise this
    reads a different key and always misses."""
    hit = _cost_cache.get(_qualified(session_id, repo_config))
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
    session_id = _qualified(session_id, repo_config)
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


def trace_observation_summary(trace: dict) -> dict:
    """Extract a compact observation summary from a Langfuse trace dict.

    The Langfuse list-endpoint response includes an ``observations`` array
    on each trace.  This helper distills it into a small summary suitable
    for cost-attribution dashboards and the ``/traces/recent`` REST
    endpoint — none of the full prompt/completion bodies, just the
    aggregate numbers a fleet-level cost analyzer needs.

    Returns a dict with:
    - ``model``: trace-level model string, or the first GENERATION model (``""`` if absent)
    - ``input_tokens``: total input/prompt tokens across all GENERATION observations
    - ``output_tokens``: total output/completion tokens across all GENERATION observations
    - ``total_tokens``: ``input_tokens + output_tokens``
    - ``tool_calls``: sorted list of ``{name, count}`` for non-chat SPAN observations
    - ``error_count``: number of ERROR-level observations
    - ``warning_count``: number of WARNING-level observations
    - ``observation_count``: total number of observations
    """
    observations = trace.get("observations") or []

    input_tokens = 0
    output_tokens = 0
    tool_calls: dict[str, int] = {}
    error_count = 0
    warning_count = 0

    for obs in observations:
        name = obs.get("name") or ""

        usage = obs.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens += int(usage.get("input") or usage.get("promptTokens") or 0)
            output_tokens += int(
                usage.get("output") or usage.get("completionTokens") or 0
            )

        if name and not name.startswith("chat "):
            tool_calls[name] = tool_calls.get(name, 0) + 1

        level = obs.get("level")
        if level == "ERROR":
            error_count += 1
        elif level == "WARNING":
            warning_count += 1

    model = trace.get("model") or ""
    if not model:
        for obs in observations:
            if obs.get("type") == "GENERATION" and obs.get("model"):
                model = obs.get("model") or ""
                break

    tool_call_list = sorted(
        [{"name": k, "count": v} for k, v in tool_calls.items()],
        key=lambda x: -x["count"],
    )

    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "tool_calls": tool_call_list,
        "error_count": error_count,
        "warning_count": warning_count,
        "observation_count": len(observations),
    }


def list_all_traces_since(
    settings: Settings,
    from_timestamp: str,
    repo_config: RepoConfig | None = None,
    *,
    max_traces: int | None = None,
) -> list[dict]:
    """Return traces created at or after *from_timestamp*, newest-first, by
    paginating the Langfuse public API.

    When *max_traces* is set, pagination stops as soon as that many traces
    have been collected, bounding the fetch to roughly ``ceil(max_traces/50)``
    pages instead of the entire backlog. Traces are requested newest-first
    (``orderBy=timestamp.desc``) so the bounded result is the most RECENT
    *max_traces*, not an arbitrary slice — without this a stale watermark
    could pull thousands of traces in one pass and peg the event loop.

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
            params={
                "fromTimestamp": from_timestamp,
                "limit": 50,
                "orderBy": "timestamp.desc",
            },
            error_label="trace list",
        ):
            all_traces.extend(page)
            if max_traces is not None and len(all_traces) >= max_traces:
                return all_traces[:max_traces]
        return all_traces
    except Exception:  # noqa: BLE001 — never crash the caller
        log.exception("failed to list Langfuse traces since %s", from_timestamp)
        return []

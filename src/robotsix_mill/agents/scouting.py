"""The scout agent: evaluates OpenRouter models per agent role and
proposes improvement drafts when a materially better option exists.

Seam: tests monkeypatch ``run_scout_agent``. No real network in tests.
The scout makes direct REST calls to OpenRouter — it is NOT an LLM agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, Field

from ..config import Settings

log = logging.getLogger("robotsix_mill.scout")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# ── role definitions ──────────────────────────────────────────────────

CAPABLE_ROLES = [
    ("model", "MILL_MODEL", "coordinator / implement"),
    ("refine_model", "MILL_REFINE_MODEL", "spec authoring"),
]
STRUCTURED_ROLES = [
    ("retrospect_model", "MILL_RETROSPECT_MODEL", "retrospect"),
    ("audit_model", "MILL_AUDIT_MODEL", "audit"),
]
CHEAP_ROLES = [
    ("explore_model", "MILL_EXPLORE_MODEL", "explore scout"),
    ("test_model", "MILL_TEST_MODEL", "test distiller"),
    ("web_research_model", "MILL_WEB_RESEARCH_MODEL", "web research"),
    ("agent_check_model", "MILL_AGENT_CHECK_MODEL", "agent definition checker"),
]

ALL_ROLES = CAPABLE_ROLES + STRUCTURED_ROLES + CHEAP_ROLES

CAPABLE_CANDIDATES = [
    "anthropic/claude-sonnet-4-20250514",
    "openai/gpt-4o",
    "google/gemini-2.5-pro",
]
CHEAP_CANDIDATES = [
    "deepseek/deepseek-v4-flash",
    "meta-llama/llama-4-maverick",
    "google/gemini-2.0-flash-001",
    "anthropic/claude-3.5-haiku",
]

LATENCY_P99_WARN_MS = 10_000  # 10 seconds
THROUGHPUT_P50_WARN_TPS = 20


# ── data types ────────────────────────────────────────────────────────


class PercentileStats(BaseModel):
    """Percentile latency/throughput statistics from an OpenRouter endpoint."""
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p99: float | None = None


class EndpointInfo(BaseModel):
    """A single provider endpoint for a model, including pricing, latency, and throughput data."""
    provider_name: str = ""
    # OpenRouter sends ``status`` as an *integer* code now (observed:
    # 0 for normal, -2 for degraded). Older response shapes used a
    # string like "active". Accept either so a server-side schema
    # change can't crash the scout pass. ``active_provider_count``
    # below treats 0 and "active" as equivalent.
    status: int | str = ""
    uptime_last_30m: float | None = None
    supports_tool_calls: bool | None = None
    context_length: int = 0
    prompt_price: float | None = None
    completion_price: float | None = None
    latency_last_30m: PercentileStats | None = None
    throughput_last_30m: PercentileStats | None = None


class ModelInfo(BaseModel):
    """Aggregated model metadata and its list of provider endpoints.

    Provides derived properties for provider counts, uptime, latency,
    throughput, and estimated generation time.
    """
    id: str = ""
    name: str = ""
    context_length: int = 0
    prompt_price: float | None = None
    completion_price: float | None = None
    endpoints: list[EndpointInfo] = Field(default_factory=list)

    @property
    def provider_count(self) -> int:
        """Total number of provider endpoints."""
        return len(self.endpoints)

    @property
    def active_provider_count(self) -> int:
        """Number of endpoints currently treated as active. OpenRouter
        used to return a ``"active"`` string for status; today it sends
        an integer code where 0 means the endpoint is healthy. Count
        either shape so a schema change can't silently zero this out."""
        return sum(
            1 for e in self.endpoints
            if e.status == "active" or e.status == 0
        )

    @property
    def has_tool_calls(self) -> bool:
        """Whether any endpoint supports tool calls."""
        return any(e.supports_tool_calls is True for e in self.endpoints)

    @property
    def is_preview(self) -> bool:
        """Whether the model id indicates a preview release."""
        return "-preview" in self.id.lower()

    @property
    def max_uptime(self) -> float | None:
        """Best uptime across all endpoints, or ``None`` if no data."""
        ups = [e.uptime_last_30m for e in self.endpoints if e.uptime_last_30m is not None]
        return max(ups) if ups else None

    @property
    def max_latency_p99_ms(self) -> float | None:
        """Worst-case P99 time-to-first-token in milliseconds across endpoints."""
        vals = [
            e.latency_last_30m.p99
            for e in self.endpoints
            if e.latency_last_30m is not None and e.latency_last_30m.p99 is not None
        ]
        return max(vals) if vals else None

    @property
    def min_throughput_p50_tps(self) -> float | None:
        """Worst-case P50 throughput in tokens per second across endpoints."""
        vals = [
            e.throughput_last_30m.p50
            for e in self.endpoints
            if e.throughput_last_30m is not None and e.throughput_last_30m.p50 is not None
        ]
        return min(vals) if vals else None

    @property
    def estimated_slow_generation_seconds(self) -> float | None:
        """Estimated time in seconds to generate 4000 tokens at worst-case P50 throughput.

        Returns ``None`` when throughput data is unavailable.
        """
        tps = self.min_throughput_p50_tps
        if tps is not None and tps > 0:
            return 4000.0 / tps
        return None


class ScoutResult(BaseModel):
    """Return value of :func:`run_scout_agent`.

    Contains the updated memory ledger and any draft improvement proposals
    generated during the scouting run.
    """
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────────


def _auth_headers(settings: Settings) -> dict[str, str]:
    """Build the HTTP auth headers dict from settings using the OpenRouter API key."""
    h: dict[str, str] = {}
    if settings.openrouter_api_key:
        h["Authorization"] = f"Bearer {settings.openrouter_api_key}"
    return h


def _fetch_models(client: httpx.Client, settings: Settings) -> dict[str, ModelInfo]:
    """GET ``/models`` from the OpenRouter API and return a dict mapping model
    IDs to :class:`ModelInfo` objects.  Each model's pricing fields are coerced
    via :func:`_float`."""
    headers = _auth_headers(settings)
    r = client.get(f"{OPENROUTER_BASE}/models", headers=headers, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    models: dict[str, ModelInfo] = {}
    for raw in data.get("data", []):
        rid = raw.get("id", "")
        pricing = raw.get("pricing", {}) or {}
        models[rid] = ModelInfo(
            id=rid,
            name=raw.get("name", ""),
            context_length=raw.get("context_length", 0),
            prompt_price=_float(pricing.get("prompt")),
            completion_price=_float(pricing.get("completion")),
        )
    return models


def _parse_percentile_stats(raw: dict | None) -> PercentileStats | None:
    """Parse a raw percentile dict into a :class:`PercentileStats`, returning
    ``None`` when the raw dict is empty or all percentile values are ``None``."""
    if not raw:
        return None
    stats = PercentileStats(
        p50=_float(raw.get("p50")),
        p75=_float(raw.get("p75")),
        p90=_float(raw.get("p90")),
        p99=_float(raw.get("p99")),
    )
    if stats.p50 is None and stats.p75 is None and stats.p90 is None and stats.p99 is None:
        return None
    return stats


def _fetch_endpoints(
    client: httpx.Client, settings: Settings, model_id: str
) -> list[EndpointInfo]:
    """Fetch endpoint/availability data for a single model from OpenRouter.
    Returns a list of :class:`EndpointInfo` objects, or an empty list on
    HTTP errors."""
    headers = _auth_headers(settings)
    try:
        r = client.get(
            f"{OPENROUTER_BASE}/models/{model_id}/endpoints",
            headers=headers,
            timeout=30.0,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        log.warning("Failed to fetch endpoints for %s", model_id)
        return []
    data = r.json()
    # OpenRouter's /models/{id}/endpoints returns:
    #   {"data": {"id": ..., "name": ..., "endpoints": [ … ]}}
    # Older code assumed data["data"] was the endpoints list directly
    # (iterating a dict yields its KEYS — bare strings — and the next
    # `raw.get(...)` blew up with `'str' object has no attribute 'get'`).
    # Read the nested .endpoints, while tolerating the legacy list
    # shape in case the API ever flips back.
    container = data.get("data", {})
    if isinstance(container, dict):
        items = container.get("endpoints", []) or []
    elif isinstance(container, list):
        items = container
    else:
        items = []
    eps: list[EndpointInfo] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue  # defensive: malformed entry
        pricing = raw.get("pricing", {}) or {}
        eps.append(EndpointInfo(
            provider_name=raw.get("provider_name", ""),
            status=raw.get("status", ""),
            uptime_last_30m=_float(raw.get("uptime_last_30m")),
            supports_tool_calls=raw.get("supports_tool_calls"),
            context_length=raw.get("context_length", 0),
            prompt_price=_float(pricing.get("prompt")),
            completion_price=_float(pricing.get("completion")),
            latency_last_30m=_parse_percentile_stats(raw.get("latency_last_30m")),
            throughput_last_30m=_parse_percentile_stats(raw.get("throughput_last_30m")),
        ))
    return eps


def _float(v: object) -> float | None:
    """Coerce *v* to ``float``, returning ``None`` when the value is ``None``
    or cannot be converted."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── evaluation ────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    """Scored evaluation of one model for one agent role.

    Captures provider counts, uptime, pricing, latency, throughput,
    flags, and a composite score used to compare candidates.
    """
    model_id: str
    role_name: str
    env_var: str
    total_providers: int = 0
    active_providers: int = 0
    max_uptime: float | None = None
    is_preview: bool = False
    has_tool_calls: bool = False
    prompt_price: float | None = None
    completion_price: float | None = None
    max_latency_p99_ms: float | None = None
    min_throughput_p50_tps: float | None = None
    estimated_slow_generation_s: float | None = None
    flags: list[str] = field(default_factory=list)
    score: float = 0.0


def _evaluate_model(  # noqa: C901  # TODO: split scoring/flagging into sub-functions (ticket: split_run_scout_agent)
    model_id: str,
    role_name: str,
    env_var: str,
    role_tier: str,
    model_info: ModelInfo,
) -> EvalResult:
    """Score *model_id* for a single agent role, producing an :class:`EvalResult`
    with provider counts, uptime, pricing, latency, throughput, flags, and
    a composite score.  Flags such as ``SINGLE_PROVIDER``, ``PREVIEW``, or
    ``ZERO_PROVIDERS`` are set based on endpoint data and role tier."""
    e = EvalResult(
        model_id=model_id,
        role_name=role_name,
        env_var=env_var,
        total_providers=model_info.provider_count,
        active_providers=model_info.active_provider_count,
        max_uptime=model_info.max_uptime,
        is_preview=model_info.is_preview,
        has_tool_calls=model_info.has_tool_calls,
        prompt_price=model_info.prompt_price,
        completion_price=model_info.completion_price,
        max_latency_p99_ms=model_info.max_latency_p99_ms,
        min_throughput_p50_tps=model_info.min_throughput_p50_tps,
        estimated_slow_generation_s=model_info.estimated_slow_generation_seconds,
    )

    # --- flagging ---
    if model_info.provider_count == 0:
        e.flags.append("ZERO_PROVIDERS")
    elif model_info.provider_count == 1:
        e.flags.append("SINGLE_PROVIDER")
    if model_info.is_preview:
        e.flags.append("PREVIEW")
    if model_info.active_provider_count == 0 and model_info.provider_count > 0:
        e.flags.append("NO_ACTIVE_PROVIDERS")
    if e.max_uptime is not None and e.max_uptime < 0.90:
        e.flags.append("LOW_UPTIME")
    if role_tier in ("capable", "structured") and not model_info.has_tool_calls:
        e.flags.append("NO_TOOL_CALLS")
    if e.max_latency_p99_ms is not None and e.max_latency_p99_ms > LATENCY_P99_WARN_MS:
        e.flags.append("SLOW_LATENCY")
    if e.min_throughput_p50_tps is not None and e.min_throughput_p50_tps < THROUGHPUT_P50_WARN_TPS:
        e.flags.append("SLOW_THROUGHPUT")

    # --- scoring ---
    score = 0.0

    if model_info.provider_count >= 3:
        score += 30.0
    elif model_info.provider_count == 2:
        score += 15.0
    elif model_info.provider_count == 1:
        score += 5.0

    score += model_info.active_provider_count * 8.0

    if e.max_uptime is not None:
        score += e.max_uptime * 20.0

    if model_info.is_preview:
        score -= 40.0

    if role_tier in ("capable", "structured"):
        if model_info.has_tool_calls:
            score += 20.0
        else:
            score -= 30.0

    if e.max_latency_p99_ms is not None:
        excess_s = (e.max_latency_p99_ms - 2000) / 1000
        if excess_s > 0:
            score -= min(30.0, excess_s * 6.0)

    if e.min_throughput_p50_tps is not None:
        deficit = max(0, 50 - e.min_throughput_p50_tps)
        score -= min(20.0, deficit * 0.4)

    if role_tier == "cheap":
        if model_info.prompt_price is not None and model_info.completion_price is not None:
            combined = model_info.prompt_price + model_info.completion_price
            if combined > 0:
                score += max(0, 30.0 - combined * 2)
    else:
        if model_info.prompt_price is not None and model_info.completion_price is not None:
            combined = model_info.prompt_price + model_info.completion_price
            if combined > 0:
                score += max(0, 15.0 - combined * 0.5)

    e.score = score
    return e


def _flag_text(flags: list[str]) -> str:
    """Join a list of flag strings into a pipe-delimited display string, or
    return an empty string when there are no flags."""
    if not flags:
        return ""
    return " | ".join(flags)


# ── memory parsing / updating ─────────────────────────────────────────


def _parse_memory(memory: str) -> dict[str, set[str]]:
    """Parse the memory ledger text into a ``dict[str, set[str]]`` mapping role
    env-var names to sets of previously-proposed model IDs.  Only entries under
    the ``## Proposed`` section are collected."""
    proposed: dict[str, set[str]] = {}
    current_section: str | None = None
    for line in memory.splitlines():
        line = line.strip()
        if line.startswith("## Proposed"):
            current_section = "proposed"
            continue
        elif line.startswith("## Declined"):
            current_section = "declined"
            continue
        elif line.startswith("## "):
            current_section = None
            continue
        if current_section and line.startswith("- "):
            import re
            m = re.match(r"- `([^`]+)`\s+for\s+(\w+)", line)
            if m:
                model_id = m.group(1)
                env_var = m.group(2)
                proposed.setdefault(env_var, set()).add(model_id)
    return proposed


def _build_updated_memory(
    old_memory: str,
    new_proposals: list[tuple[str, str, str]],
) -> str:
    """Merge new proposals into the old memory ledger, prepending a date header.
    New entries are inserted immediately after the ``## Proposed`` section heading."""
    from datetime import date
    today = date.today().isoformat()
    if not old_memory or old_memory.strip() == "":
        old_memory = "# Scout Memory\n\n## Proposed\n\n## Declined\n"
    if "## Proposed" not in old_memory:
        old_memory += "\n## Proposed\n"
    for env_var, model_id, role_name in new_proposals:
        line = f"- `{model_id}` for {env_var} ({today})\n"
        idx = old_memory.find("## Proposed")
        if idx >= 0:
            nl = old_memory.find("\n", idx)
            if nl >= 0:
                old_memory = old_memory[:nl + 1] + line + old_memory[nl + 1:]
    return old_memory


# ── draft body helpers ────────────────────────────────────────────────


def _fragile_text(best: EvalResult) -> str:
    """Produce a warning paragraph when *best* carries fragility flags such as
    ``SINGLE_PROVIDER`` or is a preview model.  Returns an empty string when no
    fragility concerns are present."""
    parts: list[str] = []
    if "SINGLE_PROVIDER" in best.flags:
        parts.append(
            "⚠️ **Fragile:** this candidate has a single provider — "
            "if that provider degrades there is no fallback."
        )
    if best.is_preview:
        parts.append(
            "⚠️ **Fragile:** this is a preview model — its id may "
            "change or it may vanish without notice."
        )
    if not parts:
        return ""
    return "\n".join(f"- {p}" for p in parts) + "\n"


def _latency_timeout_note(e: EvalResult, settings: Settings) -> str | None:
    """If the evaluated model shows notable latency/throughput concerns,
    return a paragraph noting the ``MILL_MODEL_REQUEST_TIMEOUT``
    implication.  Returns None when nothing needs to be said."""
    notes: list[str] = []

    if e.estimated_slow_generation_s is not None:
        est = e.estimated_slow_generation_s
        timeout = settings.model_request_timeout
        if est > timeout:
            notes.append(
                f"Estimated 4000-token generation time: **{est:.0f}s** "
                f"(at worst-case p50 throughput of {e.min_throughput_p50_tps:.0f} tps). "
            )
            notes.append(
                f"⚠️ This exceeds the current `MILL_MODEL_REQUEST_TIMEOUT` "
                f"({timeout:.0f}s).  If you switch, consider raising "
                f"`MILL_MODEL_REQUEST_TIMEOUT` to at least {int(est * 1.5)}s "
                f"to allow safe headroom."
            )
        elif est > timeout * 0.7:
            notes.append(
                f"Estimated 4000-token generation time: **{est:.0f}s** "
                f"(at worst-case p50 throughput of {e.min_throughput_p50_tps:.0f} tps). "
            )
            notes.append(
                f"This is within the current `MILL_MODEL_REQUEST_TIMEOUT` "
                f"({timeout:.0f}s) but with limited headroom.  Monitor for "
                f"timeouts on complex tickets."
            )

    if e.max_latency_p99_ms is not None and e.max_latency_p99_ms > LATENCY_P99_WARN_MS:
        notes.append(
            f"⚠️ P99 time-to-first-token is **{e.max_latency_p99_ms / 1000:.1f}s** — "
            f"the model may feel sluggish to start responding."
        )

    if not notes:
        return None
    return "\n".join(notes)


def _build_draft_body(
    *,
    reason_kind: str,
    reason_text: str,
    env_var: str,
    configured_id: str,
    best: EvalResult,
    current_eval: EvalResult,
    label: str,
    settings: Settings,
) -> str:
    """Build the full Markdown body of a scout draft for a regression or
    improvement finding.  Includes the heading, reason, proposed change,
    evidence comparison, and any fragility or latency/timeout advisories."""
    heading = (
        "## Regression detected" if reason_kind == "regression"
        else "## Improvement identified"
    )
    body = (
        f"{heading}\n\n"
        f"{reason_text}\n\n"
        f"## Proposed change\n\n"
        f"Set `{env_var}={best.model_id}` in `.env`:\n\n"
        f"```\n"
        f"{env_var}={best.model_id}\n"
        f"```\n\n"
        f"## Evidence\n\n"
        f"- **Current** (`{configured_id}`): {_eval_summary(current_eval)}\n"
        f"- **Candidate** (`{best.model_id}`): {_eval_summary(best)}\n"
    )
    if best.flags:
        body += f"\n- ⚠️ Candidate flags: {_flag_text(best.flags)}\n"
    fragile = _fragile_text(best)
    if fragile:
        body += f"\n{fragile}"
    timeout_note = _latency_timeout_note(best, settings)
    if timeout_note:
        body += f"\n## Latency / timeout advisory\n\n{timeout_note}\n"
    return body


# ── main seam ─────────────────────────────────────────────────────────


def _evaluate_role(  # noqa: C901  # TODO: split into smaller functions (ticket: split_run_scout_agent)
    *,
    env_var: str,
    attr: str,
    tier: str,
    label: str,
    configured_id: str,
    enriched: dict[str, ModelInfo],
    candidate_ids: list[str],
    already: set[str],
    settings: Settings,
) -> list[tuple[str, str, str]]:
    """Evaluate all candidate models for a single configured agent role, returning
    a list of ``(draft_title, draft_body, model_id)`` proposals for any findings.
    Detects regressions (zero providers, preview, single provider) and material
    score improvements against the currently configured model."""
    current_info = enriched.get(configured_id)
    if current_info is None:
        return []
    current_eval = _evaluate_model(configured_id, label, env_var, tier, current_info)
    candidates: list[EvalResult] = []
    for cid in candidate_ids:
        if cid == configured_id:
            continue
        info = enriched.get(cid)
        if info is None:
            continue
        candidates.append(_evaluate_model(cid, label, env_var, tier, info))

    regression_reason: str | None = None
    if current_info.provider_count == 0:
        regression_reason = (
            f"`{configured_id}` has **zero providers** listed on OpenRouter — "
            "it may have been delisted or is unavailable.")
    elif current_eval.is_preview:
        regression_reason = (
            f"`{configured_id}` is a **preview** model — its id may change or "
            "it may leave preview without notice, causing silent breakage.")
    elif current_info.provider_count == 1:
        regression_reason = (
            f"`{configured_id}` has only **{current_info.provider_count} provider** "
            f"({_providers_text(current_info.endpoints)}). A single-provider model "
            "has no fallback when that provider 429s or has latency spikes — "
            "the pipeline can stall repeatedly.")
    if regression_reason:
        best = _best_candidate(candidates)
        if best is None or best.model_id in already:
            return []
        title = f"scout: switch {env_var} from `{configured_id}` to `{best.model_id}`"
        body = _build_draft_body(
            reason_kind="regression", reason_text=regression_reason,
            env_var=env_var, configured_id=configured_id,
            best=best, current_eval=current_eval, label=label, settings=settings)
        return [(title, body, best.model_id)]

    best = _best_candidate(candidates)
    if best and best.score > current_eval.score + 10.0 and best.model_id not in already:
        title = f"scout: switch {env_var} from `{configured_id}` to `{best.model_id}`"
        body = _build_draft_body(
            reason_kind="improvement",
            reason_text=f"A materially better model is available for the `{label}` role.",
            env_var=env_var, configured_id=configured_id,
            best=best, current_eval=current_eval, label=label, settings=settings)
        return [(title, body, best.model_id)]
    return []


def run_scout_agent(  # noqa: C901  # TODO: split into smaller functions (ticket: split_run_scout_agent)
    *,
    settings: Settings,
    memory: str = "",
) -> ScoutResult:
    """Fetch OpenRouter models, enrich them with endpoint data, and evaluate
    each configured role against its candidate pool.

    Detects regressions (zero providers, preview models, single-provider
    fragility) and material score improvements, then builds draft
    proposals for any findings. The returned :class:`ScoutResult`
    captures the updated memory ledger and draft titles/bodies.

    This is the main seam for testing: tests monkeypatch this function so
    no real network calls are made during test runs.
    """
    proposed_set = _parse_memory(memory)
    client = httpx.Client()

    try:
        all_models = _fetch_models(client, settings)
    except httpx.HTTPError as exc:
        log.warning("Failed to fetch /models: %s", exc)
        return ScoutResult(updated_memory=memory)

    needed_ids: set[str] = set()
    role_configs: dict[str, tuple[str, str, str]] = {}
    for attr, env_var, label in ALL_ROLES:
        configured = getattr(settings, attr, "")
        if configured:
            needed_ids.add(configured)
            role_configs[env_var] = (attr, "", label)
    for roles, tier in [
        (CAPABLE_ROLES, "capable"),
        (STRUCTURED_ROLES, "structured"),
        (CHEAP_ROLES, "cheap"),
    ]:
        for attr, env_var, label in roles:
            if env_var in role_configs:
                role_configs[env_var] = (attr, tier, label)
    for roles, cands in [
        (CAPABLE_ROLES + STRUCTURED_ROLES, CAPABLE_CANDIDATES),
        (CHEAP_ROLES, CHEAP_CANDIDATES),
    ]:
        for cid in cands:
            needed_ids.add(cid)

    enriched: dict[str, ModelInfo] = {}
    for mid in needed_ids:
        base = all_models.get(mid)
        if base is None:
            base = ModelInfo(id=mid, name=mid)
        endpoints = _fetch_endpoints(client, settings, mid)
        enriched[mid] = ModelInfo(
            id=base.id,
            name=base.name,
            context_length=base.context_length or (endpoints[0].context_length if endpoints else 0),
            prompt_price=_pick_price(base.prompt_price, [e.prompt_price for e in endpoints]),
            completion_price=_pick_price(base.completion_price, [e.completion_price for e in endpoints]),
            endpoints=endpoints,
        )

    drafts_titles: list[str] = []
    drafts_bodies: list[str] = []
    new_proposals: list[tuple[str, str, str]] = []

    for env_var, (attr, tier, label) in role_configs.items():
        configured_id = getattr(settings, attr, "")
        if not configured_id:
            continue
        candidate_ids = CHEAP_CANDIDATES if tier == "cheap" else CAPABLE_CANDIDATES
        already = proposed_set.get(env_var, set())
        results = _evaluate_role(
            env_var=env_var, attr=attr, tier=tier, label=label,
            configured_id=configured_id, enriched=enriched,
            candidate_ids=candidate_ids, already=already, settings=settings,
        )
        for title, body, model_id in results:
            drafts_titles.append(title)
            drafts_bodies.append(body)
            new_proposals.append((env_var, model_id, label))

    updated_memory = _build_updated_memory(memory, new_proposals)
    return ScoutResult(
        updated_memory=updated_memory,
        draft_titles=drafts_titles,
        draft_bodies=drafts_bodies,
    )


def _pick_price(
    base: float | None, endpoint_prices: list[float | None],
) -> float | None:
    """Return the first non-``None`` value from *endpoint_prices*, falling back to *base*."""
    for p in endpoint_prices:
        if p is not None:
            return p
    return base


def _providers_text(endpoints: list[EndpointInfo]) -> str:
    """Format a list of :class:`EndpointInfo` objects into a comma-separated
    string of provider names with status labels."""
    if not endpoints:
        return "none"
    return ", ".join(
        f"{e.provider_name}({e.status})" for e in endpoints[:5]
    )


def _eval_summary(e: EvalResult) -> str:
    """Produce a compact one-line summary string of an :class:`EvalResult` for logging."""
    parts = [
        f"providers={e.total_providers}",
        f"active={e.active_providers}",
    ]
    if e.max_uptime is not None:
        parts.append(f"uptime={e.max_uptime:.0%}")
    if e.prompt_price is not None:
        parts.append(f"prompt=${e.prompt_price:.2f}/1M")
    if e.completion_price is not None:
        parts.append(f"completion=${e.completion_price:.2f}/1M")
    if e.max_latency_p99_ms is not None:
        parts.append(f"p99_lat={e.max_latency_p99_ms / 1000:.1f}s")
    if e.min_throughput_p50_tps is not None:
        parts.append(f"p50_tput={e.min_throughput_p50_tps:.0f}tps")
    if e.estimated_slow_generation_s is not None:
        parts.append(f"est_4k_gen={e.estimated_slow_generation_s:.0f}s")
    parts.append(f"tool_calls={e.has_tool_calls}")
    parts.append(f"preview={e.is_preview}")
    if e.flags:
        parts.append(f"flags={_flag_text(e.flags)}")
    parts.append(f"score={e.score:.1f}")
    return ", ".join(parts)


def _best_candidate(candidates: list[EvalResult]) -> EvalResult | None:
    """Return the :class:`EvalResult` with the highest score from *candidates*,
    or ``None`` when the list is empty."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.score)

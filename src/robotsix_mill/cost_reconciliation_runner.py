"""Cost-reconciliation runner — periodic check of OpenRouter vs Langfuse spend.

Fetches yesterday's total spend from both OpenRouter (management API)
and Langfuse (traces API), compares the two totals, and when the
discrepancy exceeds $1.00 invokes the cost-reconciliation agent to
analyse the gap and file a draft ticket.

Seam: tests monkeypatch ``run_cost_reconciliation_agent`` from
``robotsix_mill.agents.cost_reconciling``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import RepoConfig, Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.cost_reconciliation")


@dataclass
class CostReconciliationPassResult:
    """Result of running a cost-reconciliation pass."""

    drafts_created: list[dict]  # [{"id": ..., "title": ...}]
    summary: str
    updated_memory: str = ""
    session_id: str = ""


def _yesterday_utc_range() -> tuple[str, str]:
    """Return ``(from_timestamp, to_timestamp)`` ISO-8601 for yesterday UTC."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start.isoformat(), today_start.isoformat()


def _yesterday_date_str() -> str:
    """Return yesterday as ``YYYY-MM-DD`` (UTC)."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# OpenRouter fetch
# ---------------------------------------------------------------------------


def _fetch_openrouter_daily(
    settings: Settings, date_str: str,
) -> tuple[float, str] | None:
    """Fetch yesterday's spend from the OpenRouter management API.

    Returns ``(total_usd, breakdown_text)`` on success, or ``None``
    when the management key is missing or the API errors.
    """
    secrets = get_secrets()
    key = secrets.openrouter_management_key
    if not key:
        log.warning(
            "cost_reconciliation: openrouter_management_key not set — "
            "skipping OpenRouter fetch"
        )
        return None

    try:
        import httpx
    except ImportError:
        log.warning("cost_reconciliation: httpx not available — skipping")
        return None

    url = f"https://openrouter.ai/api/v1/activity?date={date_str}"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
            )
    except Exception:
        log.warning(
            "cost_reconciliation: OpenRouter API request failed for %s",
            date_str, exc_info=True,
        )
        return None

    if r.status_code == 401 or r.status_code == 403:
        log.warning(
            "cost_reconciliation: OpenRouter API returned %d — "
            "management key may be invalid or lacking permissions",
            r.status_code,
        )
        return None

    if r.status_code != 200:
        log.warning(
            "cost_reconciliation: OpenRouter API returned %d for %s",
            r.status_code, date_str,
        )
        return None

    try:
        data = r.json()
    except ValueError:
        log.warning(
            "cost_reconciliation: OpenRouter API returned non-JSON response"
        )
        return None

    entries = data.get("data", [])
    if not isinstance(entries, list):
        entries = []

    total_usd = 0.0
    breakdown_lines: list[str] = []
    for entry in entries:
        usage = float(entry.get("usage", 0) or 0)
        byok = float(entry.get("byok_usage_inference", 0) or 0)
        model = entry.get("model", "unknown")
        requests = entry.get("num_requests", 0) or 0
        sub_total = usage + byok
        total_usd += sub_total
        breakdown_lines.append(
            f"  {model}: ${sub_total:.4f} (usage=${usage:.4f} "
            f"byok=${byok:.4f}) requests={requests}"
        )

    breakdown = "\n".join(breakdown_lines) if breakdown_lines else "(no entries)"

    log.info(
        "cost_reconciliation: OpenRouter %s total = $%.4f (%d model entries)",
        date_str, total_usd, len(entries),
    )
    return total_usd, breakdown


# ---------------------------------------------------------------------------
# Langfuse fetch
# ---------------------------------------------------------------------------


def _fetch_langfuse_daily(
    settings: Settings, from_ts: str, to_ts: str,
) -> tuple[float, str]:
    """Fetch yesterday's total cost from Langfuse by paginating all
    traces in the UTC day window.

    Returns ``(total_cost, breakdown_text)``.  On API error, returns
    ``(0.0, error message)`` — graceful degradation.
    """
    from .langfuse_client import _langfuse_api_get

    PAGE_SIZE = 100
    EXAMINE_CAP = 2000   # safety cap — a single day shouldn't exceed this

    all_traces: list[dict] = []
    page = 1
    api_ok = False

    try:
        while len(all_traces) < EXAMINE_CAP:
            body = _langfuse_api_get(
                settings,
                "/api/public/traces",
                params={
                    "fromTimestamp": from_ts,
                    "toTimestamp": to_ts,
                    "limit": PAGE_SIZE,
                    "page": page,
                    "orderBy": "timestamp.desc",
                },
            )
            if body is None:
                log.warning(
                    "cost_reconciliation: Langfuse API request failed on page %d",
                    page,
                )
                break

            api_ok = True
            data = body.get("data", [])
            all_traces.extend(data)

            meta = body.get("meta", {})
            total_pages = meta.get("totalPages", 1)
            if page >= total_pages:
                break
            page += 1

    except Exception:
        log.exception("cost_reconciliation: Langfuse fetch failed")
        return 0.0, "Langfuse API error — unable to fetch traces"

    if not api_ok:
        return 0.0, "Langfuse API error — unable to fetch traces"

    traces = all_traces[:EXAMINE_CAP]

    # Aggregate by trace name for the breakdown.
    agg: dict[str, dict] = {}
    total_cost = 0.0
    zero_cost_count = 0

    for t in traces:
        cost = float(t.get("totalCost") or 0)
        total_cost += cost
        name = (t.get("name") or "").strip()
        if not name:
            name = "(unnamed)"
        if name not in agg:
            agg[name] = {"cost": 0.0, "count": 0}
        agg[name]["cost"] += cost
        agg[name]["count"] += 1
        if cost == 0.0:
            zero_cost_count += 1

    breakdown_lines: list[str] = []
    for name in sorted(agg, key=lambda n: agg[n]["cost"], reverse=True):
        entry = agg[name]
        breakdown_lines.append(
            f"  {name}: ${entry['cost']:.4f} ({entry['count']} traces)"
        )

    if zero_cost_count > 0:
        breakdown_lines.append(
            f"\n  ({zero_cost_count} traces with zero cost)"
        )

    breakdown = "\n".join(breakdown_lines) if breakdown_lines else "(no traces)"

    log.info(
        "cost_reconciliation: Langfuse %s → %s total = $%.4f (%d traces, %d pages)",
        from_ts[:10], to_ts[:10], total_cost, len(traces), page,
    )
    return total_cost, breakdown


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_cost_reconciliation_pass(
    session_id: str = "",
    repo_config: "RepoConfig | None" = None,
) -> CostReconciliationPassResult:
    """Execute one cost-reconciliation pass.

    1. Fetch OpenRouter yesterday total.
    2. Fetch Langfuse yesterday total.
    3. Compare.  If delta ≤ $1.00, log clean and return.
    4. If delta > $1.00, invoke agent and create draft ticket.

    Args:
        session_id: Langfuse session id from the poll loop (optional).

    Returns:
        ``CostReconciliationPassResult`` with created draft info.
    """
    settings = Settings()
    service = TicketService(
        settings,
        board_id=(repo_config.board_id if repo_config else ""),
    )

    from_ts, to_ts = _yesterday_utc_range()
    date_str = _yesterday_date_str()

    # --- OpenRouter ----------------------------------------------------
    or_result = _fetch_openrouter_daily(settings, date_str)
    if or_result is None:
        # Management key missing or API failed — skip gracefully.
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=f"OpenRouter fetch skipped (key missing or API error) for {date_str}",
            updated_memory="",
            session_id=session_id,
        )
    or_total, or_breakdown = or_result

    # --- Langfuse ------------------------------------------------------
    lf_total, lf_breakdown = _fetch_langfuse_daily(settings, from_ts, to_ts)

    # --- Compare -------------------------------------------------------
    delta = abs(or_total - lf_total)

    log.info(
        "cost_reconciliation: %s — OR=$%.4f LF=$%.4f delta=$%.4f",
        date_str, or_total, lf_total, delta,
    )

    if delta <= 1.00:
        summary = (
            f"clean: OR=${or_total:.2f} LF=${lf_total:.2f} delta=${delta:.2f}"
        )
        log.info("cost_reconciliation: %s", summary)
        return CostReconciliationPassResult(
            drafts_created=[], summary=summary, updated_memory="", session_id=session_id,
        )

    # --- Agent ---------------------------------------------------------
    from .agents.cost_reconciling import run_cost_reconciliation_agent

    agent_result = run_cost_reconciliation_agent(
        settings=settings,
        openrouter_total=or_total,
        langfuse_total=lf_total,
        delta=delta,
        openrouter_breakdown=or_breakdown,
        langfuse_breakdown=lf_breakdown,
    )

    # --- Draft ticket --------------------------------------------------
    gap_id = date_str
    marker = f"<!-- cost_reconciliation-gap-id: {gap_id} -->"

    title = (
        f"Cost reconciliation: OpenRouter vs Langfuse — "
        f"${delta:.2f} delta on {date_str}"
    )

    body_parts = [
        agent_result.analysis,
        "",
        f"**Conclusion:** {agent_result.conclusion}",
        "",
        "## Raw data",
        "",
        f"- **OpenRouter total:** ${or_total:.4f}",
        f"- **Langfuse total:** ${lf_total:.4f}",
        f"- **Delta:** ${delta:.4f}",
        f"- **Date:** {date_str}",
        "",
        "### OpenRouter breakdown",
        "",
        "```",
        or_breakdown,
        "```",
        "",
        "### Langfuse breakdown",
        "",
        "```",
        lf_breakdown,
        "```",
        "",
        marker,
    ]
    body = "\n".join(body_parts)

    try:
        ticket = service.create(
            title=title,
            description=body,
            source=SourceKind.COST_RECONCILIATION,
        )
        log.info(
            "cost_reconciliation: created draft %s — %s",
            ticket.id, title,
        )
        return CostReconciliationPassResult(
            drafts_created=[{"id": ticket.id, "title": ticket.title}],
            summary=f"delta=${delta:.2f} — draft {ticket.id}",
            updated_memory=getattr(agent_result, "updated_memory", ""),
            session_id=session_id,
        )
    except Exception:
        log.exception("cost_reconciliation: failed to create draft ticket")
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=f"delta=${delta:.2f} — draft creation failed",
            updated_memory=getattr(agent_result, "updated_memory", ""),
            session_id=session_id,
        )

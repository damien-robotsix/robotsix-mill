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

from robotsix_llmio.core import CostWindow

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


def _yesterday_window() -> CostWindow:
    """The most recent fully-settled UTC day [yesterday 00:00, today 00:00)."""
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return CostWindow(start=end - timedelta(days=1), end=end)


def _fmt_breakdown(items: dict) -> str:
    """Render a ``label -> cost`` mapping as a sorted markdown block."""
    if not items:
        return "(no entries)"
    return "\n".join(
        f"  {name}: ${cost:.4f}"
        for name, cost in sorted(items.items(), key=lambda kv: kv[1], reverse=True)
    )


def _fetch_provider_cost(settings, window):
    """OpenRouter-billed cost for *window* via llmio's ProviderCostSource.

    Returns ``(total, breakdown_text)`` or ``None`` when the management key is
    absent or the activity API errors (skip gracefully — never crash the pass).
    """
    key = get_secrets().openrouter_management_key
    if not key:
        log.warning(
            "cost_reconciliation: openrouter_management_key not set — "
            "skipping OpenRouter fetch"
        )
        return None
    from robotsix_llmio.openrouter import OpenRouterProviderCostSource

    try:
        pc = OpenRouterProviderCostSource(management_key=key).fetch_provider_cost(
            window
        )
    except Exception:
        log.warning(
            "cost_reconciliation: OpenRouter activity fetch failed", exc_info=True
        )
        return None
    log.info(
        "cost_reconciliation: OpenRouter %s total = $%.4f (%d requests)",
        window.start.date().isoformat(),
        pc.total_cost,
        pc.request_count,
    )
    return pc.total_cost, _fmt_breakdown(pc.breakdown)


def _fetch_logged_cost(settings, window, repo_config):
    """Langfuse-logged cost for *window* via llmio's CostLogSource.

    Returns ``(total, breakdown_text)``. On API error returns
    ``(0.0, error message)`` — graceful degradation. NOTE: only the most
    recent settled day is reconciled, which is always inside the Langfuse
    retention horizon (time-based prune), so the logged window is complete.
    """
    if repo_config is not None:
        base = repo_config.langfuse_base_url
        pk = repo_config.langfuse_public_key
        sk = repo_config.langfuse_secret_key
    else:
        s = get_secrets()
        base = s.langfuse_base_url
        pk = s.langfuse_public_key
        sk = s.langfuse_secret_key
    if not (pk and sk):
        return 0.0, "no Langfuse credentials"
    from robotsix_llmio.core import LangfuseCostLogSource

    try:
        logged = LangfuseCostLogSource(
            public_key=pk, secret_key=sk, base_url=base
        ).fetch_logged_cost(window)
    except Exception:
        log.exception("cost_reconciliation: Langfuse fetch failed")
        return 0.0, "Langfuse API error — unable to fetch traces"

    agg: dict = {}
    for r in logged.records:
        name = r.name or "(unnamed)"
        agg[name] = agg.get(name, 0.0) + r.cost
    log.info(
        "cost_reconciliation: Langfuse %s total = $%.4f (%d traces)",
        window.start.date().isoformat(),
        logged.total_cost,
        logged.record_count,
    )
    return logged.total_cost, _fmt_breakdown(agg)


def run_cost_reconciliation_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
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
    if repo_config and repo_config.board_id:
        service = TicketService(settings, board_id=repo_config.board_id)
    else:
        service = TicketService(settings)

    window = _yesterday_window()
    date_str = window.start.date().isoformat()

    # --- OpenRouter (provider-billed, via llmio ProviderCostSource) ----
    or_result = _fetch_provider_cost(settings, window)
    if or_result is None:
        # Management key missing or API failed — skip gracefully.
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=f"OpenRouter fetch skipped (key missing or API error) for {date_str}",
            updated_memory="",
            session_id=session_id,
        )
    or_total, or_breakdown = or_result

    # --- Langfuse (logged, via llmio CostLogSource) --------------------
    lf_total, lf_breakdown = _fetch_logged_cost(settings, window, repo_config)

    # --- Compare (llmio reconcile: window-total, flat $1 tolerance) ----
    from robotsix_llmio.core import LoggedCost, ProviderCost, reconcile

    disc = reconcile(
        LoggedCost(total_cost=lf_total, record_count=0),
        ProviderCost(total_cost=or_total),
    )
    delta = disc.delta

    log.info(
        "cost_reconciliation: %s — OR=$%.4f LF=$%.4f delta=$%.4f",
        date_str,
        or_total,
        lf_total,
        delta,
    )

    if disc.within_tolerance:
        summary = f"clean: OR=${or_total:.2f} LF=${lf_total:.2f} delta=${delta:.2f}"
        log.info("cost_reconciliation: %s", summary)
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=summary,
            updated_memory="",
            session_id=session_id,
        )

    # --- Prior-proposal dedup -----------------------------------------
    # Same date already filed? Skip — repeated $1+ deltas on the same
    # day would otherwise produce a duplicate draft per run.
    from .pass_runner import _verify_prior_proposals

    prior = _verify_prior_proposals(
        service,
        settings,
        SourceKind.COST_RECONCILIATION,
    )
    if date_str in prior:
        ticket_id = prior[date_str].get("ticket_id", "?")
        summary = f"already filed: {ticket_id} (date={date_str})"
        log.info("cost_reconciliation: %s", summary)
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=summary,
            updated_memory="",
            session_id=session_id,
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
            ticket.id,
            title,
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

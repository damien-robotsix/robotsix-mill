"""Cost-reconciliation runner — periodic check of OpenRouter vs Langfuse spend.

Fetches yesterday's total spend from both OpenRouter (management API)
and Langfuse (traces API), compares the two totals, and when the
discrepancy exceeds $1.00 files a draft ticket whose body is built
deterministically from the raw numbers — no LLM call is made on any
path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from robotsix_llmio.core import CostWindow

from ..config import RepoConfig, Settings, get_secrets
from ..core.models import SourceKind
from ..core.service import TicketService

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


def _fetch_logged_cost(settings, window, repo_config, *, provider=None):
    """Langfuse-logged cost for *window* via llmio's CostLogSource.

    When *provider* is set (e.g. ``"openrouter"``) only the slice of logged
    cost stamped with that provider tag is summed — the per-key pass uses this
    so the logged side matches the OpenRouter key's billing scope (Claude SDK
    spend, which an OpenRouter key never bills, is excluded → 0-vs-0 on a
    claude_sdk fleet instead of a false discrepancy).

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
        source = LangfuseCostLogSource(public_key=pk, secret_key=sk, base_url=base)
        if provider:
            logged = source.fetch_logged_cost_by_provider(window, provider)
        else:
            logged = source.fetch_logged_cost(window)
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


def _file_discrepancy(
    settings,
    service,
    *,
    date_str: str,
    or_total: float,
    lf_total: float,
    delta: float,
    or_breakdown: str,
    lf_breakdown: str,
    session_id: str,
) -> CostReconciliationPassResult:
    """Dedup → file a deterministic draft for an over-tolerance discrepancy.

    Shared by the account-level and per-key passes. The draft body is built
    deterministically from the raw numbers — no LLM call is made on any path.
    """
    from .pass_runner import _verify_prior_proposals

    prior = _verify_prior_proposals(service, settings, SourceKind.COST_RECONCILIATION)
    if date_str in prior:
        ticket_id = prior[date_str].get("ticket_id", "?")
        summary = f"already filed: {ticket_id} (date={date_str})"
        log.info("cost_reconciliation: %s", summary)
        return CostReconciliationPassResult(
            drafts_created=[], summary=summary, session_id=session_id
        )

    marker = f"<!-- cost_reconciliation-gap-id: {date_str} -->"
    title = f"OpenRouter vs Langfuse — ${delta:.2f} delta on {date_str}"
    header = (
        "Automated cost-reconciliation check: OpenRouter vs Langfuse spend "
        f"diverged by more than the $1.00 tolerance on {date_str}. Raw figures "
        "below — no automated analysis is performed; investigate manually if "
        "needed."
    )
    body = "\n".join(
        [
            header,
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
    )
    try:
        ticket = service.create(
            title=title, description=body, source=SourceKind.COST_RECONCILIATION
        )
        log.info("cost_reconciliation: created draft %s — %s", ticket.id, title)
        return CostReconciliationPassResult(
            drafts_created=[{"id": ticket.id, "title": ticket.title}],
            summary=f"delta=${delta:.2f} — draft {ticket.id}",
            updated_memory="",
            session_id=session_id,
        )
    except Exception:
        log.exception("cost_reconciliation: failed to create draft ticket")
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=f"delta=${delta:.2f} — draft creation failed",
            updated_memory="",
            session_id=session_id,
        )


def _key_snapshot_path(settings, board_id: str):
    from pathlib import Path

    return (
        Path(settings.data_dir) / (board_id or "default") / "openrouter_key_usage.json"
    )


def _load_key_snapshot(path):
    """Return ``(cumulative_usd, snapshot_at)`` or ``None`` (no prior snapshot)."""
    import json

    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return float(d["cumulative"]), datetime.fromisoformat(d["at"])
    except OSError, ValueError, KeyError:
        return None


def _save_key_snapshot(path, cumulative: float, at: datetime) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cumulative": cumulative, "at": at.isoformat()}),
        encoding="utf-8",
    )


def _run_per_key_pass(
    settings, service, repo_config: RepoConfig, session_id: str
) -> CostReconciliationPassResult:
    """Per-project reconcile: this repo's OpenRouter key usage (snapshot+diff)
    vs its Langfuse-logged cost over ``[last-snapshot, now]``."""
    from robotsix_llmio.core import CostWindow, LoggedCost, ProviderCost, reconcile
    from robotsix_llmio.openrouter import OpenRouterKeyCostSource

    now = datetime.now(timezone.utc)
    snap_path = _key_snapshot_path(settings, repo_config.board_id)
    try:
        cur = OpenRouterKeyCostSource(
            api_key=repo_config.openrouter_api_key
        ).fetch_key_usage()
    except Exception:
        log.warning(
            "cost_reconciliation[per-key %s]: usage fetch failed",
            repo_config.repo_id,
            exc_info=True,
        )
        return CostReconciliationPassResult(
            drafts_created=[],
            summary="per-key usage fetch failed",
            session_id=session_id,
        )

    prev = _load_key_snapshot(snap_path)
    _save_key_snapshot(snap_path, cur.usage, now)
    if prev is None:
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=f"per-key baseline recorded (usage=${cur.usage:.2f})",
            session_id=session_id,
        )

    prev_cum, prev_at = prev
    or_total = max(0.0, cur.usage - prev_cum)
    window = CostWindow(start=prev_at, end=now)
    # An OpenRouter key only bills the OpenRouter slice, so reconcile the logged
    # side filtered to provider="openrouter" — Claude SDK spend is excluded.
    lf_total, lf_breakdown = _fetch_logged_cost(
        settings, window, repo_config, provider="openrouter"
    )

    disc = reconcile(
        LoggedCost(total_cost=lf_total, record_count=0),
        ProviderCost(total_cost=or_total),
    )
    date_str = now.date().isoformat()
    log.info(
        "cost_reconciliation[per-key %s]: OR=$%.4f LF=$%.4f delta=$%.4f window=%s..%s",
        repo_config.repo_id,
        or_total,
        lf_total,
        disc.delta,
        prev_at.isoformat(),
        now.isoformat(),
    )
    if disc.within_tolerance:
        return CostReconciliationPassResult(
            drafts_created=[],
            summary=(
                f"clean (per-key {repo_config.repo_id}): "
                f"OR=${or_total:.2f} LF=${lf_total:.2f} delta=${disc.delta:.2f}"
            ),
            session_id=session_id,
        )
    return _file_discrepancy(
        settings,
        service,
        date_str=date_str,
        or_total=or_total,
        lf_total=lf_total,
        delta=disc.delta,
        or_breakdown=f"(per-key {repo_config.repo_id}) usage delta over "
        f"{prev_at.date().isoformat()}..{date_str}",
        lf_breakdown=lf_breakdown,
        session_id=session_id,
    )


def run_cost_reconciliation_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> CostReconciliationPassResult:
    """Execute one cost-reconciliation pass.

    1. Fetch OpenRouter yesterday total.
    2. Fetch Langfuse yesterday total.
    3. Compare.  If delta ≤ $1.00, log clean and return.
    4. If delta > $1.00, create a draft ticket whose body is built
       deterministically from the raw numbers (no LLM call).

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

    # Per-key mode: when this repo carries its own OpenRouter key, reconcile
    # ITS key usage (cumulative snapshot + diff) against ITS Langfuse project,
    # over the [last-snapshot, now] window — so the provider and logged sides
    # cover the same period and provider spend is attributed per-project.
    if repo_config is not None and repo_config.openrouter_api_key:
        return _run_per_key_pass(settings, service, repo_config, session_id)

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

    return _file_discrepancy(
        settings,
        service,
        date_str=date_str,
        or_total=or_total,
        lf_total=lf_total,
        delta=delta,
        or_breakdown=or_breakdown,
        lf_breakdown=lf_breakdown,
        session_id=session_id,
    )

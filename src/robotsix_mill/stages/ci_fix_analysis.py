"""CI-failure analysis helpers for the ci-fix stage.

Extracted from :mod:`.ci_fix` to keep that module focused.  These are the
stateless failure-analysis functions (merge-conflict detection, failure-detail
assembly, failure-source fetching, and failing-summary artifact persistence);
``CIFixStage`` re-exports the ones it still calls as thin methods.

They are plain module-level functions with no dependency on ``CIFixStage``
instance state.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.github_code_scanning import CodeScanningAlertsUnavailable
from .base import Outcome, StageContext
from .ci_fix_helpers import (
    _bounded_multi_run_log_text,
    _build_compact_failing_summary,
    _build_failing_summary,
    _pr_changed_paths,
    _write_text,
)

log = logging.getLogger("robotsix_mill.stages.ci_fix_analysis")


def _check_merge_conflict(
    ticket: Ticket,
    ctx: StageContext,
    repo_dir: str,
    branch: str,
    target: str,
    *,
    _detect_merge_conflict: Any,
) -> Outcome | None:
    """Check whether the PR branch has merge conflicts with *target*.

    Calls the forge's ``pr_status`` to read ``mergeable_state``, then
    delegates to :func:`_detect_merge_conflict` to build a block note
    when a conflict is detected.

    Returns:
        ``Outcome(State.BLOCKED, ...)`` when a merge conflict is
        detected, or ``None`` when the branch is mergeable (or the
        forge cannot be reached — fall through to normal CI-fix).
    """
    s = ctx.settings

    # Only applicable when a forge is configured.
    if s.forge_kind == "none":
        return None

    try:
        forge = get_forge(s, repo_config=ctx.repo_config)
        pr = forge.pr_status(source_branch=branch)
    except Exception:
        log.warning(
            "%s: pr_status failed during merge-conflict check",
            ticket.id,
            exc_info=True,
        )
        return None

    reason = _detect_merge_conflict(ticket.id, repo_dir, target, pr)
    if reason is None:
        return None

    # Hand the conflict to the rebase agent instead of blocking for a
    # human.  The merge stage already does exactly this from
    # human_mr_approval and waiting_auto_merge ("Route directly to
    # REBASING … without operator action"); ci_fix was the one conflict
    # path that still demanded a manual rebase, which is why 10 tickets
    # sat blocked on 2026-08-12 with nothing wrong but a moved target
    # branch.  The rebase stage owns the bound: it retries up to
    # ``_REBASE_COUNTER``/max_attempts and blocks with its own note when
    # the conflict really is unresolvable, so this cannot loop forever.
    # On success it returns IMPLEMENT_COMPLETE, the merge stage
    # re-verifies the gates, and a still-red CI lands back here.
    log.warning(
        "%s: merge conflict detected — routing to REBASING (mergeable_state=%s)",
        ticket.id,
        pr.get("mergeable_state") if pr else "N/A",
    )
    return Outcome(State.REBASING, reason)


def _build_failure_detail(
    ticket: Ticket,
    ctx: StageContext,
    branch: str,
    failing: list[dict[str, Any]],
    compact: bool = False,
) -> tuple[str, list[dict[str, Any]], set[str], bool, str, list[int], list[str]]:
    """Enrich the failing-check list with job logs + code-scanning alerts.

    Returns ``(failing_summary, alerts, changed_paths, alerts_unreadable,
    head_sha, failing_run_ids, failing_run_urls)`` so callers can inspect
    the raw alert data (e.g. for FP triage gating), detect when alerts
    were unreadable due to a 403 permission gap, include the branch HEAD
    SHA in the failure fingerprint, and pass a run_id or run_url to
    ``fetch_ci_logs``.

    When *compact* is True the returned *failing_summary* is a bounded
    digest (see ``_build_compact_failing_summary``) instead of the full
    inline detail — used for the 2nd and later ``wait_for_ci`` iterations
    so the pydantic-ai transcript stops growing with loop depth.
    """
    s = ctx.settings

    alerts: list[dict[str, Any]] = []
    changed_paths: set[str] = set()
    alerts_unreadable = False
    head_sha = ""
    failing_run_ids: list[int] = []
    failing_run_urls: list[str] = []
    run_blocks: list[tuple[str, str]] = []

    try:
        forge = get_forge(s, repo_config=ctx.repo_config)
        (
            alerts,
            changed_paths,
            head_sha,
            failing_run_ids,
            failing_run_urls,
            run_blocks,
        ) = _fetch_failure_sources(forge, branch)
    except CodeScanningAlertsUnavailable:
        log.warning(
            "%s: code-scanning alerts unreadable (HTTP 403) — "
            "token lacks 'security-events' permission",
            ticket.id,
        )
        alerts_unreadable = True
        # Still try to fetch changed_paths and job logs — do not lose
        # log enrichment just because alerts are unreadable.
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            (
                alerts,
                changed_paths,
                head_sha,
                failing_run_ids,
                failing_run_urls,
                run_blocks,
            ) = _fetch_failure_sources(forge, branch, fetch_alerts=False)
        except Exception:
            log.warning("%s: failed to fetch job logs", ticket.id)
    except Exception:
        log.warning("%s: failed to fetch job logs / alerts", ticket.id)

    log_text = _bounded_multi_run_log_text(run_blocks, s.ci_fix_log_context_max_chars)

    if compact and s.ci_fix_iteration_summary_max_chars > 0:
        summary = _build_compact_failing_summary(
            failing,
            log_text,
            alerts,
            changed_paths,
            s.ci_fix_iteration_summary_max_chars,
        )
    else:
        summary = _build_failing_summary(
            failing,
            log_text,
            alerts,
            changed_paths,
            max_annotations=s.ci_fix_max_annotations,
            max_alerts=s.ci_fix_max_alerts,
        )

    return (
        summary,
        alerts,
        changed_paths,
        alerts_unreadable,
        head_sha,
        failing_run_ids,
        failing_run_urls,
    )


def _fetch_failure_sources(
    forge: Any,
    branch: str,
    fetch_alerts: bool = True,
) -> tuple[
    list[dict[str, Any]],
    set[str],
    str,
    list[int],
    list[str],
    list[tuple[str, str]],
]:
    """Fetch alerts, changed paths, PR head SHA, and per-run job logs.

    Returns ``(alerts, changed_paths, head_sha, failing_run_ids,
    failing_run_urls, run_blocks)`` where *run_blocks* is a list of
    ``(header, log_body)`` pairs, one per failing workflow run.

    *fetch_alerts* lets the 403-alerts fallback path skip the
    code-scanning call (which would re-raise ``CodeScanningAlertsUnavailable``)
    while still fetching changed paths and job logs.
    """
    alerts = (
        forge.list_code_scanning_alerts(source_branch=branch) if fetch_alerts else []
    )
    changed_paths = _pr_changed_paths(forge, branch)
    pr = forge.pr_status(source_branch=branch)
    head_sha = (pr or {}).get("sha", "")
    failing_run_ids: list[int] = []
    failing_run_urls: list[str] = []
    run_blocks: list[tuple[str, str]] = []
    if head_sha:
        try:
            runs = forge.list_workflow_runs(head_sha=head_sha)
        except Exception:
            log.warning(
                "failed to list workflow runs for head %s — no run ids/logs",
                head_sha,
            )
            runs = []
        for run in runs:
            if run.get("conclusion") == "failure":
                failing_run_ids.append(run["id"])
                run_url = run.get("html_url", "")
                if run_url:
                    failing_run_urls.append(run_url)
                try:
                    logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                except Exception:
                    log.warning(
                        "failed to fetch job logs for run %s",
                        run["id"],
                        exc_info=True,
                    )
                    logs = ""
                if logs:
                    url_note = f", url: {run_url}" if run_url else ""
                    header = (
                        f"\n--- {run.get('name', 'workflow')} "
                        f"(run {run['id']}{url_note}) ---\n"
                    )
                    run_blocks.append((header, logs))
    return (
        alerts,
        changed_paths,
        head_sha,
        failing_run_ids,
        failing_run_urls,
        run_blocks,
    )


def _write_failing_summary_artifact(
    ctx: StageContext,
    ticket: Ticket,
    failing_summary: str,
    failing: list[dict[str, Any]],
) -> None:
    """Persist the failure detail to ``failing_summary.txt``.

    Best-effort: a write failure is logged, not raised.
    If *failing_summary* is empty, falls back to the raw check names
    so the file is never silently empty.
    """
    try:
        content = failing_summary.strip()
        if not content:
            names = [chk.get("name", "?") for chk in failing]
            content = f"(no detail available) failing checks: {', '.join(names)}"
        path = ctx.service.workspace(ticket).artifacts_dir / "failing_summary.txt"
        _write_text(path, content)
    except Exception:
        log.exception("%s: failed to write failing_summary.txt artifact", ticket.id)

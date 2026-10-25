"""Foreign-PR disposition for the orphaned-PR check — split out of
``orphaned_pr_check``.

Handles open PRs on branches mill did not create: tracking tickets (or a
daily digest), auto-disposal of mergeable/superseded foreign PRs, and
collapsing legacy per-PR trackers.  Entry point: ``_process_foreign_prs``,
called from ``run_orphaned_pr_check_pass``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...config import RepoConfig, Settings
from ...core.models import SourceKind, Ticket
from ...core.service import TicketService
from ...core.states import State

if TYPE_CHECKING:
    from ...scm.forge.base import Forge
    from .orphaned_pr_check import OrphanedPrCheckResult

log = logging.getLogger("robotsix_mill.agents.runners.orphaned_pr_check")


def _process_foreign_prs(
    foreign_prs: list[dict[str, Any]],
    settings: Settings,
    service: TicketService,
    forge: Forge,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
    all_orphan_titles: frozenset[str] = frozenset(),
) -> None:
    """Dispose of FOREIGN (non-board) open PRs.

    A foreign PR is one whose head branch does NOT start with
    ``settings.branch_prefix`` (e.g. a human ``feature/*`` branch or a
    fleet agent's ``fix/*`` branch).  Foreign PRs are NEVER closed — the
    mill only merges green ones or files a tracking ticket so the board
    can review and merge or close them.

    Foreign PRs whose head branch starts with one of
    ``settings.orphaned_pr_foreign_ignore_branch_prefixes`` (dependabot /
    renovate bumps, release-please release PRs, pin-bump runners) are
    owned by other automation and are ignored outright: no ticket, no
    cap consumption — they are only logged as ``IGNORED_PREFIX``.

    Disposition of the remaining candidates depends on
    ``settings.orphaned_pr_auto_dispose_foreign_prs``:

    - **off** (legacy): file one deterministic tracking ticket per PR,
      capped by the shared per-pass action caps.
    - **on**: check each PR's live CI + mergeability.  Green, conflict-free
      PRs are merged directly; the rest are consolidated into a single
      per-board-per-day digest ticket so the human gate sees at most one
      tracking ticket per board.
    """
    ignore_prefixes = tuple(settings.orphaned_pr_foreign_ignore_branch_prefixes)

    candidates: list[dict[str, Any]] = []
    for pr in foreign_prs:
        branch = pr["branch"]
        if ignore_prefixes and branch.startswith(ignore_prefixes):
            log_line = (
                f"repo={repo_config.repo_id} branch={branch} "
                f"ticket_state=NOT_FOUND action=IGNORED_PREFIX "
                f"classification=foreign_pr "
                f"dry_run={settings.orphaned_pr_dry_run}"
            )
            log.info("orphaned-pr-check: %s", log_line)
            result.actions.append(log_line)
            result.foreign_ignored += 1
            continue
        candidates.append(pr)

    if settings.orphaned_pr_auto_dispose_foreign_prs:
        _auto_dispose_foreign_prs(
            candidates, settings, service, forge, repo_config, result, all_orphan_titles
        )
    else:
        _file_foreign_tracking_tickets(
            candidates, settings, service, repo_config, result, all_orphan_titles
        )


def _file_foreign_tracking_tickets(
    candidates: list[dict[str, Any]],
    settings: Settings,
    service: TicketService,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
    all_orphan_titles: frozenset[str] = frozenset(),
) -> None:
    """Legacy path: file one tracking ticket per foreign PR.

    File actions count against the same combined
    (``orphaned_pr_max_actions_per_pass``) and per-type file
    (``orphaned_pr_max_files_per_pass``) caps as the mill-PR phase, using
    the counts already accumulated on *result*.
    """
    max_actions = settings.orphaned_pr_max_actions_per_pass
    max_files = settings.orphaned_pr_max_files_per_pass

    for idx, pr in enumerate(candidates):
        combined_taken = result.closed + result.filed + result.foreign_filed
        files_taken = result.filed + result.foreign_filed
        if combined_taken >= max_actions or files_taken >= max_files:
            remaining = len(candidates) - idx
            cap_msg = (
                f"orphaned-pr-check: foreign action cap reached "
                f"(files={files_taken}/{max_files}, "
                f"total={combined_taken}/{max_actions}) — "
                f"{remaining} foreign PR(s) remain unprocessed"
            )
            log.info(cap_msg)
            result.actions.append(cap_msg)
            break

        branch = pr["branch"]
        title = _foreign_ticket_title(repo_config, pr)
        is_dedup = title in all_orphan_titles
        action = "DEDUP_SKIP" if is_dedup else "FILE_TICKET"
        log_line = (
            f"repo={repo_config.repo_id} branch={branch} "
            f"ticket_state=NOT_FOUND action={action} "
            f"classification=foreign_pr "
            f"dry_run={settings.orphaned_pr_dry_run}"
        )
        log.info("orphaned-pr-check: %s", log_line)
        result.actions.append(log_line)

        if settings.orphaned_pr_dry_run or is_dedup:
            result.foreign_skipped += 1
            continue

        _file_foreign_ticket(service, repo_config, pr, title)
        result.foreign_filed += 1


# ------------------------------------------------------------------ auto-dispose


def _log_foreign_action(
    result: OrphanedPrCheckResult,
    repo_config: RepoConfig,
    branch: str,
    action: str,
    classification: str,
    dry_run: bool,
) -> None:
    """Emit (and record) a structured foreign-PR action log line."""
    log_line = (
        f"repo={repo_config.repo_id} branch={branch} "
        f"ticket_state=NOT_FOUND action={action} "
        f"classification={classification} dry_run={dry_run}"
    )
    log.info("orphaned-pr-check: %s", log_line)
    result.actions.append(log_line)


def _foreign_pr_is_mergeable(forge: Forge, branch: str) -> tuple[bool, str]:
    """Decide whether a foreign PR is safe to auto-merge.

    Returns ``(True, "")`` only when the PR is open, the forge reports it
    conflict-free (``mergeable is True``), and live CI concluded
    ``success``.  Otherwise returns ``(False, reason)`` where *reason*
    explains why the PR needs human review (used in the digest body).
    """
    pr_info = forge.pr_status(source_branch=branch)
    if pr_info is None or pr_info.get("state") != "open":
        return False, "PR is no longer open"
    mergeable = pr_info.get("mergeable")
    if mergeable is False:
        return False, "merge conflicts with the target branch"

    ci = forge.check_status(source_branch=branch)
    conclusion = ci.get("conclusion") if ci else None
    if conclusion == "failure":
        return False, "CI is failing"
    if conclusion == "pending":
        return False, "CI is still running"
    if conclusion != "success":
        return False, "no CI checks reported — cannot confirm green"
    if mergeable is None:
        return False, "mergeability not yet computed by the forge"
    return True, ""


def _auto_dispose_foreign_prs(
    candidates: list[dict[str, Any]],
    settings: Settings,
    service: TicketService,
    forge: Forge,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
    all_orphan_titles: frozenset[str] = frozenset(),
) -> None:
    """Auto-dispose foreign PRs without flooding the human gate.

    Green, conflict-free PRs are merged directly (standard path); every
    other PR (red / pending / conflicted / ambiguous / merge-failed /
    merge-deferred) is folded into a single per-board-per-day digest
    ticket.  Legacy per-PR ``Track external PR`` shells are collapsed
    into the digest so the human gate sees at most one tracking ticket
    per board.
    """
    dry_run = settings.orphaned_pr_dry_run
    max_actions = settings.orphaned_pr_max_actions_per_pass
    merges_taken = 0
    digest: list[tuple[dict[str, Any], str]] = []

    for pr in candidates:
        branch = pr["branch"]
        green, reason = _foreign_pr_is_mergeable(forge, branch)

        if not green:
            _log_foreign_action(
                result, repo_config, branch, "DIGEST", "foreign_blocked", dry_run
            )
            digest.append((pr, reason))
            continue

        if dry_run:
            _log_foreign_action(
                result, repo_config, branch, "MERGE", "foreign_green", dry_run
            )
            result.foreign_skipped += 1
            continue

        if merges_taken >= max_actions:
            _log_foreign_action(
                result, repo_config, branch, "DIGEST", "foreign_green_deferred", dry_run
            )
            digest.append((pr, "auto-merge deferred — per-pass action cap reached"))
            continue

        _log_foreign_action(
            result, repo_config, branch, "MERGE", "foreign_green", dry_run
        )
        merge_result = forge.merge_pr(source_branch=branch)
        if merge_result.get("merged"):
            result.foreign_merged += 1
            merges_taken += 1
            continue
        digest.append(
            (pr, f"auto-merge failed: {merge_result.get('reason', 'unknown')}")
        )

    if digest:
        _file_foreign_digest_ticket(
            digest, settings, service, repo_config, result, all_orphan_titles
        )

    _collapse_legacy_foreign_trackers(service, settings, repo_config, result)


def _foreign_digest_title(repo_config: RepoConfig, date: str) -> str:
    """Return the deterministic per-board-per-day digest ticket title."""
    return f"External PR review digest: {repo_config.repo_id} {date}"


def _build_digest_body(
    repo_config: RepoConfig, digest: list[tuple[dict[str, Any], str]]
) -> str:
    """Render the digest ticket body listing every blocked foreign PR."""
    header = (
        f"{len(digest)} external (non-mill) open PR(s) on "
        f"`{repo_config.repo_id}` need human review — each has failing or "
        f"pending CI, merge conflicts, or could not be auto-confirmed green.\n"
    )
    entries: list[str] = []
    for pr, reason in digest:
        number = pr.get("number")
        pr_label = f"#{number}" if number is not None else "(number unknown)"
        entries.append(
            f"- PR {pr_label} — {pr.get('title') or '(no title)'}\n"
            f"  - URL: {pr.get('url') or '(none)'}\n"
            f"  - Author: {pr.get('author_login') or '(unknown)'}\n"
            f"  - Branch: `{pr['branch']}`\n"
            f"  - Reason: {reason}"
        )
    footer = "\nReview each PR and merge if appropriate, or close if obsolete."
    return "\n".join([header, *entries, footer])


def _file_foreign_digest_ticket(
    digest: list[tuple[dict[str, Any], str]],
    settings: Settings,
    service: TicketService,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
    all_orphan_titles: frozenset[str],
) -> None:
    """File (or dedup-skip) the single per-board-per-day digest ticket."""
    date = datetime.now(UTC).date().isoformat()
    title = _foreign_digest_title(repo_config, date)
    is_dedup = title in all_orphan_titles
    action = "DEDUP_SKIP" if is_dedup else "FILE_DIGEST"
    log_line = (
        f"repo={repo_config.repo_id} branch=- "
        f"ticket_state=NOT_FOUND action={action} "
        f"classification=foreign_digest count={len(digest)} "
        f"dry_run={settings.orphaned_pr_dry_run}"
    )
    log.info("orphaned-pr-check: %s", log_line)
    result.actions.append(log_line)

    if settings.orphaned_pr_dry_run or is_dedup:
        result.foreign_skipped += 1
        return

    service.create(
        title=title,
        description=_build_digest_body(repo_config, digest),
        source=SourceKind.ORPHANED_PR_CHECK,
    )
    result.foreign_filed += 1


def _collapse_legacy_foreign_trackers(
    service: TicketService,
    settings: Settings,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
) -> None:
    """Close open per-PR ``Track external PR`` shells for this board.

    Under auto-dispose the per-board-per-day digest supersedes the legacy
    one-ticket-per-PR tracking shells, so any still-open shell is folded
    into the digest and auto-closed.  Respects dry-run.
    """
    _ACTIVE_STATES_EXCL = {
        State.DONE,
        State.CLOSED,
        State.ANSWERED,
        State.EPIC_CLOSED,
        State.ERRORED,
    }
    pattern = re.compile(rf"Track external PR: {re.escape(repo_config.repo_id)}[#/].*")
    all_tickets: list[Ticket] = service.recent_proposals_for(
        source=SourceKind.ORPHANED_PR_CHECK, limit=500
    )
    for ticket in all_tickets:
        if ticket.state in _ACTIVE_STATES_EXCL:
            continue
        if not pattern.fullmatch(ticket.title):
            continue
        action_label = (
            "COLLAPSE_TRACKER"
            if not settings.orphaned_pr_dry_run
            else "DRY_RUN_COLLAPSE"
        )
        log_line = (
            f"repo={repo_config.repo_id} ticket={ticket.id} "
            f"title={ticket.title!r} action={action_label}"
        )
        log.info("orphaned-pr-check: %s", log_line)
        result.actions.append(log_line)

        if settings.orphaned_pr_dry_run:
            result.foreign_skipped += 1
        else:
            service.close_tracker(
                ticket.id,
                note=(
                    "Superseded by the daily external-PR review digest — "
                    "auto-collapsed by the orphaned-PR pass"
                ),
            )


def _foreign_ticket_title(repo_config: RepoConfig, pr: dict[str, Any]) -> str:
    """Return the deterministic tracking-ticket title for a foreign PR.

    Prefers the stable PR number; falls back to the branch when the
    forge dict lacks a number.
    """
    number = pr.get("number")
    if number is not None:
        return f"Track external PR: {repo_config.repo_id}#{number}"
    return f"Track external PR: {repo_config.repo_id}/{pr['branch']}"


def _file_foreign_ticket(
    service: TicketService,
    repo_config: RepoConfig,
    pr: dict[str, Any],
    title: str,
) -> None:
    """File a tracking ticket for a foreign (non-board) open PR.

    Dedup is handled by the caller (:func:`_process_foreign_prs`) before
    this function is invoked — the caller checks ``all_orphan_titles``
    against the deterministic title and skips creation when a match is
    found.
    """
    number = pr.get("number")
    pr_label = f"#{number}" if number is not None else "(number unknown)"
    body = (
        f"An external (non-mill) open PR has no active mill tracking ticket.\n\n"
        f"- Repo: `{repo_config.repo_id}`\n"
        f"- PR: {pr_label}\n"
        f"- URL: {pr.get('url') or '(none)'}\n"
        f"- Author: {pr.get('author_login') or '(unknown)'}\n"
        f"- Branch: `{pr['branch']}`\n"
        f"- Title: {pr.get('title') or '(none)'}\n\n"
        f"External PR with no mill ticket — review and merge if appropriate, "
        f"or close if obsolete."
    )
    service.create(
        title=title,
        description=body,
        source=SourceKind.ORPHANED_PR_CHECK,
    )

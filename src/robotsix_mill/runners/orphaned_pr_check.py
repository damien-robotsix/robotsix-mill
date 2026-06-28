"""Orphaned-PR check — deterministic per-repo periodic pass.

Lists open PRs for a managed repo, classifies mill-authored ones as
orphaned when no active ticket drives them, and either auto-closes the
PR (with a comment) or files a tracking ticket so the work is picked
back up.  No LLM agent — pure Python.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..config import Settings
from ..config import RepoConfig
from ..core.models import SourceKind, Ticket
from ..core.service import TicketService
from ..core.states import State
from ..forge import get_forge
from ..runtime.tracing import make_session_id

if TYPE_CHECKING:
    from ..forge.base import Forge

log = logging.getLogger(__name__)

# Ticket states that cause a PR to be classified as orphaned.
_ORPHAN_STATES: frozenset[State] = frozenset({State.DONE, State.CLOSED, State.ERRORED})

# Subset of orphan states that trigger an auto-close (DONE/CLOSED).
# ERRORED tickets with a non-empty diff get a tracking ticket instead.
_CLOSE_ON_STATES: frozenset[State] = frozenset({State.DONE, State.CLOSED})


@dataclass
class OrphanedPrCheckResult:
    """Result of one orphaned-PR check pass for a single repo."""

    repo_id: str
    total_scanned: int = 0
    closed: int = 0
    filed: int = 0
    skipped: int = 0
    dry_run: bool = True
    actions: list[str] = field(default_factory=list)  # log lines for audit trail


def run_orphaned_pr_check_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> OrphanedPrCheckResult:
    """Run one orphaned-PR check pass for *repo_config*.

    Args:
        session_id: Tracing session id.  Generated automatically when
            empty.
        repo_config: Managed repo to scan.  Required.

    Returns:
        ``OrphanedPrCheckResult`` with counts and an audit log of every
        action (or would-be action under dry-run).
    """
    if repo_config is None:
        raise ValueError("orphaned_pr_check requires a repo_config")

    if not session_id:
        session_id = make_session_id("orphaned-pr-check")

    settings = Settings()
    service = TicketService(settings, board_id=repo_config.board_id)
    forge = get_forge(settings, repo_config=repo_config)
    result = OrphanedPrCheckResult(
        repo_id=repo_config.repo_id,
        dry_run=settings.orphaned_pr_dry_run,
    )

    open_branches: set[str] = forge.list_open_pr_branches()
    mill_branches = {b for b in open_branches if b.startswith(settings.branch_prefix)}
    result.total_scanned = len(mill_branches)

    _classify_branches(
        sorted(mill_branches),
        settings,
        service,
        forge,
        repo_config,
        result,
    )
    return result


def _classify_branches(
    mill_branches: list[str],
    settings: Settings,
    service: TicketService,
    forge: "Forge",
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
) -> None:
    """Iterate sorted mill branches, classify each, and update *result*."""
    actions_taken = 0
    max_actions = settings.orphaned_pr_max_actions_per_pass

    for branch in mill_branches:
        if actions_taken >= max_actions:
            remaining = len(mill_branches) - actions_taken
            cap_msg = (
                f"orphaned-pr-check: action cap "
                f"({max_actions}) reached — "
                f"{remaining} branch(es) remain unprocessed"
            )
            log.info(cap_msg)
            result.actions.append(cap_msg)
            break

        ticket_id = branch.removeprefix(settings.branch_prefix)

        # --- Age guard ---
        ticket: Ticket | None = service.get(ticket_id)
        if ticket is not None:
            age = datetime.now(timezone.utc) - ticket.created_at
            if age < timedelta(hours=settings.orphaned_pr_min_age_hours):
                log.debug(
                    "orphaned-pr-check: %s/%s too young (%s) — skipping",
                    repo_config.repo_id,
                    branch,
                    age,
                )
                result.skipped += 1
                continue

        # --- Orphan classification ---
        is_orphan = (ticket is None) or (ticket.state in _ORPHAN_STATES)
        if not is_orphan:
            continue  # mill is actively driving this PR

        # --- Guard: PR may already be closed on forge ---
        pr_info = forge.pr_status(source_branch=branch)
        if pr_info is None or pr_info.get("state") != "open":
            continue  # already closed/merged

        # --- Action decision ---
        should_close = (
            ticket is not None and ticket.state in _CLOSE_ON_STATES
        ) or _pr_has_empty_diff(forge, branch)
        action = "CLOSE" if should_close else "FILE_TICKET"
        state_label = ticket.state.value if ticket else "NOT_FOUND"
        log_line = (
            f"repo={repo_config.repo_id} branch={branch} "
            f"ticket_state={state_label} action={action} "
            f"dry_run={settings.orphaned_pr_dry_run}"
        )
        log.info("orphaned-pr-check: %s", log_line)
        result.actions.append(log_line)

        if settings.orphaned_pr_dry_run:
            result.skipped += 1
            continue

        if should_close:
            comment = _build_close_comment(repo_config.repo_id, branch, ticket)
            forge.post_pr_comment(source_branch=branch, body=comment)
            forge.close_pr(source_branch=branch)
            result.closed += 1
        else:
            _file_orphan_ticket(service, settings, repo_config, branch, ticket)
            result.filed += 1
        actions_taken += 1


# ------------------------------------------------------------------ helpers


def _pr_has_empty_diff(forge: "Forge", branch: str) -> bool:
    """Return True when the PR for *branch* has no effective file changes."""
    files = forge.pr_files(source_branch=branch)
    if not files:
        return True
    return all(f.get("additions", 0) + f.get("deletions", 0) == 0 for f in files)


def _build_close_comment(
    repo_id: str,
    branch: str,
    ticket: Ticket | None,
) -> str:
    """Build a Markdown comment explaining the auto-close."""
    if ticket is not None:
        reason = (
            f"tracking ticket `{ticket.id}` reached state "
            f"`{ticket.state.value}` and this PR was never merged."
        )
    else:
        reason = "the PR has an empty diff (no file changes)."
    return (
        f"This PR was automatically closed by the mill's orphaned-PR "
        f"cleanup pass.\n\n"
        f"Reason: {reason}\n\n"
        f"If this was closed in error, reopen the PR or file a new ticket."
    )


def _file_orphan_ticket(
    service: TicketService,
    settings: Settings,
    repo_config: RepoConfig,
    branch: str,
    ticket: Ticket | None,
) -> None:
    """File a tracking ticket for an orphaned PR.

    Uses a deterministic title so the mill's BoardManager deduplicates
    against existing open tickets with the same title.
    """
    title = f"Track orphaned PR: {repo_config.repo_id}/{branch}"
    body = (
        f"An open PR on branch `{branch}` has no active tracking ticket.\n\n"
        f"- Repo: `{repo_config.repo_id}`\n"
        f"- Branch: `{branch}`\n"
        f"- Prior ticket state: "
        f"{ticket.state.value if ticket else 'NOT_FOUND'}\n\n"
        f"Please review and either close the PR or continue the work."
    )
    service.create(
        title=title,
        description=body,
        source=SourceKind.ORPHANED_PR_CHECK,
    )

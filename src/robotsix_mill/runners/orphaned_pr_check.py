"""Orphaned-PR check — deterministic per-repo periodic pass.

Lists open PRs for a managed repo, classifies mill-authored ones as
orphaned when no active ticket drives them, and either auto-closes the
PR (with a comment) or files a tracking ticket so the work is picked
back up.  No LLM agent — pure Python.

Core algorithm: ``classify_orphaned_prs`` consumes the forge's
``list_open_pr_branches`` and the ``TicketService``, reuses the
existing PR↔ticket linkage (branch-naming convention), and produces
a list of :class:`ClassifiedOrphanPr` entries.  Each entry carries an
:class:`OrphanClassification` that drives the downstream action
(close vs file-ticket).
"""

from __future__ import annotations

import enum
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

if TYPE_CHECKING:
    from ..forge.base import Forge

log = logging.getLogger(__name__)


class OrphanClassification(enum.Enum):
    """Granular reason *why* a mill PR is orphaned.

    Maps to downstream actions: close (C) or file-ticket (F).
    """

    # -- close-eligible (clearly obsolete) --------------------------------
    SUPERSEDED = "superseded"
    """Empty diff — the PR carries no effective file changes."""

    TICKET_DONE_UNMERGED = "ticket_done_unmerged"
    """Ticket is DONE but the PR was never merged.  Non-empty diff, no
    merge conflicts detected."""

    TICKET_CLOSED_UNMERGED = "ticket_closed_unmerged"
    """Ticket is CLOSED but the PR was never merged.  Non-empty diff, no
    merge conflicts detected."""

    TICKET_DONE_CONFLICTING = "ticket_done_conflicting"
    """Ticket is DONE, PR is open but has merge conflicts — abandoned."""

    TICKET_CLOSED_CONFLICTING = "ticket_closed_conflicting"
    """Ticket is CLOSED, PR is open but has merge conflicts — abandoned."""

    NO_TICKET_EMPTY_DIFF = "no_ticket_empty_diff"
    """No mill ticket drives this branch and the diff is empty."""

    # -- file-ticket-eligible (worth picking up) --------------------------
    TICKET_ERRORED = "ticket_errored"
    """Ticket is in ERRORED state.  PR has non-empty diff with no
    conflicts detected — worth filing a tracking ticket."""

    TICKET_ERRORED_EMPTY_DIFF = "ticket_errored_empty_diff"
    """Ticket is in ERRORED state but the PR has an empty diff.
    Close-eligible."""

    TICKET_ERRORED_CONFLICTING = "ticket_errored_conflicting"
    """Ticket is in ERRORED state and PR has merge conflicts.
    Close-eligible (abandoned)."""

    NO_TICKET = "no_ticket"
    """No mill ticket drives this branch.  PR has non-empty diff with no
    conflicts detected — worth picking up by filing a tracking ticket."""

    NO_TICKET_CONFLICTING = "no_ticket_conflicting"
    """No mill ticket drives this branch and PR has merge conflicts.
    Close-eligible."""


# ------------------------------------------------------------------ helpers


def _pr_has_empty_diff(forge: Forge, branch: str) -> bool:
    """Return True when the PR for *branch* has no effective file changes."""
    files = forge.pr_files(source_branch=branch)
    if not files:
        return True
    return all(f.get("additions", 0) + f.get("deletions", 0) == 0 for f in files)


def _pr_has_conflicts(forge: Forge, branch: str) -> bool:
    """Return True when the PR for *branch* has merge conflicts."""
    pr_info = forge.pr_status(source_branch=branch)
    if pr_info is None:
        return False
    # mergeable: True→mergeable, False→conflicts, None→unknown (treat
    # as mergeable — the forge hasn't computed yet).
    return pr_info.get("mergeable") is False


def _determine_classification(
    ticket: Ticket | None,
    empty_diff: bool,
    has_conflicts: bool,
) -> OrphanClassification:
    """Map (ticket, diff, conflicts) → granular orphan classification."""
    if ticket is None:
        return _classify_no_ticket(empty_diff, has_conflicts)
    return _classify_with_ticket(ticket.state, empty_diff, has_conflicts)


def _classify_no_ticket(empty_diff: bool, has_conflicts: bool) -> OrphanClassification:
    if empty_diff:
        return OrphanClassification.NO_TICKET_EMPTY_DIFF
    if has_conflicts:
        return OrphanClassification.NO_TICKET_CONFLICTING
    return OrphanClassification.NO_TICKET


def _classify_with_ticket(
    state: State, empty_diff: bool, has_conflicts: bool
) -> OrphanClassification:
    if state == State.ERRORED:
        return _classify_errored(empty_diff, has_conflicts)
    if state == State.DONE:
        return _classify_done(empty_diff, has_conflicts)
    if state == State.CLOSED:
        return _classify_closed(empty_diff, has_conflicts)
    raise ValueError(f"Unexpected orphan ticket state: {state}")


def _classify_errored(empty_diff: bool, has_conflicts: bool) -> OrphanClassification:
    if empty_diff:
        return OrphanClassification.TICKET_ERRORED_EMPTY_DIFF
    if has_conflicts:
        return OrphanClassification.TICKET_ERRORED_CONFLICTING
    return OrphanClassification.TICKET_ERRORED


def _classify_done(empty_diff: bool, has_conflicts: bool) -> OrphanClassification:
    if empty_diff:
        return OrphanClassification.SUPERSEDED
    if has_conflicts:
        return OrphanClassification.TICKET_DONE_CONFLICTING
    return OrphanClassification.TICKET_DONE_UNMERGED


def _classify_closed(empty_diff: bool, has_conflicts: bool) -> OrphanClassification:
    if empty_diff:
        return OrphanClassification.SUPERSEDED
    if has_conflicts:
        return OrphanClassification.TICKET_CLOSED_CONFLICTING
    return OrphanClassification.TICKET_CLOSED_UNMERGED


# ------------------------------------------------------------------ data


@dataclass
class ClassifiedOrphanPr:
    """One orphaned PR with its classification reason."""

    branch: str
    """Full branch name (includes branch-prefix)."""

    ticket_id: str | None
    """Recovered ticket id (prefix stripped), or ``None`` when the
    branch name doesn't parse to a ticket id."""

    classification: OrphanClassification
    """Why this PR was classified as orphaned."""

    ticket_state: str | None = None
    """``ticket.state.value`` when a ticket was found, else ``None``."""


# Ticket states that cause a PR to be classified as orphaned.
_ORPHAN_STATES: frozenset[State] = frozenset({State.DONE, State.CLOSED, State.ERRORED})

# Classifications that mandate auto-close (vs file-ticket).
_CLOSE_CLASSIFICATIONS: frozenset[OrphanClassification] = frozenset(
    {
        OrphanClassification.SUPERSEDED,
        OrphanClassification.TICKET_DONE_UNMERGED,
        OrphanClassification.TICKET_CLOSED_UNMERGED,
        OrphanClassification.TICKET_DONE_CONFLICTING,
        OrphanClassification.TICKET_CLOSED_CONFLICTING,
        OrphanClassification.NO_TICKET_EMPTY_DIFF,
        OrphanClassification.TICKET_ERRORED_EMPTY_DIFF,
        OrphanClassification.TICKET_ERRORED_CONFLICTING,
        OrphanClassification.NO_TICKET_CONFLICTING,
    }
)


@dataclass
class OrphanedPrCheckResult:
    """Result of one orphaned-PR check pass for a single repo."""

    repo_id: str
    total_scanned: int = 0
    closed: int = 0
    filed: int = 0
    skipped: int = 0
    human_pr_skipped: int = 0  # PRs skipped because author is not the bot
    dry_run: bool = True
    actions: list[str] = field(default_factory=list)
    classifications: list[ClassifiedOrphanPr] = field(default_factory=list)
    """Enumerated list of every classified orphaned PR from this pass.
    Filled in before any action is taken — safe for dry-run inspection
    and downstream consumers."""


# ------------------------------------------------------------------ core


def classify_orphaned_prs(
    mill_branches: list[str],
    settings: Settings,
    service: TicketService,
    forge: Forge,
) -> list[ClassifiedOrphanPr]:
    """Core algorithm: classify each mill branch as orphaned or not.

    Reuses the existing PR↔ticket linkage (branch naming convention).
    Branches with an active, in-progress ticket are excluded; the rest
    receive a granular :class:`OrphanClassification` that drives the
    downstream action (close vs file-ticket).

    Age-guard filtering is NOT done here — callers should pre-filter
    young tickets before calling this function.  Branches whose PR is
    already closed on the forge side are also excluded.

    Returns:
        A list of :class:`ClassifiedOrphanPr` — one per orphaned PR.
    """
    result: list[ClassifiedOrphanPr] = []

    for branch in mill_branches:
        ticket_id = branch.removeprefix(settings.branch_prefix)
        ticket: Ticket | None = service.get(ticket_id)

        # --- Active ticket drives this PR → not orphaned ---
        if ticket is not None and ticket.state not in _ORPHAN_STATES:
            continue

        # --- PR already closed/merged on forge → skip ---
        pr_info = forge.pr_status(source_branch=branch)
        if pr_info is None or pr_info.get("state") != "open":
            continue

        empty_diff = _pr_has_empty_diff(forge, branch)
        has_conflicts = _pr_has_conflicts(forge, branch)
        classification = _determine_classification(ticket, empty_diff, has_conflicts)

        result.append(
            ClassifiedOrphanPr(
                branch=branch,
                ticket_id=ticket_id,
                classification=classification,
                ticket_state=ticket.state.value if ticket else None,
            )
        )

    return result


# ------------------------------------------------------------------ runner


def run_orphaned_pr_check_pass(
    repo_config: RepoConfig | None = None,
) -> OrphanedPrCheckResult:
    """Run one orphaned-PR check pass for *repo_config*.

    Args:
        repo_config: Managed repo to scan.  Required.

    Returns:
        ``OrphanedPrCheckResult`` with counts and an audit log of every
        action (or would-be action under dry-run).
    """
    if repo_config is None:
        raise ValueError("orphaned_pr_check requires a repo_config")

    settings = Settings()
    service = TicketService(settings, board_id=repo_config.board_id)
    forge = get_forge(settings, repo_config=repo_config)
    result = OrphanedPrCheckResult(
        repo_id=repo_config.repo_id,
        dry_run=settings.orphaned_pr_dry_run,
    )

    # Resolve allowed bot logins for author guard
    if settings.orphaned_pr_bot_logins:
        allowed_logins: set[str] = set(settings.orphaned_pr_bot_logins)
    else:
        resolved = forge.get_authenticated_user_login()
        if resolved:
            allowed_logins = {resolved}
        else:
            allowed_logins = set()
            log.warning(
                "orphaned-pr-check: could not resolve forge bot login; "
                "author guard is inactive for this pass (branch-prefix filter still active)"
            )

    open_prs: list[dict] = forge.list_open_prs()
    mill_prs = [
        pr for pr in open_prs if pr["branch"].startswith(settings.branch_prefix)
    ]
    result.total_scanned = len(mill_prs)

    open_orphan_titles: frozenset[str] = (
        _load_open_orphan_titles(service)
        if not settings.orphaned_pr_dry_run
        else frozenset()
    )

    _classify_branches(
        sorted(mill_prs, key=lambda p: p["branch"]),
        allowed_logins,
        settings,
        service,
        forge,
        repo_config,
        result,
        open_orphan_titles,
    )
    return result


def _classify_branches(
    mill_prs: list[dict],
    allowed_logins: set[str],
    settings: Settings,
    service: TicketService,
    forge: Forge,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
    open_orphan_titles: frozenset[str] = frozenset(),
) -> None:
    """Iterate sorted mill PRs, classify each, and update *result*.

    Phase 0a: author guard — skip PRs whose author_login is not a known bot.
    Phase 0b: age-filter branches, then delegate to
    :func:`classify_orphaned_prs` for pure classification.
    Phase 2: act on each classification (close or file-ticket) up to
    per-type and combined caps.
    """

    # ---- phase 0a: author guard --------------------------------------
    prs_after_author: list[dict] = []
    for pr in mill_prs:
        author = pr["author_login"]
        if allowed_logins and author not in allowed_logins:
            log.debug(
                "orphaned-pr-check: %s/%s authored by %s (not bot) — skipping",
                repo_config.repo_id,
                pr["branch"],
                author[:20],  # truncate to avoid leaking full handles in DEBUG logs
            )
            result.human_pr_skipped += 1
            continue
        prs_after_author.append(pr)

    # ---- phase 0b: age-filter branches -------------------------------
    age_filtered: list[str] = []
    for pr in prs_after_author:
        branch = pr["branch"]
        ticket_id = branch.removeprefix(settings.branch_prefix)
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
        age_filtered.append(branch)

    # ---- phase 1: classify all eligible branches --------------------
    classifications = classify_orphaned_prs(
        age_filtered, settings=settings, service=service, forge=forge
    )
    result.classifications = classifications

    # ---- phase 2: act on classifications (capped) -------------------
    _apply_classifications(
        classifications,
        settings,
        service,
        forge,
        repo_config,
        result,
    )


def _apply_classifications(
    classifications: list[ClassifiedOrphanPr],
    settings: Settings,
    service: TicketService,
    forge: Forge,
    repo_config: RepoConfig,
    result: OrphanedPrCheckResult,
) -> None:
    """Act on each classification up to per-type and combined action caps."""
    max_actions = settings.orphaned_pr_max_actions_per_pass
    max_closes = settings.orphaned_pr_max_closes_per_pass
    max_files = settings.orphaned_pr_max_files_per_pass
    closes_taken = 0
    files_taken = 0
    total_taken = 0

    for cpr in classifications:
        if total_taken >= max_actions or (
            closes_taken >= max_closes and files_taken >= max_files
        ):
            remaining = len(classifications) - total_taken
            cap_msg = (
                f"orphaned-pr-check: action cap reached "
                f"(closes={closes_taken}/{max_closes}, files={files_taken}/{max_files}, "
                f"total={total_taken}/{max_actions}) — "
                f"{remaining} classification(s) remain unprocessed"
            )
            log.info(cap_msg)
            result.actions.append(cap_msg)
            break

        should_close = cpr.classification in _CLOSE_CLASSIFICATIONS
        # Per-type action caps (from this branch)
        if should_close:
            if closes_taken >= max_closes:
                log.debug(
                    "orphaned-pr-check: close cap reached, skipping %s", cpr.branch
                )
                result.skipped += 1
                continue
        else:
            if files_taken >= max_files:
                log.debug(
                    "orphaned-pr-check: file cap reached, skipping %s", cpr.branch
                )
                result.skipped += 1
                continue

        # Dedup check (from origin/main)
        is_dedup = (
            not should_close
            and _orphan_ticket_title(repo_config, cpr.branch) in open_orphan_titles
        )

        action = (
            "CLOSE" if should_close else ("DEDUP_SKIP" if is_dedup else "FILE_TICKET")
        )
        state_label = cpr.ticket_state or "NOT_FOUND"
        log_line = (
            f"repo={repo_config.repo_id} branch={cpr.branch} "
            f"ticket_state={state_label} action={action} "
            f"classification={cpr.classification.value} "
            f"dry_run={settings.orphaned_pr_dry_run}"
        )
        log.info("orphaned-pr-check: %s", log_line)
        result.actions.append(log_line)

        if settings.orphaned_pr_dry_run or is_dedup:
            result.skipped += 1
            continue

        if should_close:
            comment = _build_close_comment(cpr, repo_config.repo_id)
            forge.post_pr_comment(source_branch=cpr.branch, body=comment)
            forge.close_pr(source_branch=cpr.branch)
            result.closed += 1
            closes_taken += 1
        else:
            _file_orphan_ticket(service, settings, repo_config, cpr)
            result.filed += 1
            files_taken += 1
        total_taken += 1


# ------------------------------------------------------------------ actions


def _build_close_comment(cpr: ClassifiedOrphanPr, repo_id: str) -> str:
    """Build a Markdown comment explaining the auto-close."""
    classification_reasons: dict[OrphanClassification, str] = {
        OrphanClassification.SUPERSEDED: (
            "the PR has an empty diff (no effective file changes)."
        ),
        OrphanClassification.TICKET_DONE_UNMERGED: (
            f"tracking ticket `{cpr.ticket_id}` reached state `done` "
            f"and this PR was never merged.  No merge conflicts detected."
        ),
        OrphanClassification.TICKET_CLOSED_UNMERGED: (
            f"tracking ticket `{cpr.ticket_id}` reached state `closed` "
            f"and this PR was never merged.  No merge conflicts detected."
        ),
        OrphanClassification.TICKET_DONE_CONFLICTING: (
            f"tracking ticket `{cpr.ticket_id}` reached state `done`, "
            f"the PR has merge conflicts, and appears abandoned."
        ),
        OrphanClassification.TICKET_CLOSED_CONFLICTING: (
            f"tracking ticket `{cpr.ticket_id}` reached state `closed`, "
            f"the PR has merge conflicts, and appears abandoned."
        ),
        OrphanClassification.NO_TICKET_EMPTY_DIFF: (
            "the PR has an empty diff (no file changes) and no "
            "tracking ticket was found."
        ),
        OrphanClassification.TICKET_ERRORED_EMPTY_DIFF: (
            f"tracking ticket `{cpr.ticket_id}` is in `errored` state "
            f"and the PR has an empty diff."
        ),
        OrphanClassification.TICKET_ERRORED_CONFLICTING: (
            f"tracking ticket `{cpr.ticket_id}` is in `errored` state "
            f"and the PR has merge conflicts — abandoned."
        ),
        OrphanClassification.NO_TICKET_CONFLICTING: (
            "the PR has merge conflicts and no tracking ticket was found — abandoned."
        ),
    }
    reason = classification_reasons.get(
        cpr.classification,
        f"classification: {cpr.classification.value}.",
    )
    return (
        f"This PR was automatically closed by the mill's orphaned-PR "
        f"cleanup pass.\n\n"
        f"Reason: {reason}\n\n"
        f"If this was closed in error, reopen the PR or file a new ticket."
    )


def _orphan_ticket_title(repo_config: RepoConfig, branch: str) -> str:
    """Return the deterministic tracking-ticket title for an orphaned PR branch."""
    return f"Track orphaned PR: {repo_config.repo_id}/{branch}"


def _load_open_orphan_titles(service: TicketService) -> frozenset[str]:
    """Return titles of all non-terminal orphaned-PR tracking tickets."""
    tickets: list[Ticket] = service.recent_proposals_for(
        source=SourceKind.ORPHANED_PR_CHECK, limit=500
    )
    return frozenset(t.title for t in tickets if t.state not in _ORPHAN_STATES)


def _file_orphan_ticket(
    service: TicketService,
    settings: Settings,
    repo_config: RepoConfig,
    cpr: ClassifiedOrphanPr,
) -> None:
    """File a tracking ticket for an orphaned PR.

    Uses a deterministic title so the mill's BoardManager deduplicates
    against existing open tickets with the same title.
    """
    title = _orphan_ticket_title(repo_config, cpr.branch)
    body = (
        f"An open PR on branch `{cpr.branch}` has no active tracking ticket.\n\n"
        f"- Repo: `{repo_config.repo_id}`\n"
        f"- Branch: `{cpr.branch}`\n"
        f"- Classification: `{cpr.classification.value}`\n"
        f"- Prior ticket state: "
        f"{cpr.ticket_state or 'NOT_FOUND'}\n\n"
        f"Please review and either close the PR or continue the work."
    )
    service.create(
        title=title,
        description=body,
        source=SourceKind.ORPHANED_PR_CHECK,
    )

"""Upstream-CI recovery runner — resumes tickets parked on a red target branch.

``stages/ci_fix_helpers._check_upstream_ci_breakage`` parks a ticket
``BLOCKED`` when the checks failing on its PR are the same ones failing on
the target branch: the PR is not the cause, and the ci-fix agent must not
burn its budget on it.  Until this pass existed that park was terminal —
nothing re-checked the target branch, so every parked ticket needed a
human ``resume-blocked`` even though the condition it waited on had
cleared on its own.  On 2026-08-26 robotsix-chat's ``main`` was red for
23 hours (Trivy); seven tickets parked, and all seven were still parked
nine hours after ``main`` went green.

A deterministic, no-LLM pass: forge status reads + state transitions.  No
AI agent, no pass_runner, no Langfuse tracing.

Per board:

1. Find ``BLOCKED`` tickets whose most recent ``BLOCKED`` history note
   starts with :data:`~robotsix_mill.stages.ci_fix_helpers.UPSTREAM_CI_BLOCK_MARKER`.
2. Resolve the target branch's current head from its latest workflow runs
   and ask the forge for that commit's aggregate CI conclusion — the SAME
   call the guard made when it parked the ticket, so the two agree.
3. When the conclusion is no longer ``"failure"`` (green, or nothing
   pending-red), bring the PR branch up to date with the target
   (``forge.update_branch`` — server-side merge of the base into the PR,
   which re-runs the PR's CI against the now-green base) and
   ``resume_blocked`` the ticket back into the stage it was parked from.
   A pending/unknown conclusion leaves the ticket parked for the next pass.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...core.models import Ticket, TicketEvent
from ...core.service import TicketService
from ...core.states import State
from ...stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER
from .timeout_escalation_runner import _boards_to_scan

if TYPE_CHECKING:
    from ...config import RepoConfig, Settings

log = logging.getLogger("robotsix_mill.upstream_ci_recovery")


def _last_blocked_note(service: TicketService, ticket_id: str) -> str:
    """Return the note of the most recent ``BLOCKED`` event for *ticket_id*.

    Block reasons live in the event log, not on the ticket row.  A ticket
    may carry an older upstream park followed by an unrelated block, so
    only the LATEST ``BLOCKED`` event decides whether the ticket is ours.
    """
    events: list[TicketEvent] = service.history(ticket_id, order="desc")
    for ev in events:
        if ev.state is State.BLOCKED:
            return ev.note or ""
    return ""


def _is_upstream_parked(note: str) -> bool:
    """True when *note* is an upstream-CI park (single- or multi-repo form).

    The merge path prefixes its note with ``[<repo_id>] `` for multi-repo
    tickets, so match the marker anywhere in the leading segment rather
    than insisting on position zero.
    """
    head = note[:160]
    return UPSTREAM_CI_BLOCK_MARKER in head


def _target_head_sha(forge: Any, target: str) -> str | None:
    """Head SHA of *target* as seen by its most recent workflow run.

    The forge API lists runs newest-first; the first run on the branch is
    the latest commit that has CI at all, which is exactly the commit the
    guard would compare against.  ``None`` when the branch has no runs.
    """
    runs = forge.list_workflow_runs(branch=target)
    for run in runs:
        sha = run.get("head_sha")
        if sha:
            return str(sha)
    return None


def _repo_for_board(settings: Settings, board_id: str) -> RepoConfig | None:
    from ...config import get_repos_config

    for rc in get_repos_config().repos.values():
        if rc.board_id == board_id:
            return rc
    return None


def _target_is_green(forge: Any, target: str, ticket_id: str) -> tuple[bool, str]:
    """Return ``(recovered, target_sha)`` for the target branch.

    Recovered means the target head has CI status and its conclusion is
    not ``"failure"``.  ``None``/``"pending"`` keep the ticket parked: a
    half-finished run on the target proves nothing yet, and resuming into
    a still-red base would just re-park the ticket one poll later.
    """
    sha = _target_head_sha(forge, target)
    if not sha:
        log.info(
            "%s: target branch %r has no workflow runs — leaving parked",
            ticket_id,
            target,
        )
        return False, ""
    status = forge.commit_ci_conclusion(sha=sha)
    conclusion = (status or {}).get("conclusion")
    if conclusion is None or conclusion == "pending":
        log.info(
            "%s: target branch %r @ %s CI is %s — leaving parked",
            ticket_id,
            target,
            sha[:8],
            conclusion or "unavailable",
        )
        return False, sha
    return conclusion != "failure", sha


def _refresh_pr_branch(forge: Any, ticket: Ticket) -> str:
    """Best-effort: merge the (now green) base into the PR branch.

    The PR's recorded CI is the red run against the old base; without a
    new push the stage would re-read that stale failure.  ``update_branch``
    is the forge-native way to re-run PR CI against the current base tip
    and must never raise per its contract; anything unexpected is logged
    and the resume proceeds regardless — ci_fix has its own re-run path.
    """
    if not ticket.branch:
        return "no branch recorded"
    try:
        result = forge.update_branch(source_branch=ticket.branch)
    except Exception as exc:  # pragma: no cover - contract says never raises
        log.warning("%s: update_branch raised: %s", ticket.id, exc)
        return f"update_branch raised {type(exc).__name__}"
    if result.get("updated"):
        return "PR branch updated from target"
    return f"PR branch not updated ({result.get('reason', 'unknown')})"


def run_upstream_ci_recovery(settings: Settings) -> dict[str, Any]:
    """Execute one upstream-CI recovery pass across every known board.

    Returns ``{"resumed": n, "still_parked": n, "skipped": n}`` where
    *skipped* counts parked tickets whose board has no repo config or
    whose forge lookup failed (they stay parked and are retried next pass).
    """
    from ...config import target_branch_for
    from ...forge import get_forge

    resumed = 0
    still_parked = 0
    skipped = 0

    for board_id in _boards_to_scan(settings):
        service = TicketService(settings, board_id=board_id)
        try:
            blocked = service.list(state=State.BLOCKED)
        except Exception:
            log.exception("upstream_ci_recovery: board=%r list failed", board_id)
            continue

        parked = [
            t for t in blocked if _is_upstream_parked(_last_blocked_note(service, t.id))
        ]
        if not parked:
            continue

        rc = _repo_for_board(settings, board_id)
        if rc is None:
            log.warning(
                "upstream_ci_recovery: board=%r has %d upstream-parked ticket(s) "
                "but no registered repo — skipping",
                board_id,
                len(parked),
            )
            skipped += len(parked)
            continue

        target = target_branch_for(settings, rc)
        try:
            forge = get_forge(settings, repo_config=rc)
        except Exception:
            log.exception("upstream_ci_recovery: board=%r get_forge failed", board_id)
            skipped += len(parked)
            continue

        # One target-branch lookup per board, not per ticket.
        try:
            green, sha = _target_is_green(forge, target, parked[0].id)
        except Exception:
            log.exception(
                "upstream_ci_recovery: board=%r target %r status lookup failed",
                board_id,
                target,
            )
            skipped += len(parked)
            continue
        if not green:
            still_parked += len(parked)
            continue

        for ticket in parked:
            refresh = _refresh_pr_branch(forge, ticket)
            note = (
                f"target branch `{target}` CI is green again ({sha[:8]}) — "
                f"auto-resumed by upstream_ci_recovery; {refresh}"
            )
            try:
                service.resume_blocked(ticket.id, note=note)
                resumed += 1
                log.info("upstream_ci_recovery: %s resumed — %s", ticket.id, note)
            except Exception:
                log.exception("upstream_ci_recovery: %s resume failed", ticket.id)
                skipped += 1

    log.info(
        "upstream_ci_recovery: pass complete — resumed=%d still_parked=%d skipped=%d",
        resumed,
        still_parked,
        skipped,
    )
    return {"resumed": resumed, "still_parked": still_parked, "skipped": skipped}

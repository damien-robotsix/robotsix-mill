"""Priority-stall diagnostic check.

A :class:`DiagnosticCheck` that detects priority tickets stuck at
``IMPLEMENT_COMPLETE`` and investigates the merge deferral reason
(branch status, CI state, mergeability). When a priority ticket
has been at ``IMPLEMENT_COMPLETE`` for more than one monitor cycle
without advancing, this check emits a diagnostic event carrying the
root-cause analysis. It files **no tickets** — investigation findings
belong in a chat subsession, not in a ticket.

Registered via :func:`register_check` so the daily diagnostic agent
picks it up automatically — no runner edits required.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ...config import get_repos_config
from ...core.service import TicketService
from ...core.states import State
from ...forge import get_forge
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_events import emit_diagnostic_event

log = logging.getLogger(__name__)


def _check_pr_status(
    forge: Any, branch: str, result: dict[str, Any]
) -> dict[str, Any] | None:
    """Check PR status and populate result dict. Returns pr dict or None for early exit."""
    try:
        pr: dict[str, Any] | None = forge.pr_status(source_branch=branch)
    except Exception as e:
        result["summary"] = f"PR status check failed: {e}"
        return None

    if pr is None:
        result["summary"] = "no PR found for branch"
        return None

    result["pr_url"] = pr.get("url")
    result["mergeable"] = pr.get("mergeable")
    result["mergeable_state"] = pr.get("mergeable_state")

    if pr.get("merged"):
        result["summary"] = "PR already merged (ticket should advance)"
        return None

    if pr.get("state") == "closed":
        result["summary"] = "PR closed without merge"
        return None

    return pr


def _check_mergeability(pr: dict[str, Any], result: dict[str, Any]) -> bool:
    """Check mergeability. Returns True if should continue investigation."""
    mergeable = pr.get("mergeable")
    if mergeable is False:
        result["branch_status"] = "conflicting"
        result["summary"] = "PR has merge conflicts with target branch"
        return False
    return True


def _check_ci_status(
    forge: Any, branch: str, pr: dict[str, Any], result: dict[str, Any]
) -> None:
    """Check CI status and populate result dict."""
    try:
        ci_status = forge.check_status(source_branch=branch)
    except Exception as e:
        result["summary"] = f"CI status check failed: {e}"
        return

    if ci_status is None:
        result["ci_state"] = "unknown"
        result["summary"] = "no CI data available"
        return

    conclusion = ci_status.get("conclusion")
    pending = ci_status.get("pending", []) or []
    failing = ci_status.get("failing", []) or []

    result["pending_checks"] = pending
    result["failing_checks"] = [f.get("name", "unknown") for f in failing]

    if conclusion == "failure":
        result["ci_state"] = "failing"
        failing_names = ", ".join(result["failing_checks"][:5])
        result["summary"] = f"CI failing: {failing_names}"
        return

    if conclusion == "pending" or pending:
        result["ci_state"] = "pending"
        result["summary"] = f"CI pending: {len(pending)} check(s) in flight"
        return

    if conclusion == "success":
        result["ci_state"] = "green"
        mergeable_state = pr.get("mergeable_state")
        if mergeable_state == "behind":
            result["branch_status"] = "behind"
            result["summary"] = "CI green but branch is behind target (needs rebase)"
            return
        if mergeable_state in ("blocked", "unknown"):
            result["summary"] = (
                f"CI green but mergeable_state={mergeable_state!r} — "
                "check branch protection rules"
            )
            return
        result["summary"] = "CI green and mergeable — should be promotable"
        return

    result["summary"] = f"unexpected CI conclusion: {conclusion!r}"


def _investigate_merge_deferral(
    branch: str,
    settings: Any,
    repo_config: Any,
) -> dict[str, Any]:
    """Investigate why a ticket at IMPLEMENT_COMPLETE can't advance.

    Returns a dict with:
    - ``branch_status``: "ok" | "behind" | "conflicting" | "unknown"
    - ``ci_state``: "green" | "failing" | "pending" | "unknown"
    - ``mergeable``: True | False | None
    - ``mergeable_state``: str | None
    - ``failing_checks``: list of failing check names
    - ``pending_checks``: list of pending check names
    - ``pr_url``: str | None
    - ``summary``: human-readable explanation
    """
    result: dict[str, Any] = {
        "branch_status": "unknown",
        "ci_state": "unknown",
        "mergeable": None,
        "mergeable_state": None,
        "failing_checks": [],
        "pending_checks": [],
        "pr_url": None,
        "summary": "investigation incomplete",
    }

    try:
        forge = get_forge(settings, repo_config=repo_config)
    except Exception as e:
        result["summary"] = f"could not resolve forge: {e}"
        return result

    pr = _check_pr_status(forge, branch, result)
    if pr is None:
        return result

    if not _check_mergeability(pr, result):
        return result

    _check_ci_status(forge, branch, pr, result)
    return result


class PriorityStallCheck:
    """Detect priority tickets stuck at IMPLEMENT_COMPLETE and investigate why."""

    name = "priority_stall"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the priority-stall check and file diagnostic tickets."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("priority_stall check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="priority_stall check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        service = TicketService(settings, board_id=board_id)

        # Find priority tickets at IMPLEMENT_COMPLETE
        tickets = service.list(state=State.IMPLEMENT_COMPLETE)
        priority_tickets = [t for t in tickets if t.priority]

        if not priority_tickets:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="no priority tickets at IMPLEMENT_COMPLETE",
            )

        # Filter to tickets that have been stuck for at least one monitor cycle
        # Use a conservative threshold: 2x the typical poll interval (default ~10 min)
        # This ensures we don't fire on tickets that just arrived at IMPLEMENT_COMPLETE
        stuck_threshold = timedelta(minutes=20)
        now = datetime.now(UTC)
        stuck_tickets = [
            t for t in priority_tickets if (now - t.updated_at) > stuck_threshold
        ]

        if not stuck_tickets:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=(
                    f"{len(priority_tickets)} priority ticket(s) at "
                    "IMPLEMENT_COMPLETE, none stuck yet"
                ),
            )

        # Resolve repo config for this board
        repo_config = None
        try:
            repos = get_repos_config()
            for rc in repos.repos.values():
                if rc.board_id == board_id:
                    repo_config = rc
                    break
        except Exception:
            log.warning(
                "priority_stall: could not resolve repo config for %s", board_id
            )

        emitted = 0
        investigated: list[dict[str, Any]] = []
        ci_in_flight = 0

        for ticket in stuck_tickets:
            branch = ticket.branch or f"{settings.branch_prefix}{ticket.id}"
            investigation = _investigate_merge_deferral(branch, settings, repo_config)
            investigation["ticket_id"] = ticket.id
            investigation["ticket_title"] = ticket.title
            investigated.append(investigation)

            log.info(
                "priority_stall: ticket %s stuck at IMPLEMENT_COMPLETE — %s",
                ticket.id,
                investigation["summary"],
            )

            # CI still in flight is not a stall — the ticket is waiting on
            # exactly the thing it should be waiting on.  Emitting here would
            # produce an event per priority PR whose checks outlast the
            # 20-minute threshold, which is most of them.
            if investigation["ci_state"] == "pending":
                ci_in_flight += 1
                log.info(
                    "priority_stall: ticket %s has CI in flight — not filing",
                    ticket.id,
                )
                continue

            # Surface the finding as a diagnostic event only — never a
            # ticket. Investigation belongs in a chat subsession, not in a
            # ticket (operator policy). The event store dedups on
            # (category, ticket_id, normalized_key), so a repeated stall on
            # the same ticket does not flood the store.
            try:
                if emit_diagnostic_event(
                    settings,
                    board_id,
                    category="PRIORITY_STALL",
                    ticket_id=ticket.id,
                    reason=investigation["summary"],
                    normalized_key=ticket.id,
                ):
                    emitted += 1
            except Exception:
                log.exception(
                    "priority_stall: failed to emit event for %s",
                    ticket.id,
                )

        summary = (
            f"{len(priority_tickets)} priority ticket(s) at IMPLEMENT_COMPLETE, "
            f"{len(stuck_tickets)} stuck ({ci_in_flight} waiting on in-flight CI); "
            f"{emitted} diagnostic event(s) emitted (no tickets filed)"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
        )


register_check(PriorityStallCheck())

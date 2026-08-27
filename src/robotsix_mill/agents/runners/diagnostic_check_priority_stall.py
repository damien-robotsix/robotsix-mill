"""Priority-stall diagnostic check.

A :class:`DiagnosticCheck` that detects priority tickets stuck at
``IMPLEMENT_COMPLETE`` and investigates the merge deferral reason
(branch status, CI state, mergeability). When a priority ticket
has been at ``IMPLEMENT_COMPLETE`` for more than one monitor cycle
without advancing, this check files a diagnostic ticket with the
root-cause analysis.

Registered via :func:`register_check` so the daily diagnostic agent
picks it up automatically — no runner edits required.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ...config import get_repos_config
from ...core.models import SourceKind, TicketKind
from ...core.service import TicketService
from ...core.states import DONE_OR_CLOSED, State
from ...forge import get_forge
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)

log = logging.getLogger(__name__)

_DIAGNOSTIC_TITLE_PREFIX = "[diagnostic] priority stall:"


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

        drafts_created: list[dict[str, Any]] = []
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
            # exactly the thing it should be waiting on.  Filing here would
            # produce a ticket per priority PR whose checks outlast the
            # 20-minute threshold, which is most of them.
            if investigation["ci_state"] == "pending":
                ci_in_flight += 1
                log.info(
                    "priority_stall: ticket %s has CI in flight — not filing",
                    ticket.id,
                )
                continue

            # File a diagnostic ticket with the investigation results
            title = (
                f"{_DIAGNOSTIC_TITLE_PREFIX} {ticket.id} — "
                f"{investigation['summary'][:60]}"
            )
            if self._is_duplicate(ticket.id, service):
                log.info(
                    "priority_stall: an open diagnostic for %s already exists "
                    "— not filing %r",
                    ticket.id,
                    title,
                )
                continue

            body = self._build_body(ticket, investigation, board_id)
            try:
                diag_ticket = service.create(
                    title,
                    body,
                    source=SourceKind.AGENT,
                    kind=TicketKind.TASK,
                )
                log.info(
                    "priority_stall: created ticket %s — %r",
                    diag_ticket.id,
                    title,
                )
                drafts_created.append({"id": diag_ticket.id, "title": title})
            except Exception:
                log.exception(
                    "priority_stall: failed to file ticket for %s",
                    ticket.id,
                )

        summary = (
            f"{len(priority_tickets)} priority ticket(s) at IMPLEMENT_COMPLETE, "
            f"{len(stuck_tickets)} stuck ({ci_in_flight} waiting on in-flight CI); "
            f"{len(drafts_created)} diagnostic draft(s) filed"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
            drafts_created=drafts_created,
        )

    @staticmethod
    def _is_duplicate(stalled_ticket_id: str, service: TicketService) -> bool:
        """Return True if an open diagnostic already covers *stalled_ticket_id*.

        Scoped to the stalled ticket rather than to the full title on
        purpose: the title carries a slice of the investigation summary,
        which changes as the PR moves (``CI pending`` → ``branch is
        behind`` → ``mergeable_state='blocked'``).  Matching on the whole
        title would therefore file a fresh ticket on every state change
        instead of recognising the stall it already reported.
        """
        prefix = f"{_DIAGNOSTIC_TITLE_PREFIX} {stalled_ticket_id}".strip().casefold()
        for t in service.list():
            if (
                t.title.strip().casefold().startswith(prefix)
                and t.state not in DONE_OR_CLOSED
            ):
                return True
        return False

    @staticmethod
    def _build_body(
        ticket: Any,
        investigation: dict[str, Any],
        board_id: str,
    ) -> str:
        """Build the diagnostic ticket body."""
        lines = [
            "Auto-filed by the daily diagnostic agent (priority_stall check).",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Stuck ticket:** `{ticket.id}` — {ticket.title}",
            f"- **State:** {ticket.state.value}",
            f"- **Priority:** {ticket.priority}",
            f"- **Branch:** `{ticket.branch or '(default)'}`",
            f"- **Last updated:** {ticket.updated_at.isoformat()}",
            "",
            "### Investigation results",
            "",
            f"- **Branch status:** {investigation['branch_status']}",
            f"- **CI state:** {investigation['ci_state']}",
            f"- **Mergeable:** {investigation['mergeable']}",
            f"- **Mergeable state:** {investigation['mergeable_state']}",
        ]

        if investigation.get("pr_url"):
            lines.append(f"- **PR URL:** {investigation['pr_url']}")

        if investigation.get("failing_checks"):
            lines.append(
                f"- **Failing checks:** {', '.join(investigation['failing_checks'])}"
            )

        if investigation.get("pending_checks"):
            lines.append(
                f"- **Pending checks:** {', '.join(investigation['pending_checks'])}"
            )

        lines.extend(
            [
                "",
                "### Summary",
                "",
                investigation["summary"],
                "",
                "### Action",
                "",
                (
                    "This priority ticket is stuck at IMPLEMENT_COMPLETE. "
                    "Investigate the merge deferral reason above and take "
                    "appropriate action:"
                ),
                "",
                "- If **branch is behind**: trigger a rebase",
                "- If **CI is failing**: investigate the failure and fix or escalate",
                "- If **merge conflicts**: resolve conflicts and rebase",
                "- If **branch protection**: check required status contexts",
                "",
                (
                    "Once resolved, the ticket should advance to "
                    "HUMAN_MR_APPROVAL or WAITING_AUTO_MERGE."
                ),
            ]
        )
        return "\n".join(lines) + "\n"


register_check(PriorityStallCheck())

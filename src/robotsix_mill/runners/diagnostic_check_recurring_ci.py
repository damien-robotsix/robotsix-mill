"""Recurring CI failure diagnostic check.

A :class:`DiagnosticCheck` that reads the diagnostic event store,
groups ``CI_FAILURE`` events by their normalized key, and auto-files a
fix-proposal draft ticket when a key has been hit by at least
``diagnostic_ci_failure_threshold`` distinct tickets (default 3).

Registered via :func:`register_check` so the daily diagnostic agent
picks it up automatically — no runner edits required.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ..core.models import SourceKind, TicketKind
from ..core.service import TicketService
from ..core.states import DONE_OR_CLOSED
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_events import list_diagnostic_events

log = logging.getLogger(__name__)

_DIAGNOSTIC_TITLE_PREFIX = "[diagnostic] recurring CI failure:"


def _normalized_key_short(key: str) -> str:
    """First 8 chars of a hex key — enough to disambiguate in titles."""
    return key[:8] if len(key) >= 8 else key


class RecurringCIFailureCheck:
    """Detect recurring CI failures and file fix-proposal draft tickets."""

    name = "recurring_ci_failure"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        try:
            return self._run(ctx)
        except Exception:  # noqa: BLE001 — preserve log-and-swallow contract
            log.exception("recurring_ci_failure check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="recurring_ci_failure check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        threshold = settings.diagnostic_ci_failure_threshold
        if threshold == 0:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="recurring CI failure detection disabled (threshold=0)",
            )

        events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        if not events:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="no CI_FAILURE events in store",
            )

        # Group by normalized key; collect distinct ticket ids.
        groups: dict[str, set[str]] = defaultdict(set)
        # Also track the most recent reason per key for the ticket body.
        reasons: dict[str, str] = {}
        for ev in events:
            groups[ev.normalized_key].add(ev.ticket_id)
            reasons[ev.normalized_key] = ev.reason  # last wins; fine for body

        # Find keys that have crossed the threshold.
        triggered = {
            key: tickets for key, tickets in groups.items() if len(tickets) >= threshold
        }
        if not triggered:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=(
                    f"{len(events)} CI_FAILURE event(s) across "
                    f"{len(groups)} key(s); none reached threshold {threshold}"
                ),
            )

        service = TicketService(settings, board_id=board_id)
        drafts_created: list[dict[str, Any]] = []

        for key, tickets in sorted(triggered.items()):
            short = _normalized_key_short(key)
            title = f"{_DIAGNOSTIC_TITLE_PREFIX} key={short} ({len(tickets)} tickets)"
            if self._is_duplicate(title, service):
                log.info("recurring_ci_failure: skipping duplicate ticket %r", title)
                continue
            body = self._build_body(board_id, key, tickets, reasons.get(key, ""))
            try:
                ticket = service.create(
                    title,
                    body,
                    source=SourceKind.AGENT,
                    kind=TicketKind.TASK,
                )
                log.info(
                    "recurring_ci_failure: filed fix-proposal ticket %s — %r",
                    ticket.id,
                    title,
                )
                drafts_created.append({"id": ticket.id, "title": title})
            except Exception:  # noqa: BLE001 — one failed group must not block others
                log.exception(
                    "recurring_ci_failure: failed to file ticket for key %s", key
                )

        summary = (
            f"{len(events)} CI_FAILURE event(s) across {len(groups)} key(s); "
            f"{len(triggered)} key(s) reached threshold {threshold}; "
            f"{len(drafts_created)} fix-proposal draft(s) filed"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
            drafts_created=drafts_created,
        )

    @staticmethod
    def _is_duplicate(title: str, service: TicketService) -> bool:
        """Return True if a non-terminal ticket with *title* already exists."""
        norm = title.strip().casefold()
        for t in service.list():
            if t.title.strip().casefold() == norm and t.state not in DONE_OR_CLOSED:
                return True
        return False

    @staticmethod
    def _build_body(
        board_id: str,
        normalized_key: str,
        tickets: set[str],
        reason: str,
    ) -> str:
        """Build the fix-proposal ticket body."""
        ticket_list = "\n".join(f"- `{tid}`" for tid in sorted(tickets))
        lines = [
            "Auto-filed by the daily diagnostic agent (recurring_ci_failure check).",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Normalized failure key:** `{normalized_key}`",
            f"- **Distinct tickets affected:** {len(tickets)}",
            "",
            "### Affected tickets",
            ticket_list,
            "",
            "### Failure reason (representative)",
            "",
            "```",
            reason[:4000] if reason else "(no reason recorded)",
            "```",
            "",
            "### Action",
            (
                "Review the recurring CI failure pattern above. If a systemic "
                "fix is appropriate (e.g. a pre-commit hook, a CI workflow "
                "change, or a lint rule adjustment), draft a task ticket for "
                "the fix. Once the root cause is resolved, this diagnostic "
                "will stop filing for this key — existing events age out "
                "naturally as new tickets cycle through CI."
            ),
        ]
        return "\n".join(lines) + "\n"


register_check(RecurringCIFailureCheck())

"""Recurring-category diagnostic check.

Scans :class:`DiagnosticEvent` rows for categories whose distinct-ticket
count has crossed the configured threshold, and auto-generates fix-proposal
tickets for each recurring failure mode.

Registered via :func:`~.diagnostic_checks.register_check` so the daily
diagnostic pass picks it up without editing the runner.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.models import SourceKind
from ..core.service import TicketService
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)

log = logging.getLogger(__name__)

DEFAULT_CI_FAILURE_RECURRENCE_THRESHOLD = 3


class RecurringCategoryCheck:
    """Diagnostic check that scans for recurring diagnostic-event categories.

    For each category tracked in ``TRACKED_CATEGORIES``, queries events
    grouped by sub-category and, when a group's distinct-ticket count
    reaches the configured threshold, files a fix-proposal draft ticket.
    """

    name = "recurring_category"

    # Map of category → default threshold (used when Settings doesn't
    # provide a category-specific threshold).
    TRACKED_CATEGORIES: dict[str, int] = {
        "CI_FAILURE": DEFAULT_CI_FAILURE_RECURRENCE_THRESHOLD,
    }

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        service = TicketService(ctx.settings, ctx.board_id)
        threshold = self._threshold_for("CI_FAILURE", ctx)

        groups = service.check_recurring_categories(
            category="CI_FAILURE",
            threshold=threshold,
            repo_id=ctx.board_id,
        )
        if not groups:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary=f"No CI_FAILURE sub-categories have reached the "
                f"threshold of {threshold} distinct ticket(s).",
            )

        drafts_created: list[dict[str, Any]] = []
        for g in groups:
            title = (
                f"Recurring CI failure: {g['sub_category']} "
                f"({g['distinct_tickets']} ticket(s))"
            )
            body = (
                f"## Recurring CI failure detected\n\n"
                f"**Category:** {g['category']}\n\n"
                f"**Failure mode:** {g['sub_category']}\n\n"
                f"**Distinct tickets:** {g['distinct_tickets']}\n\n"
                f"**Sample ticket(s):** {', '.join(g['ticket_ids'][:5])}\n\n"
                f"**Latest reason:**\n```\n{g['reason']}\n```\n"
            )
            try:
                ticket = service.create(
                    title=title,
                    description=body,
                    source=SourceKind.RECURRING_CATEGORY,
                    priority=True,
                )
                drafts_created.append({"id": ticket.id, "title": title})
            except Exception:
                log.exception(
                    "Failed to file fix-proposal ticket for recurring category %s / %s",
                    g["category"],
                    g["sub_category"],
                )

        return DiagnosticCheckResult(
            name=self.name,
            ok=len(groups) == 0 or len(drafts_created) > 0,
            summary=(
                f"Found {len(groups)} recurring CI_FAILURE group(s) above "
                f"threshold {threshold}; filed {len(drafts_created)} fix-proposal "
                f"ticket(s)."
            ),
            drafts_created=drafts_created,
        )

    @staticmethod
    def _threshold_for(category: str, ctx: DiagnosticCheckContext) -> int:
        """Return the recurrence threshold for *category*.

        Reads the category-specific setting when available;
        falls back to the default defined in ``TRACKED_CATEGORIES``.
        """
        default = RecurringCategoryCheck.TRACKED_CATEGORIES.get(category, 3)
        # Allow Settings to override per-category thresholds in the future.
        # For now, use the default.
        _ = ctx  # reserved for future Settings integration
        return default


# Singleton instance for registration.
recurring_category_check = RecurringCategoryCheck()

register_check(recurring_category_check)

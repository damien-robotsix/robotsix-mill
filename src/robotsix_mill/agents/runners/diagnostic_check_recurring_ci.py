"""Recurring CI failure diagnostic check — summary only.

A :class:`DiagnosticCheck` that reads the diagnostic event store and
reports how the ``CI_FAILURE`` events distribute across semantic buckets
(``ruff-format``, ``mypy``, ``pytest-failure``, …) and how many of them
the ci-fix agent later resolved.

It files **no tickets**. The previous incarnation auto-filed a
``[diagnostic] recurring CI failure: key=…`` report ticket per normalized
key once ``diagnostic_ci_failure_threshold`` distinct tickets had hit it.
That was noise: the key was a hash of the failing check *names* (in
practice ``"failing checks: ci / tests"``), the dedup only looked at
non-terminal twins so the same eight keys were re-filed on every pass
(21 report tickets in three passes), and each report then consumed
refine/implement cycles for a "review this pattern" task. Learning from
recurring CI failures is now the job of the ``ci_prevention_rules``
periodic pass, which rewrites a rules section in the implement agent's
memory ledger instead.

Registered via :func:`register_check` so the daily diagnostic agent picks
it up automatically.
"""

from __future__ import annotations

import logging
from collections import Counter

from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_events import list_diagnostic_events

log = logging.getLogger(__name__)


class RecurringCIFailureCheck:
    """Summarise recurring CI failures by bucket; never files tickets."""

    name = "recurring_ci_failure"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Summarise the board's ``CI_FAILURE`` events by semantic bucket."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("recurring_ci_failure check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="recurring_ci_failure check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        events = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
        if not events:
            return DiagnosticCheckResult(
                name=self.name,
                ok=True,
                summary="no CI_FAILURE events in store",
            )
        resolved = list_diagnostic_events(
            settings, board_id, category="CI_FIX_RESOLVED"
        )

        by_bucket: Counter[str] = Counter(ev.bucket or "unknown" for ev in events)
        tickets = {ev.ticket_id for ev in events}
        breakdown = ", ".join(
            f"{bucket}={count}" for bucket, count in by_bucket.most_common()
        )
        summary = (
            f"{len(events)} CI_FAILURE event(s) across {len(tickets)} ticket(s) "
            f"[{breakdown}]; {len(resolved)} resolved by ci_fix; "
            "prevention rules are maintained by the ci_prevention_rules pass "
            "(no tickets filed)"
        )
        return DiagnosticCheckResult(name=self.name, ok=True, summary=summary)


register_check(RecurringCIFailureCheck())

"""Error-detection diagnostic check.

The first concrete :class:`~robotsix_mill.agents.runners.diagnostic_checks.DiagnosticCheck`
of the daily diagnostic agent. It consumes the shared, fail-safe data
layer (:mod:`diagnostic_data`) to find errored runs in the last 24h and
emits one deduplicated diagnostic event per unique error per day. It
files **no tickets** — investigation findings belong in a chat
subsession, not in a ticket.

Scope is deliberately the runs-log source only: the normalized Langfuse
trace shape exposes no error/level field yet, so errored-trace detection
is a follow-up that must first extend the data layer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ...runtime.run_registry import RESTART_INTERRUPTED_ERROR
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_data import query_run_errors
from .diagnostic_events import emit_diagnostic_event

log = logging.getLogger(__name__)


def _signature(run: dict[str, Any]) -> str:
    """Derive a normalized, single-line signature for an errored *run*.

    Uses the first non-empty line of the ``error`` field (stripped),
    falling back to ``summary`` and finally ``"unknown"``.
    """
    for source in (run.get("error"), run.get("summary")):
        if isinstance(source, str):
            for line in source.splitlines():
                stripped = line.strip()
                if stripped:
                    return stripped
    return "unknown"


def _fingerprint(run: dict[str, Any]) -> tuple[str, str]:
    """Group key for an errored *run*: ``(kind, normalized signature)``."""
    kind = run.get("kind") or "unknown"
    return (str(kind), _signature(run))


class ErroredRunsCheck:
    """Detect errored runs and surface them as diagnostic events (no tickets)."""

    name = "errored_runs"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the errored-runs check and emit diagnostic events."""
        try:
            return self._run(ctx)
        except Exception:
            log.exception("errored_runs check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="errored_runs check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

        errors = query_run_errors(board_id, since=since, settings=settings)

        # A run the process restart killed mid-flight is stamped
        # RESTART_INTERRUPTED_ERROR by RunRegistry.  That is a deploy
        # artifact, not a defect in the pass: filing it produced a
        # "[diagnostic] errored run: completeness_check — interrupted by
        # process restart" ticket on every mill deploy (c05c, 2026-08-26)
        # that no one could act on.  Count and skip.
        restart_interrupted = [
            run for run in errors if _signature(run) == RESTART_INTERRUPTED_ERROR
        ]
        if restart_interrupted:
            log.info(
                "errored_runs: skipping %d run(s) interrupted by process restart "
                "(board=%s)",
                len(restart_interrupted),
                board_id,
            )
            errors = [run for run in errors if run not in restart_interrupted]

        # Group errored runs by fingerprint (kind + normalized signature).
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for run in errors:
            groups.setdefault(_fingerprint(run), []).append(run)

        if not groups:
            summary = "no errored runs detected in the last 24h"
            log.info("errored_runs: %s (board=%s)", summary, board_id)
            return DiagnosticCheckResult(name=self.name, ok=True, summary=summary)

        emitted = 0
        for (kind, signature), runs in groups.items():
            log.info(
                "errored_runs: detected %d run(s) for fingerprint "
                "(kind=%s, signature=%r)",
                len(runs),
                kind,
                signature,
            )
            # Surface the finding as a diagnostic event only — never a
            # ticket. Investigation belongs in a chat subsession, not in a
            # ticket (operator policy). The event store dedups on
            # (category, ticket_id, normalized_key).
            first_run_id = str(runs[0].get("id") or "unknown")
            try:
                if emit_diagnostic_event(
                    settings,
                    board_id,
                    category="ERRORED_RUN",
                    ticket_id=first_run_id,
                    reason=f"{kind}: {signature}",
                    normalized_key=f"{kind}::{signature}",
                ):
                    emitted += 1
            except Exception:
                log.exception(
                    "errored_runs: failed to emit event for fingerprint "
                    "(kind=%s, signature=%r)",
                    kind,
                    signature,
                )

        summary = (
            f"{len(errors)} errored run(s) in {len(groups)} group(s); "
            f"{emitted} diagnostic event(s) emitted (no tickets filed)"
        )
        return DiagnosticCheckResult(
            name=self.name,
            ok=True,
            summary=summary,
        )


register_check(ErroredRunsCheck())

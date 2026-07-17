"""Error-detection diagnostic check.

The first concrete :class:`~robotsix_mill.runners.diagnostic_checks.DiagnosticCheck`
of the daily diagnostic agent. It consumes the shared, fail-safe data
layer (:mod:`diagnostic_data`) to find errored runs in the last 24h and
auto-files exactly one deduplicated draft ticket per unique error per
day.

Scope is deliberately the runs-log source only: the normalized Langfuse
trace shape exposes no error/level field yet, so errored-trace detection
is a follow-up that must first extend the data layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.models import SourceKind, TicketKind
from ..core.service import TicketService
from ..core.states import DONE_OR_CLOSED
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    register_check,
)
from .diagnostic_data import query_run_errors

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
    """Detect errored runs and file one draft ticket per unique error/day."""

    name = "errored_runs"

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        try:
            return self._run(ctx)
        except Exception:  # noqa: BLE001 — preserve the log-and-swallow contract
            log.exception("errored_runs check failed")
            return DiagnosticCheckResult(
                name=self.name,
                ok=False,
                summary="errored_runs check raised an exception",
            )

    def _run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        settings = ctx.settings
        board_id = ctx.board_id

        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        errors = query_run_errors(board_id, since=since, settings=settings)

        # Group errored runs by fingerprint (kind + normalized signature).
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for run in errors:
            groups.setdefault(_fingerprint(run), []).append(run)

        if not groups:
            summary = "no errored runs detected in the last 24h"
            log.info("errored_runs: %s (board=%s)", summary, board_id)
            return DiagnosticCheckResult(name=self.name, ok=True, summary=summary)

        service = TicketService(settings, board_id=board_id)
        drafts_created: list[dict[str, Any]] = []

        for (kind, signature), runs in groups.items():
            log.info(
                "errored_runs: detected %d run(s) for fingerprint "
                "(kind=%s, signature=%r)",
                len(runs),
                kind,
                signature,
            )
            short_sig = signature if len(signature) <= 80 else signature[:77] + "..."
            title = f"[diagnostic] errored run: {kind} — {short_sig} ({today})"
            try:
                if self._is_duplicate(title, service):
                    log.info("errored_runs: skipping duplicate ticket %r", title)
                    continue
                body = self._build_body(board_id, kind, signature, runs)
                ticket = service.create(
                    title,
                    body,
                    source=SourceKind.AGENT,
                    kind=TicketKind.TASK,
                )
                log.info("errored_runs: created ticket %s — %r", ticket.id, title)
                drafts_created.append({"id": ticket.id, "title": title})
            except Exception:  # noqa: BLE001 — one failed group must not block others
                log.exception(
                    "errored_runs: failed to file ticket for fingerprint "
                    "(kind=%s, signature=%r)",
                    kind,
                    signature,
                )

        summary = (
            f"{len(errors)} errored run(s) in {len(groups)} group(s); "
            f"{len(drafts_created)} draft(s) filed"
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
        kind: str,
        signature: str,
        runs: list[dict[str, Any]],
    ) -> str:
        """Build the investigating-context ticket body for an error group."""
        lines = [
            "Auto-filed by the daily diagnostic agent (errored_runs check).",
            "",
            f"- **Repository / board:** `{board_id}`",
            f"- **Error kind:** `{kind}`",
            f"- **Signature:** {signature}",
            f"- **Affected runs:** {len(runs)}",
            "",
            "### Affected run(s)",
        ]
        for run in runs:
            lines.append(
                f"- run `{run.get('id')}` — started_at "
                f"`{run.get('started_at')}`, finished_at "
                f"`{run.get('finished_at')}`"
            )
        lines.append("")
        lines.append("### Raw error(s)")
        for run in runs:
            error_text = run.get("error") or run.get("summary") or "(no error text)"
            lines.append(f"- run `{run.get('id')}`:")
            lines.append("")
            lines.append("```")
            lines.append(str(error_text))
            lines.append("```")
        return "\n".join(lines) + "\n"


register_check(ErroredRunsCheck())

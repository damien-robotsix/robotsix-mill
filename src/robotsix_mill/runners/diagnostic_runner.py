"""Daily diagnostic agent — deterministic check orchestrator.

This is the foundational skeleton of the "daily diagnostic agent" epic.
It is a plain-Python orchestrator (no LLM, no memory ledger) that
iterates the pluggable check registry (``diagnostic_checks``) and
aggregates the per-check results into a single pass result.

The skeleton ships with ZERO registered checks: an empty-registry pass
returns an empty-but-valid result. Later epic children register concrete
checks (error detection, draft-count validation) via
``diagnostic_checks.register_check`` WITHOUT editing this runner.

Each check is run inside its own ``try/except`` so one failing check
never aborts the pass — the failure is logged, recorded as a failed
result, and the next check still runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .diagnostic_checks import DiagnosticCheckResult, get_registered_checks

log = logging.getLogger(__name__)


@dataclass
class DiagnosticPassResult:
    """Aggregated outcome of one diagnostic pass.

    ``drafts_created`` is required because the shared poll loop
    (``_run_periodic_pass``) reads ``result.drafts_created`` and
    optionally ``result.summary``.
    """

    drafts_created: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


def run_diagnostic_pass(session_id: str) -> DiagnosticPassResult:
    """Run a full diagnostic pass over the registered checks.

    Mirrors the shape of ``run_run_health_pass``: construct ``Settings()``,
    resolve the target board, iterate the check registry, and aggregate
    each check's ``drafts_created`` into the pass result. With an empty
    registry this returns an empty-but-valid result.
    """
    settings = Settings()
    board_id = settings.diagnostic_target_repo_id

    checks = get_registered_checks()
    log.info(
        "Diagnostic pass starting (session=%s, board=%s, checks=%d)",
        session_id,
        board_id,
        len(checks),
    )

    results: list[DiagnosticCheckResult] = []
    for check in checks:
        try:
            results.append(check.run())
        except Exception:
            # One failing check never aborts the pass: log, record a
            # failed result, and continue to the next check.
            log.exception("Diagnostic check %s failed", check.name)
            results.append(
                DiagnosticCheckResult(
                    name=check.name,
                    ok=False,
                    summary="check raised an exception",
                )
            )

    drafts_created: list[dict[str, Any]] = []
    for r in results:
        drafts_created.extend(r.drafts_created)
    failed = sum(1 for r in results if not r.ok)

    summary = (
        f"{len(checks)} check(s) run, {failed} failed, "
        f"{len(drafts_created)} draft(s) filed"
    )
    log.info("Diagnostic pass complete (session=%s): %s", session_id, summary)

    return DiagnosticPassResult(drafts_created=drafts_created, summary=summary)

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
from .diagnostic_checks import (
    DiagnosticCheckContext,
    DiagnosticCheckResult,
    get_registered_checks,
)

# Import concrete check modules for their register_check side-effect.
# These live here (not in diagnostic_checks) to avoid a cyclic import:
# each check module imports from diagnostic_checks, so importing them
# from diagnostic_checks would create a cycle.
from . import diagnostic_check_errors  # noqa: E402,F401
from . import diagnostic_check_recurring_ci  # noqa: E402,F401

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


def _accessible_repos(monitored: list[str]) -> tuple[list[str], list[str]]:
    """Partition *monitored* into ``(valid, invalid)`` by accessibility.

    A repo is accessible if it is a registered repo OR the synthetic meta
    board (mirroring ``diagnostic_data._repo_config_for``). The
    ``load_repos_config()`` call is wrapped in the log-and-swallow
    contract: on any failure we log and treat every configured repo as
    valid (attempt unvalidated) rather than crashing the pass.
    """
    try:
        from ..config.repos import load_repos_config
        from ..runtime.worker.core import Worker

        registry = load_repos_config()
        valid: list[str] = []
        invalid: list[str] = []
        for repo_id in monitored:
            if repo_id == Worker._META_BOARD or repo_id in registry.repos:
                valid.append(repo_id)
            else:
                invalid.append(repo_id)
        return valid, invalid
    except Exception:  # noqa: BLE001 — a config-load outage must not crash the pass
        log.exception(
            "diagnostic: load_repos_config failed; attempting all "
            "configured repos unvalidated"
        )
        return list(monitored), []


def run_diagnostic_pass(session_id: str, repo_config: Any = None) -> DiagnosticPassResult:
    """Run a full diagnostic pass over every monitored repo × check.

    Construct ``Settings()``, resolve the monitored-repository list
    (``diagnostic_monitored_repo_ids``, falling back to the single
    ``diagnostic_target_repo_id`` when empty), validate accessibility
    (unknown repos are logged and skipped, never raised), then iterate
    the check registry once per valid repo — handing each check a
    :class:`DiagnosticCheckContext`. Each ``(repo, check)`` pair runs in
    its own ``try/except`` so one failure never aborts the pass. With an
    empty registry this returns an empty-but-valid result.
    """
    settings = Settings()
    monitored = settings.diagnostic_monitored_repo_ids or [
        settings.diagnostic_target_repo_id
    ]
    valid, invalid = _accessible_repos(monitored)

    checks = get_registered_checks()
    log.info(
        "Diagnostic pass starting (session=%s): monitoring %d repo(s): %s, checks=%d",
        session_id,
        len(valid),
        valid,
        len(checks),
    )
    if invalid:
        log.warning(
            "Diagnostic pass: skipping %d inaccessible repo(s): %s",
            len(invalid),
            invalid,
        )

    results: list[DiagnosticCheckResult] = []
    for board_id in valid:
        ctx = DiagnosticCheckContext(board_id=board_id, settings=settings)
        for check in checks:
            try:
                results.append(check.run(ctx))
            except Exception:
                # One failing (repo, check) pair never aborts the pass: log,
                # record a failed result, and continue.
                log.exception(
                    "Diagnostic check %s failed for board %s",
                    check.name,
                    board_id,
                )
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
        f"{len(valid)} repo(s) × {len(checks)} check(s), {failed} failed, "
        f"{len(drafts_created)} draft(s) filed"
    )
    log.info("Diagnostic pass complete (session=%s): %s", session_id, summary)

    return DiagnosticPassResult(drafts_created=drafts_created, summary=summary)

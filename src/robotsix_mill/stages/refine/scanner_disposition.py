"""Per-source bulk auto-disposition policy for scanner findings.

Scanner / finding periodic agents (audit, module_curator, health, …) fan
out large numbers of auto-generated "needs human" drafts into the refine
stage, where many land in ``HUMAN_ISSUE_APPROVAL`` and stall the human
gate.  This module gives each scanner source a *deterministic*
disposition so the refine triage can dispose of scanner findings before
they flood the human gate:

* ``auto-close-stale`` — close the finding without a human (terminal).
* ``auto-ready``       — route straight to implement (low-risk /
  well-scoped), skipping the human gate.
* ``hold-for-human``   — keep the human decision, but retained under the
  source's existing rollup epic (``pass_runner._find_or_create_scanner_epic``)
  rather than fanned out as individual gate tickets.

This is the scanner-category equivalent of the orphaned-PR bulk
auto-disposition (``orphaned_pr_check``).
"""

from __future__ import annotations

from enum import StrEnum

from ...config.settings import Settings
from ...core.models import SourceKind


class ScannerDisposition(StrEnum):
    """The three disposition outcomes a scanner source can be mapped to."""

    AUTO_CLOSE_STALE = "auto-close-stale"
    AUTO_READY = "auto-ready"
    HOLD_FOR_HUMAN = "hold-for-human"


#: Scanner sources covered by the bulk auto-disposition policy.  A source
#: outside this set is never auto-dispositioned — triage falls through to
#: its normal path.
SCANNER_POLICY_SOURCES: frozenset[str] = frozenset(
    {
        SourceKind.MODULE_CURATOR,
        SourceKind.AUDIT,
        SourceKind.COMPLETENESS_CHECK,
        SourceKind.MODULE_SIZE,
        SourceKind.SURVEY,
        SourceKind.DOCSTRING_COVERAGE,
        SourceKind.AGENT_CHECK,
        SourceKind.HEALTH,
        SourceKind.BC_CHECK,
    }
)


#: Built-in default disposition per scanner source.  The ``AUTO_READY``
#: entries mirror the sources already in ``_AUTO_APPROVE_SOURCES`` (they
#: route straight to implement today); the churn-heavy sources default to
#: ``AUTO_CLOSE_STALE`` and the design/scope sources to
#: ``HOLD_FOR_HUMAN``.  Operators override per-source via
#: ``settings.scanner_auto_disposition_policy``.
DEFAULT_SCANNER_DISPOSITION: dict[str, ScannerDisposition] = {
    SourceKind.AUDIT: ScannerDisposition.AUTO_READY,
    SourceKind.AGENT_CHECK: ScannerDisposition.AUTO_READY,
    SourceKind.BC_CHECK: ScannerDisposition.AUTO_READY,
    SourceKind.COMPLETENESS_CHECK: ScannerDisposition.AUTO_READY,
    SourceKind.MODULE_CURATOR: ScannerDisposition.AUTO_READY,
    SourceKind.DOCSTRING_COVERAGE: ScannerDisposition.AUTO_CLOSE_STALE,
    SourceKind.MODULE_SIZE: ScannerDisposition.AUTO_CLOSE_STALE,
    SourceKind.HEALTH: ScannerDisposition.HOLD_FOR_HUMAN,
    SourceKind.SURVEY: ScannerDisposition.HOLD_FOR_HUMAN,
}


def resolve_scanner_disposition(
    source: str, settings: Settings
) -> ScannerDisposition | None:
    """Return the configured :class:`ScannerDisposition` for *source*.

    Returns ``None`` when *source* is not a scanner source covered by the
    policy (so the caller falls through to the normal triage path), or
    when a configured value is unrecognised.  A per-source override in
    ``settings.scanner_auto_disposition_policy`` takes precedence over the
    built-in :data:`DEFAULT_SCANNER_DISPOSITION`.
    """
    if source not in SCANNER_POLICY_SOURCES:
        return None

    override = (settings.scanner_auto_disposition_policy or {}).get(source)
    if override is not None:
        try:
            return ScannerDisposition(override)
        except ValueError:
            # Unrecognised override — ignore it and fall back to the
            # built-in default rather than silently mis-routing.
            pass

    return DEFAULT_SCANNER_DISPOSITION.get(source)
